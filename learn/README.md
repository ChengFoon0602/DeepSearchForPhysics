# learn/ · 用裸 LangGraph 重造最小多智能体 Orchestrator

> 学习产物：理解 deepagents 的核心机制，用裸 LangGraph 手写一遍。**不替换生产代码**（`agent/`、`api/`、`tools/` 不受影响），这是"从会用变成会造"的练习。

## 跑起来

```powershell
# 项目根目录
python learn/run_demo.py --stage B1   # 最小 agent 循环
python learn/run_demo.py --stage B2   # task 子智能体
python learn/run_demo.py --stage B3   # interrupt/resume 审批
python learn/run_demo.py              # 全跑
```

依赖项目的 `.env`（中转站 key）+ `agent/reasoning_model.py`（deepseek-v4-flash 思考模型 + 自定义流式）。

## 三个 stage 各教什么

### B1 · 最小 agent 循环 —— 对应 `langchain.agents.create_agent`

```
START → model → (有 tool_calls?) → tools → model → ... → END
        ↑_______________________________|
```

- `call_model`：带历史调 LLM，返回 AIMessage
- `call_tools`：遍历 `tool_calls` 执行，返回 ToolMessage
- `should_continue`：路由器——还有工具调用就回 model，否则 END

**这是 agent 的本体**。deepagents 的 `create_agent` 内部就是这套图骨架，只是叠了 middleware 栈。

### B2 · task 子智能体 —— 对应 `SubAgentMiddleware._build_task_tool`

```
主 agent → task 工具调用
             │ subagent.invoke(state)   ← 子智能体是独立 agent，嵌套运行
             ▼
         Command(update={"messages":[ToolMessage(结果)]})  ← 结果注入主图
```

**核心机制**（也是 deepagents 源码里的三点）：
1. 子智能体状态 = `messages=[HumanMessage(description)]`（**上下文隔离**，主图历史不泄漏）
2. `subagent.invoke()` 嵌套运行，对主图是**黑盒**（所以子智能体的 token 不进主图消息流——这也解释了本项目真流式为什么天然只流主智能体）
3. 返回 `Command(update=...)` 把结果以 ToolMessage 注回主图

### B3 · interrupt/resume 审批 —— 对应 `HumanInTheLoopMiddleware` + LangGraph `interrupt`

```
model → (要调 generate_report?) → approval 节点 → interrupt({...}) 挂起
                                          │
                              Command(resume={approve: True})  ← 人类决策
                                          ▼
                               执行 generate_report → ToolMessage → model
```

- `interrupt()` **只能在图节点里调用**（不能放工具里），所以审批是独立节点 + 条件边
- 挂起时状态存进 checkpointer，`Command(resume=...)` 从断点续跑
- **坑**：OpenAI 协议要求"带 tool_calls 的 assistant 消息后必须跟对应每个 tool_call_id 的 tool 消息"——所以审批节点里同批次的其它 tool_call 也要一并执行返回，否则消息序列不完整，下一轮 model 调用会 400

## 和 deepagents 源码对照表

| mini_orchestrator | deepagents / langchain 源码 |
|---|---|
| `build_agent_loop`（model/tools/router） | `langchain.agents.create_agent` 的图骨架 |
| `build_task_tool` 的 `task()` | `SubAgentMiddleware._build_task_tool` 的 `task`/`atask` |
| `build_task_tool` 的 `subagent.invoke(state)` | `_validate_and_prepare_state` + `subagent.ainvoke` |
| 返回 `Command(update={"messages":[ToolMessage]})` | `_return_command_with_state_update` |
| `build_subagent`（复用 agent 循环） | `_get_subagents_legacy` 里 `create_agent(subagent)` |
| `approval_gate` + `interrupt()` | `HumanInTheLoopMiddleware.after_model` 的 `interrupt(hitl_request)` |
| 子智能体上下文隔离（只传 description） | `_EXCLUDED_STATE_KEYS` 过滤 + `messages=[HumanMessage(description)]` |

## 为什么子智能体是黑盒（关键理解）

deepagents 的子智能体**不是主图的 subgraph**——它是 `task` 工具的 `func` 里 `subagent.invoke()` 嵌套运行的独立 agent。对主图的 LangGraph 执行器来说，`task` 只是一个普通工具调用，返回一个 `Command`。所以：

- 子智能体内部的 model/tool 调用**不经过主图的节点流** → 主图 `stream_mode="messages"` 看不到子智能体的 token
- 这正是本项目真流式"天然只流主智能体最终回答"的底层原因
- 子智能体结果只有"最后一条消息"回到主图（作为 ToolMessage），中间过程全丢——**这就是上下文隔离的价值**（省 token、防主线程被过程撑爆）

## 顺带发现并修复的生产 bug

`agent/reasoning_model.py` 的 `ReasoningChatModel` 原来只补 `/v1` 在 `_sse_request`（流式路径）。B 用了非流式 `invoke`（走 openai SDK 的 `_generate`），发现 SDK 用 `root_client.base_url`（不带 `/v1`）→ 请求到根路径拿到 HTML。已在 `__init__` 统一补 `/v1`，流式/非流式都正确。

## 别过度设计

B 是学习产物，代码刻意短、注释讲"为什么"。deepagents 做的事（backend、summarization、patch_tool_calls、skills、memory…）B 一概不碰——那些是"补齐边界情况"，理解了骨架之后，边界情况自然知道往哪加。
