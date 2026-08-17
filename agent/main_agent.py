from agent.subagents.knowledge_base_agent import knowledge_base_agent
from agent.subagents.database_query_agent import database_query_agent
from agent.subagents.network_search_agent import network_search_agent
from agent.subagents.physics_lit_agent import physics_lit_agent
import asyncio
import aiosqlite
import datetime
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

# main_agent tool导入
from tools.markdown_tools import generate_markdown
from tools.pdf_tools import convert_md_to_pdf
from tools.docx_tools import generate_docx
from tools.upload_file_read_tool import read_file_content

from deepagents import create_deep_agent

from agent.llm import model
from agent.prompts import main_agent_content

from api.monitor import monitor
import asyncio
import uuid
import shutil
from pathlib import Path

from api.context import set_session_context, reset_session_context, set_thread_context

from langchain_core.messages import AIMessage
from langgraph.types import Command

# SQLite 持久化检查点：对话历史落盘到 output/checkpoints.sqlite，服务器重启不丢
# 图以 astream() 异步执行，必须用 AsyncSqliteSaver。但它的 __init__ 会调用
# asyncio.get_running_loop()，只能在运行中的事件循环里构造，因此 main_agent
# 改为懒加载：第一次跑任务时（已在 uvicorn 事件循环内）才建连接、建图。
DB_PATH = Path(__file__).parents[1] / "output" / "checkpoints.sqlite"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_main_agent = None
_agent_lock = asyncio.Lock()

# ---- 人工审批（HITL）----
# 审批锚点：generate_markdown（生成报告的唯一入口）。主智能体调它前必被 HumanInTheLoopMiddleware 拦截。
# interrupt_on 的 description 支持动态 callable：(tool_call, state, runtime) -> str
def _hitl_description(tool_call, state, runtime):
    """生成审批请求的中文描述：即将生成的报告文件名 + 字数。"""
    args = tool_call.get("args", {}) or {}
    filename = args.get("filename", "(未命名)")
    content = args.get("content", "") or ""
    return f"即将生成报告「{filename}」（{len(content)} 字）。请审批是否允许落盘。"

# 每个等待审批的 thread 对应一个 asyncio.Future（由 /api/approve set_result）
_approval_futures: dict[str, asyncio.Future] = {}
APPROVAL_TIMEOUT = 300  # 秒：人类 5 分钟不审批 → 自动取消任务


async def wait_for_approval(thread_id: str) -> dict:
    """等待 /api/approve 的人类决策（超时自动取消并清理）。"""
    fut = asyncio.get_running_loop().create_future()
    _approval_futures[thread_id] = fut
    try:
        return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
    finally:
        _approval_futures.pop(thread_id, None)


def resolve_approval(thread_id: str, decision: dict) -> bool:
    """/api/approve 调用：set_result 唤醒等待中的 run_deep_agent。返回是否找到。"""
    fut = _approval_futures.get(thread_id)
    if fut is None or fut.done():
        return False
    fut.set_result(decision)
    return True


async def get_main_agent():
    """懒构建主智能体（AsyncSqliteSaver 需要 running loop，故推迟到异步上下文）。

    恒带 HITL middleware：generate_markdown 前必中断。中断后如何处理由
    run_deep_agent 决定——auto_approve=True 自动批准（等价旧行为），
    auto_approve=False 等待人类审批（前端「报告审批」开关）。
    """
    global _main_agent
    if _main_agent is None:
        async with _agent_lock:
            if _main_agent is None:
                conn = await aiosqlite.connect(str(DB_PATH))
                _main_agent = create_deep_agent(
                    model=model,
                    system_prompt=main_agent_content['system_prompt'],
                    tools=[generate_markdown, convert_md_to_pdf, generate_docx, read_file_content],
                    checkpointer=AsyncSqliteSaver(conn),
                    interrupt_on={
                        "generate_markdown": {
                            "allowed_decisions": ["approve", "edit", "reject"],
                            "description": _hitl_description,
                        }
                    },
                    subagents=[
                        database_query_agent,
                        network_search_agent,
                        knowledge_base_agent,
                        physics_lit_agent
                    ]
                )
    return _main_agent

# 执行
"""
  1. 执行主智能体 一定选异步，原因：对应多个客户端
  2. 什么时候触发我们智能体的调用或者执行？？？
  3. 客户端 -》 api/task -> fastapi 接口 -》 异步执行 -》 main_agent的运行 （异步方法）
  4. main_agent执行stream流式处理 -》 调用工具 -》 已经埋好了点  
                                   调用子智能体 -》 结果解析 -》 name = task -> monitor -> 发送子智能体
                                   调用最终结果 -》 结果 -》 monitor -> 发送结果的方法
                                   开启调用以后 -》 当前会话 -》 文件夹地址 -》 推送到前端
"""



