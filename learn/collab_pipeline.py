# 用裸 LangGraph（CompiledSubAgent / 子图嵌套）重造「协作流水线」
#
# 目的：一次只抽一层公共依赖（工具列表 = internet_search + extract_web_content），
#       但这次是**分阶段的真流水线**——网络搜索子图跑完，把结果交给物理验证子图，
#       物理验证子图跑完，统一由外层主 agent 汇总。突破 mini_orchestrator 的
#       hub-and-spoke 黑盒：子智能体不再只拿到一条 description，而是能拿到
#       上一个子图**完整的结果**。
#
# 为什么放 learn/ 不进生产：
#   - 生产价值来自信息质量，不是流水线形态（见计划文件 Context / 当心降质对话）。
#   - 这里用「真实子智能体 + 真实工具」把机制走真：复用 tools.tavily_tool，
#     复用 .env + ReasoningChatModel，证明不是玩具代码。
#
# 复用项目已有件（不重复造轮子）：
#   - agent/reasoning_model.py 的 ReasoningChatModel（deepseek 思考模型 + 自定义流式）
#   - tools/tavily_tool.py 的 internet_search / extract_web_content（真实工具，含 monitor 埋点）
#   - learn/mini_orchestrator.py 的 build_agent_loop（同一套 agent 循环骨架）
#
# 机制（对照 deepagents / langchain 源码）：
#   - create_agent(...).compile() 出来的是一个 Runnable（CompiledStateGraph）。
#     deepagents 官方注释（.venv/.../deepagents/middleware/subagents.py:
#     CompiledSubAgent 相关）：子图/预编译 runnable 作为子智能体时，state 里
#     必须有 messages 键（本项目 AgentState 满足），并且调用是「嵌套 invoke」——
#     对主图仍是黑盒，但状态可以按需透传。
#   - 这里直接用「主图节点里调子图.invoke」的方式把两个阶段串成顺序依赖，
#     search 子图的最终结果被写进 verify 子图的输入 —— 这就是协作流水线的核心：
#     阶段间信息传递 = 「上一个子图的输出喂给下一个子图的输入」。

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from agent.reasoning_model import ReasoningChatModel
from tools.tavily_tool import internet_search, extract_web_content


# ============ 状态 ============
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    verify_report: str          # 物理验证子图的最终报告（跨阶段传递）

# ============ 复用 agent 循环骨架 ============
def build_agent_loop(model, tools, *, system_prompt: str = ""):
    tool_map = {t.name: t for t in tools}
    model_with_tools = model.bind_tools(tools)

    def call_model(state: AgentState):
        msgs = state["messages"]
        if system_prompt and not any(isinstance(m, SystemMessage) for m in msgs):
            msgs = [SystemMessage(content=system_prompt), *msgs]
        return {"messages": [model_with_tools.invoke(msgs)]}

    def call_tools(state: AgentState):
        last = state["messages"][-1]
        tool_messages = []
        for tc in last.tool_calls:
            tool = tool_map[tc["name"]]
            try:
                result = tool.invoke(tc["args"])
            except Exception as e:
                result = f"[工具执行失败] {type(e).__name__}: {e}"
            tool_messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
        return {"messages": tool_messages}

    def should_continue(state: AgentState):
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    graph = StateGraph(AgentState)
    graph.add_node("model", call_model)
    graph.add_node("tools", call_tools)
    # 注意：路由不能只依赖「有 tool_calls」，因为子图 invocation 也在其中
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "model")
    return graph.compile()


# ============ 子图（阶段）============
def build_search_subagent(model, system_prompt):
    """阶段①网络搜索子图：工具 = internet_search + extract_web_content。"""
    return build_agent_loop(model, [internet_search, extract_web_content],
                            system_prompt=system_prompt)


def build_verify_subagent(model, system_prompt):
    """阶段②物理验证子图：工具 = internet_search + extract_web_content (+ verify_citations)。"""
    from tools.verify_citations_tool import verify_citations
    return build_agent_loop(model, [internet_search, extract_web_content, verify_citations],
                            system_prompt=system_prompt)


