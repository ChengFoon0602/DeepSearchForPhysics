# 用裸 LangGraph 重造最小多智能体 Orchestrator
#
# 目的：理解 deepagents 的核心机制（agent 循环 + task 子智能体 + Command 注入 + interrupt），
#       从"会用框架"变成"会造框架"。每块注释标注对应 deepagents 源码的位置。
#
# 三个 stage：
#   B1 最小 agent 循环（model → tools → model，直到模型不再调工具）—— 对应 create_agent
#   B2 task 子智能体工具（主 agent 调 task → 子智能体嵌套运行 → Command 注入结果）—— 对应 SubAgentMiddleware._build_task_tool
#   B3 interrupt/resume（审批点挂起等人类决策）—— 对应 HumanInTheLoopMiddleware + LangGraph interrupt
#
# 复用本项目的 ReasoningChatModel（deepseek-v4-flash 思考模型 + 自定义流式），
# 只重造编排机制，不重复造模型层。
import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool, tool
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent.reasoning_model import ReasoningChatModel


# ============ 工具（演示用） ============
@tool
def add_numbers(a: int, b: int) -> str:
    """加法计算工具。"""
    return f"{a} + {b} = {a + b}"


@tool
def generate_report(title: str, content: str) -> str:
    """演示"生成报告"工具（B3 里会被拦截审批）。"""
    return f"[已生成报告 {title}] {content[:30]}..."


# ============ 状态 + 图构建（B1） ============
class AgentState(TypedDict):
    messages: Annotated[list, operator.add]


def build_agent_loop(model, tools, *, system_prompt: str = "你是一个助手。", checkpointer=None):
    """B1：最小 agent 循环。

    对应 deepagents 的 create_agent（.venv/.../deepagents/graph.py 的 middleware 栈组装，
    但图的骨架是 langchain.agents.create_agent 提供的：model 节点 + tools 节点 + 路由）。
    """
    # 工具按 name 索引，供 tools 节点手动执行（比 ToolNode 更贴近"从零造"，且能看清执行流）
    tool_map = {t.name: t for t in tools}
    model_with_tools = model.bind_tools(tools)

    def call_model(state: AgentState):
        """模型节点：带历史消息调 LLM，返回 AIMessage。"""
        msgs = state["messages"]
        if system_prompt and not any(isinstance(m, SystemMessage) for m in msgs):
            msgs = [SystemMessage(content=system_prompt), *msgs]
        response = model_with_tools.invoke(msgs)
        return {"messages": [response]}

    def call_tools(state: AgentState):
        """工具节点：遍历最后一条消息的 tool_calls，执行并返回 ToolMessage。"""
        last = state["messages"][-1]
        tool_messages = []
        for tc in last.tool_calls:
            tool = tool_map[tc["name"]]
            result = tool.invoke(tc["args"])
            tool_messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
        return {"messages": tool_messages}

    def should_continue(state: AgentState):
        """路由器：还有工具调用就回 tools，否则结束。"""
        last = state["messages"][-1]
        return "tools" if last.tool_calls else END

    graph = StateGraph(AgentState)
    graph.add_node("model", call_model)
    graph.add_node("tools", call_tools)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "model")
    return graph.compile(checkpointer=checkpointer)


# ============ B2：task 子智能体工具 ============
def build_task_tool(subagents: dict):
    """构造 `task` 工具：按名字唤起子智能体，结果以 Command 注入主图。

    对应 deepagents SubAgentMiddleware._build_task_tool：
      1. _validate_and_prepare_state：构造子智能体状态 messages=[HumanMessage(description)]
      2. subagent.invoke(state) 嵌套运行（子智能体对主图是黑盒）
      3. _return_command_with_state_update：返回 Command(update={"messages":[ToolMessage(结果)]})
    """
    def task(description: str, subagent_type: str, runtime=None) -> Command:
        if subagent_type not in subagents:
            return Command(update={"messages": [ToolMessage(
                content=f"没有子智能体 {subagent_type}，可选：{list(subagents)}",
                tool_call_id=runtime.tool_call_id if runtime else "")]})
        subagent = subagents[subagent_type]
        # 子智能体状态：只有一条 HumanMessage(description)（隔离上下文，主图历史不泄漏）
        result = subagent.invoke({"messages": [HumanMessage(content=description)]})
        # 取子智能体最终消息作为结果，包成 ToolMessage 注回主图
        final_text = result["messages"][-1].content
        tool_call_id = runtime.tool_call_id if runtime else ""
        return Command(update={"messages": [ToolMessage(content=final_text, tool_call_id=tool_call_id)]})

    return StructuredTool.from_function(name="task", func=task, description=(
        "唤起一个子智能体处理独立任务。参数：description（给子智能体的详细指令），"
        "subagent_type（子智能体名字，必须是其中之一）。"
    ))