project_root_path = Path(__file__).parents[1].resolve() # 绝对 解析路径标识以及软连接
# project_root_path = Path(__file__).parents[1].absolute() # 绝对
# main_agent.invoke()
# main_agent.stream()
# main_agent.astream() [选他]
async def run_deep_agent(task_query, session_id, deep_research=False, auto_approve=True):
    """
    定义流式+异步执行主智能体！！
    执行过程中，返回  会话文件化返回  调用子智能体  调用最终结果 （monitor）
    task_query: 前端提问的问题
    session_id: 每个前端会话对应的标识 （1.存储session_id ContextVars 2.session_id 给他创建对应的output输出地址）
    deep_research: 前端「深度调研」开关。True=主智能体把网络搜索委托为完整深度检索循环；False=浅层单轮（省钱）
    auto_approve: True=生成报告前自动批准（评测/未开审批开关）；False=等前端「报告审批」（/api/approve 唤醒）
    """
    print(f"当前会话的main_agent开始执行了！ 会话id:{session_id}")
    # 准备工作 【1. session_dir（前端） 2. relative_session_dir (大模型) 3. 上传的文件拼接上传文件专属提示词】
    # project_root_path / output / session_{YYYYMMDD_HHMM}_{thread前6位}
    # 目录命名规范：任务开始前无法预知内容，用"时间戳 + thread 短 id"区分新旧与归属。
    #   同一 thread 续接时短 id 不变 → 目录可复用；时间戳让 output/ 一眼看出哪些是旧的、可清理。
    session_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    session_short = str(session_id)[:6]
    session_dir = project_root_path / "output" / f"session_{session_ts}_{session_short}"
    # 文件夹可能没有，第一次请求要创建
    session_dir.mkdir(parents=True, exist_ok=True)
    # \  \n \t -> /
    session_dir_str = str(session_dir).replace("\\","/")
    # 获取相对文件夹
    # session_dir : project_root_path / output / session_session_id(uuid)
    # project_root_path : project_root_path
    # relative_session_dir_str: / output / session_时间戳_短id
    relative_session_dir_str = str(session_dir.relative_to(project_root_path)).replace("\\","/")

    #处理上传文件 （updated / session_session_id）
    updated_dir_path = project_root_path / "updated" / f"session_{session_id}"
    updated_info_prompt = "" # 有上传文件，拼接上传文件专属解析位置的提示词
    if updated_dir_path.exists():
        # 有
        files = [ f.name  for f in updated_dir_path.iterdir()  if f.is_file()]
        # 将上传文件统一赋值到 output_dir 方便前端统一读取 session_dir
        if files:
            for filename in files:
                # 将原文件 -》 复制 -》 目标文件中  （copy2 保留原文件修改时间和权限等元数据）
                shutil.copy2(updated_dir_path / filename, session_dir / filename)
            # 构建提示词！告诉大模型，有上传文件，你要读取上传文件！！
            updated_info_prompt = (f"\n    [已上传文件] 已加载到工作目录:\n" +
                             "\n".join([f"    - {f}" for f in files]) +
                             "\n    请优先使用工具（read_file_content）读取并参考这些文件。")

    # 继续准备 1. 当前会话的对应的session_id session_dir 存储到contextVars [后续工具获取，socket -> 推送消息] 2.调用monitor给前端推送session_dir信息
    session_dir_token = set_session_context(session_dir_str)  # 存储的当前会话对应的文件夹地址
    session_id_token = set_thread_context(session_id)  #获取当前会话的session_id对应socket

    monitor.report_session_dir(session_dir_str)  # 当前会话对应的文件夹地址推送给起前端！

    # 执行main_agent
    config = {
        "configurable":{
            "thread_id":session_id
        }
    }

    # 构建提示词
    path_instruction = f"""
    【工作环境指令】
    工作目录: {relative_session_dir_str}
    {updated_info_prompt}

    规则：
    1. 新生成文件必须保存到工作目录：'{relative_session_dir_str}/filename'
    2. 读取已上传的文件时，请直接将文件名（例如：'开篇.txt'）作为 filename 参数传入（read_file_content）读取工具，不要带上任何目录前缀。
    3. 使用相对路径，禁止使用绝对路径
    4. 若存在上传文件，请先分析内容
    """

    # 前端「深度调研」开关 → 告诉主智能体本次委托深度：True 走完整深度检索循环，False 浅层单轮
    mode_instruction = (
        "\n    【任务模式】深度调研开关：已开启。凡委托网络搜索类任务，必须走完整深度检索循环"
        "（多角度检索 → 精读网页 → 缺口分析 → 定向补检索 → 带来源汇总）。"
        if deep_research else
        "\n    【任务模式】深度调研开关：关闭。网络搜索类任务保持浅层单轮检索即可，"
        "信息足够立即汇总，不精读、不追轮，控制 API 消耗。"
    )

    # 反馈结果
    try:
        # 执行（懒构建 main_agent：AsyncSqliteSaver 需在事件循环内构造）
        agent = await get_main_agent()

        # 首轮输入：用户消息（后续轮是 Command(resume=审批决策)）
        graph_input = {
            "messages":[
                {
                    "role":"user","content":task_query + path_instruction + mode_instruction
                }
            ]
        }

        # 多段执行：generate_markdown 前会中断（HITL），中断后等审批再 resume，直到正常结束
        # stream_mode=["updates","messages"]：updates 是节点级（子智能体调度/HITL/最终结果），
        # messages 是 token 级（主智能体最终回答的逐字输出，供前端流式显示）
        while True:
            interrupted = None  # 本轮是否发生审批中断
            reasoning_sent = False  # 每个 model 输出只发一次完整思考，避免重复
            async for mode, payload in agent.astream(
                graph_input, config=config, stream_mode=["updates", "messages"]
            ):
                if mode == "messages":
                    # token 级：主智能体（model 节点）的思考 + 正文分开透出
                    chunk, meta = payload
                    if meta.get("langgraph_node") != "model":
                        continue
                    reasoning = chunk.additional_kwargs.get("reasoning_content")
                    if reasoning and not reasoning_sent:
                        monitor.report_stream_reasoning(reasoning)
                        reasoning_sent = True
                    if chunk.content and not getattr(chunk, "tool_calls", None):
                        monitor.report_stream_chunk(chunk.content)
                    continue

                # updates 级：节点状态（含 __interrupt__ / 子智能体调度 / 最终结果）
                for node_name, state in payload.items():
                    if node_name == "__interrupt__":
                        # HITL 中断：state[0].value 是 HITLRequest（含待审工具调用的 name/args/description）
                        interrupted = state[0].value
                        break
                    if not state or "messages" not in state: continue
                    messages = state["messages"]
                    if messages and isinstance(messages,list):
                        last_msg = messages[-1]
                        if node_name == 'model':
                            if last_msg.tool_calls:
                                # 工具和子智能体
                                for tool_call in last_msg.tool_calls:
                                    """
                                      tool_call = {
                                          name: task
                                          args:{
                                              subagent_type:子智能体的名字
                                              description:子智能体的描述
                                          }
                                      }
                                    """
                                    if tool_call['name'] == 'task':
                                        # 调用某个子智能体
                                        # 容错：模型偶发缺 subagent_type/description（2026-08-17 评测见过
                                        # 'subagent_type' KeyError），缺键时跳过埋点，不让流式解析崩掉。
                                        args = tool_call.get('args') or {}
                                        stype = args.get('subagent_type')
                                        desc = args.get('description', '')
                                        if stype:
                                            monitor.report_assistant(stype, {'description': desc})
                            elif last_msg.content:
                                # 最终结果
                                print(f"主智能体执行结果，最终结果：{last_msg.content[:100]}")
                                monitor.report_task_result(last_msg.content)

            if interrupted is None:
                break  # 正常结束，无审批中断

            # ---- 生成报告前审批 ----
            if auto_approve:
                # 自动批准（评测 / 前端开关关闭）：行为等价旧版，报告直接落盘
                decision = {"decisions": [{"type": "approve"}]}
            else:
                # 等人类审批：把待审信息推给前端，/api/approve 唤醒
                requests = interrupted.get("action_requests", []) if isinstance(interrupted, dict) else []
                monitor._emit(
                    "approval_required",
                    "等待人工审批报告",
                    {"thread_id": session_id, "requests": requests},
                )
                print(f"[HITL] 等待审批 thread={session_id}")
                decision = await wait_for_approval(session_id)  # 超时抛 TimeoutError → 走 except

            # resume：同一 config（thread_id）继续执行，决策进入 interrupted 的分支
            graph_input = Command(resume=decision)

    except Exception as e :
        # 报错推送错误信息给前端
        monitor._emit("error",f"执行主智能发生异常信息：{str(e)}")
    finally:
        # 释放存储的地址和session_id
        reset_session_context(session_dir_token, session_id_token)