# ============ 协作流水线主图 ============
def build_collab_pipeline(model, *, search_system_prompt, verify_system_prompt,
                          search_tool_names=("internet_search", "extract_web_content")):
    """把两个子图按顺序串成一条流水线：search → verify → 汇总。

    这就是我们「重造」的核心：打破 hub-and-spoke 黑盒 ——
    子智能体之间不再只能通过主 agent 的 description 传递，而是「子图输入的
    HumanMessage 携带上一个子图的完整输出」，形成阶段间「数据依赖」。
    """
    search_subagent = build_search_subagent(model, search_system_prompt)
    verify_subagent = build_verify_subagent(model, verify_system_prompt)

    tool_map = {}
    for name in search_tool_names:   # 主图也能看到子图工具名
        if name == "internet_search":
            tool_map[name] = internet_search
        elif name == "extract_web_content":
            tool_map[name] = extract_web_content

    def run_search(state):
        """阶段①：把用户问题喂给搜索子图，收到最终结果。"""
        # 知识摘要把如何放到 messages？直接作为一条 user 消息。
        user_text = state["messages"][-1].content
        result = search_subagent.invoke({"messages": [HumanMessage(content=user_text)]})
        search_out = result["messages"][-1].content
        # 记录搜索结论（模型是黑盒，但我们把「结论」写进 state 供下一阶段用）
        return {
            "messages": [ToolMessage(content=search_out,
                                     tool_call_id="search-phase", name="search_subagent")],
            "verify_report": "",  # 占位，本阶段还没有
        }

    def run_verify(state):
        """阶段②：把上一阶段结果完整喂给验证子图（自包含 description）。"""
        search_out = state["messages"][-1].content  # 上一个阶段写入的 ToolMessage 内容
        verify_input = (
            "【上一步（网络搜索）的完整结论】\n"
            f"{search_out}\n\n"
            "请核对上面的公式/来源及其推导。若输入含可校验的 URL，用 verify_citations "
            "校验其真实性；输出你验证后的结论与来源状态。"
        )
        result = verify_subagent.invoke({"messages": [HumanMessage(content=verify_input)]})
        verify_out = result["messages"][-1].content
        return {"messages": [ToolMessage(content=verify_out, tool_call_id="verify-phase",
                                         name="verify_subagent")],
                "verify_report": verify_out}

    def run_summary(state):
        """阶段③：主 agent 汇总两阶段输出，产出最终报告。"""
        # 把子图结果注入主 agent 的上下文
        report = _compose_report(state)  # （真实场景可由主模型生成 Markdown；这里先拼文本回报）
        return {"messages": [AIMessage(content=report)]}

    graph = StateGraph(AgentState)
    graph.add_node("search", run_search)
    graph.add_node("verify", run_verify)
    graph.add_node("summary", run_summary)
    graph.add_edge(START, "search")
    graph.add_edge("search", "verify")
    graph.add_edge("verify", "summary")
    graph.add_edge("summary", END)
    return graph.compile()


def _compose_report(state):
    """把 search / verify 两阶段结果拼成最终报告（文本，学习产物不落盘）。"""
    texts = [m.content for m in state["messages"] if isinstance(m, ToolMessage) and m.name]
    search_txt = texts[0] if len(texts) > 0 else "(无搜索结论)"
    verify_txt = texts[1] if len(texts) > 1 else "(无验证结论)"
    return (
        "# 协作流水线最终报告\n\n"
        "## ① 阶段：网络搜索结论\n\n"
        + str(search_txt)[:500] +
        "\n\n## ② 阶段：物理验证结论\n\n"
        + str(verify_txt)[:500]
    )


def get_model():
    """复用项目的真实模型（deepseek-v4-flash 思考模型 + 自定义流式），读 .env。"""
    import os
    from dotenv import load_dotenv
    from agent.llm import model
    load_dotenv()
    return model