def build_subagent(model, tools, *, system_prompt: str):
    """构造一个独立子智能体 agent（复用 B1 的 agent 循环）。

    对应 deepagents _get_subagents_legacy 里对每个 subagent 调 create_agent(...)。
    """
    return build_agent_loop(model, tools, system_prompt=system_prompt)


# ============ B3：interrupt/resume 审批节点 ============
def add_approval_node(graph, *, model, tools, system_prompt: str):
    """在 agent 循环上插入"生成报告前审批"节点。

    对应 deepagents HumanInTheLoopMiddleware：模型发出 generate_report 工具调用前，
    图先进入审批节点 interrupt() 挂起，等 Command(resume=...) 后再决定是否放行工具执行。

    注意：interrupt() 只能在图节点里调用（不能放工具里），所以审批是独立节点 + 条件边。
    """
    from langgraph.checkpoint.memory import MemorySaver

    tool_map = {t.name: t for t in tools}
    model_with_tools = model.bind_tools(tools)

    def call_model(state: AgentState):
        msgs = state["messages"]
        if system_prompt and not any(isinstance(m, SystemMessage) for m in msgs):
            msgs = [SystemMessage(content=system_prompt), *msgs]
        return {"messages": [model_with_tools.invoke(msgs)]}

    def approval_gate(state: AgentState):
        """审批节点：拦截生成类工具挂起等人类决策，其余 tool_call 正常执行。

        注意：OpenAI 协议要求"带 tool_calls 的 assistant 消息后必须跟对应每个
        tool_call_id 的 tool 消息"。所以同批次里非生成类工具也要一并执行返回，
        否则消息序列不完整，下一轮 model 调用会 400。
        """
        last = state["messages"][-1]
        pending = [tc for tc in last.tool_calls if tc["name"] == "generate_report"]
        tool_messages = []

        if pending:
            # 挂起，把待审信息暴露给调用方；返回值是 resume 时 Command 传入的
            decision = interrupt({
                "tool": "generate_report",
                "args": pending[0]["args"],
                "question": "是否批准生成报告？",
            })
            if decision.get("approve"):
                result = tool_map["generate_report"].invoke(pending[0]["args"])
                tool_messages.append(ToolMessage(content=str(result), tool_call_id=pending[0]["id"]))
            else:
                tool_messages.append(ToolMessage(
                    content=f"用户拒绝了生成报告：{decision.get('message', '')}",
                    tool_call_id=pending[0]["id"]))

        # 同批次其它 tool_call 正常执行（保证每条 tool_call 都有对应 ToolMessage）
        for tc in last.tool_calls:
            if tc["name"] == "generate_report":
                continue
            result = tool_map[tc["name"]].invoke(tc["args"])
            tool_messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

        return {"messages": tool_messages}

    def route(state: AgentState):
        last = state["messages"][-1]
        if not last.tool_calls:
            return END
        # 有 generate_report 的走审批节点，否则直接走工具节点
        return "approval" if any(tc["name"] == "generate_report" for tc in last.tool_calls) else "tools"

    graph = StateGraph(AgentState)
    graph.add_node("model", call_model)
    graph.add_node("tools", lambda s: _exec_tools(s, tool_map))
    graph.add_node("approval", approval_gate)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route, {"tools": "tools", "approval": "approval", END: END})
    graph.add_edge("tools", "model")
    graph.add_edge("approval", "model")   # 审批后回模型（重试或继续）
    return graph.compile(checkpointer=MemorySaver())


def _exec_tools(state: AgentState, tool_map: dict):
    last = state["messages"][-1]
    tool_messages = []
    for tc in last.tool_calls:
        tool = tool_map[tc["name"]]
        result = tool.invoke(tc["args"])
        tool_messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
    return {"messages": tool_messages}


# ============ 主入口：组装模型 + 工具 ============
def get_model():
    """复用项目模型（思考模型 + 自定义流式）。"""
    import os
    from dotenv import load_dotenv
    load_dotenv("D:/桌面文件/大三/agent/deep-search-pro-master/.env")
    return ReasoningChatModel(
        model=os.getenv("LLM_QWEN_MAX"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY"),
        max_retries=3,
        timeout=60,
    )
