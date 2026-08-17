# learn/collab 流水线 · 与 mini_orchestrator 的对照

> 上一个学习产物（`mini_orchestrator`）证明了「子智能体 = 嵌套 invoke 黑盒」的机制；
> 这个产物更进一步——**在裸 LangGraph 里重造一条真正的协作流水线**（search → verify → 汇总），
> 把「黑盒」的边界打开：子智能体之间不再只能透过主 agent 的 description 间接传话，
> 而是能拿到上一个阶段**完整的结果**。同样**不进生产**，是学会之后的练习。

## 机制：图里嵌套图（子图 = CompiledStateGraph）

用 `langchain.agents.create_agent(...).compile()` 编出来的每个子图（`search_subagent` / `verify_subagent`）
都是一个**独立的 Runnable**。主图节点调用它 = 在主图节点函数里 `subgraph.invoke({"messages":[HumanMessage(...)]})`。

关键技术点（坑）：

1. **子图也是图**——它有自己的 `call_model → call_tools → 路由` 循环，跑完把最终
   `result["messages"][-1].content` 拿出来。
2. **阶段间数据依赖**：`run_verify` 节点把 `run_search` 写进 `state["messages"]` 的
   `ToolMessage`（内容 = 上一个子图的最终输出）拼进自己的输入 HumanMessage → 这是「协作」。
3. **OpenAI 协议**：`verify_subagent.invoke()` 时输入里只有 `HumanMessage`；子图内部自会
   补 `SystemMessage`（`build_agent_loop` 里做），不必主图操心。
4. **真实工具**：`internet_search` / `extract_web_content` / `verify_citations` 都是真实
   `@tool`，`monitor.report_tool` 埋点照常触发；跑过一次即证明 pipeline 走的是真链路。

## 和 deepagents 源码对照

| 本 learn/collab | deepagents / langchain 源码 |
|---|---|
| `build_agent_loop`（model/tools/router） | `langchain.agents.create_agent` 图骨架 |
| 每个子图 = `build_agent_loop(...).compile()` | deepagents `subagents.py` 里每个 subagent 调 `create_agent` 得到的 CompiledStateGraph |
| `run_search` 节点内 `search_subagent.invoke(...)` | 主 agent 的 `task` 工具里 `subagent.ainvoke(subagent_state)` |
| `run_verify` 把上一阶段 ToolMessage 拼进输入 | `task` 工具把 `description` 作为子智能体唯一输入（但我们的阶段间传值更直接） |
| `state["messages"]` 累积 ToolMessage | `runtime.state` 经 `_EXCLUDED_STATE_KEYS` 过滤后传给子智能体 |

## 为什么 main 到不了这里（也说明它为什么是 learn）

生产是 hub-and-spoke（主 agent 调度 task），deepagents 的 `task` 工具只传 `description`，
子智能体之间不通信。我们重造的是**一条显式流水线**：把 `search`、`verify` 做成两个阶段节点，
数据沿 `search → verify → summary` 单向流动。这突破了框架默认，但也意味着：
- 生产若引入，搜索和验证会被**强绑定顺序**（复杂度随之上涨）；
- 本项目的真实任务（物理教研）大多是「主 agent 并行调多个子 agent、各自拿结果由主 agent 整合」，
  并不需要这种强顺序流水线。

所以它证明的是**「调用者愿意时，框架黑盒可以打开」**，而不是「生产应该改用它」。

## 跑法

```powershell
python learn/run_collab.py
```

自检断言：两阶段子图依次执行、阶段消息含复摆/周期内容（PASS）。

## 为什么真工具会卡死（踩坑，库在 learn 里）

**同步 httpx / requests 在 LangGraph 事件循环里 = 死锁**。`astream`/`to_thread`
把工具的同步 `func` 丢线程池执行，主事件循环轮询线程结果 —— 同步阻塞不放，
主循环死等（连 `asyncio.wait_for` 超时都触发不了，因为它挂在同一条循环）。
2026-08-17 实测：`verify_citations` 用同步 `httpx.Client(follow_redirects=True)`
对 arxiv 这类站点叠重定向超时，评测卡死 1.5 小时，整个进程杀不掉。

正确姿势（`tools/verify_citations_tool.py` 已修复）：
- **硬超时壳**：每个 URL 探活包进 `ThreadPoolExecutor` + `future.result(timeout=6)`，
  超时判 ❌（线程会被弃但不会阻塞主循环调度）。
- **连接/读/写全部压到 1.5s**：黑洞地址（连不上）在 ~1.5s 失败，而不是顶满默认 5s。
- **工具永不抛异常**：校验失败 = 不可信，返回状态即可，不打断 agent 流程。