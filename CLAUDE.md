# Deep Search Pro · 项目说明

多智能体协作系统（学习项目）。主智能体调度子智能体，完成「网络搜索（深度检索循环）+ 本地知识库 RAG 检索 + 物理文献与公式验证 + 文档生成」。物理教研场景。带自动化回归评测（`eval/`）。

## 快速启动

```powershell
cd D:\桌面文件\大三\agent\deep-search-pro-master
.venv\Scripts\Activate.ps1
# 若被执行策略拦截，先：
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
# 再激活。

# （首次/换文档后）把 kb/docs/ 里的文档入库到本地 Chroma 向量库：
python ingest_kb.py    # 幂等：重跑 = 清空重建

python -m api.server    # ★ 不要用 README 的 python api/server.py（sys.path bug）
```

- 前端聊天页：http://localhost:8000/
- Swagger 文档：http://localhost:8000/docs
- 依赖已装进项目 `.venv`，不要用系统 Python 跑。

## 配置（.env）

必填 4 项：`OPENAI_BASE_URL`、`OPENAI_API_KEY`、`LLM_QWEN_MAX`（值填模型名，如 `deepseek-chat`）、`TAVILY_API_KEY`。
DB / RAGFlow 的 key **不填**（对应服务未配置，子智能体虽注册但不可用）。

## 架构地图

| 文件 | 作用 |
|------|------|
| `agent/llm.py` | 模型初始化，读 `LLM_QWEN_MAX` |
| `agent/main_agent.py` | 主智能体；**懒加载** `get_main_agent()`（AsyncSqliteSaver 需在事件循环内构建） |
| `agent/subagents/*.py` | 子智能体（字典：name/description/system_prompt/tools） |
| `prompt/prompts.yml` | **所有提示词在这**，改 agent 行为先改这里 |
| `ingest_kb.py` | 知识库入库脚本：`kb/docs/` → 切分 → fastembed 嵌入 → Chroma（重跑=清空重建） |
| `tools/rag_tools.py` | 本地 RAG 工具：`list_knowledge_documents` + `search_knowledge_base`（懒加载单例） |
| `kb/docs/` | 知识库源文档（.md/.txt/.pdf），人维护；改完跑 `python ingest_kb.py` 入库 |
| `kb/chroma_db/` | Chroma 向量库持久化目录（生成的，可删，重新 ingest 即可重建） |
| `api/server.py` | FastAPI + WebSocket + 静态挂载（`web/`） |
| `api/monitor.py` | 工具埋点 → WebSocket 推送 |
| `api/context.py` | ContextVar 协程级数据隔离 |
| `tools/*.py` | 工具（tavily / markdown / pdf / 文件读取） |
| `web/index.html` | 前端聊天页（KaTeX + marked 渲染） |
| `reports/` | **报告留档**：有价值的生成报告归档处（人工拷贝，不进 session） |
| `eval/` | **评测回归**：`run_eval.py` + `cases.yml`（见「评测」节） |
| `output/checkpoints.sqlite` | 对话检查点（SqliteSaver），跨重启持久化 |
| `学习日记.md` | **18 个问题的完整排查记录**，改代码前先看 |

## 评测（回归）

`eval/run_eval.py` 把历史 bug 固化成断言（子智能体调度 + 生成文件无非法分隔符 + 深度检索次数），每次改 prompt 后跑一遍防回归：

```powershell
python -m eval.run_eval              # 全量（3 case）
python -m eval.run_eval --case kb_only   # 只跑单个 case
python -m eval.run_eval --retries 2      # 失败重试 2 次
python -m eval.run_eval --clean          # 清理通过 case 的 session 目录
```

- **要求**：先停服务器（评测和 server 共用 `output/checkpoints.sqlite`），前置检查会拒绝 8000 端口占用。
- **原理**：monkeypatch `api.monitor` 单例记录事件，走真实 `run_deep_agent` 生产路径，零代码改动。
- **退出码**：0 全过 / 1 有失败 / 2 前置失败或超时。超时会终止整个进程（共享 checkpointer 连接可能脏）。
- **语料**：`eval/cases.yml`（compound_pendulum 守"网络搜索不被跳过 + 无 `$$`"、kb_only 守知识库、deep_research 守深度检索循环）。
- 模型行为是概率性的，断言用**子集语义** + 失败重试一次兜底。

## 已知坑（速查）

1. 启动必须 `python -m api.server`。
2. agent 记忆是**线程级**的：同一 `thread_id` 才能续接对话；新线程看不到旧内容（隔离设计）。
3. 改 `prompts.yml` 后必须重启服务器（只在 import 时读一次）。
4. 服务器重启后浏览器要硬刷新（`Ctrl+Shift+R`）。
5. 前端「发送」对空输入静默返回（`if (!q || busy) return`），不是 bug。
6. **知识库检索前必须先 `python ingest_kb.py` 建库**，否则工具返回"知识库为空"提示（这是预期行为，不是 bug）。
7. **首次跑 ingest 会从 HuggingFace 下载 bge-small-zh 模型 ~90MB**，需一次网络（虚拟网卡）；下载后缓存在本地，之后完全离线。下载失败可设 `HF_ENDPOINT=https://hf-mirror.com` 换镜像。
8. 新增/修改 `kb/docs/` 下的文档后，要重跑 ingest 才会生效（重跑=清空重建，幂等）。
9. **重启服务器要连 worker 一起杀**：`python -m api.server` 是 uvicorn reloader+worker 双进程，只 `taskkill` reloader 会留下孤儿 worker 占着 8000 端口、新 prompt 不生效。定位：`Get-CimInstance Win32_Process -Filter "Name='python.exe'"` 看 CommandLine/ParentProcessId，把 worker 一起 `Stop-Process -Force`。
10. **`output/` 是运行时数据**：`session_*` 目录按 `session_{YYYYMMDD_HHMM}_{thread前6位}` 命名（时间戳区分新旧、短 id 关联线程），每个只放本次任务生成的文件，**可整目录删除**；`checkpoints.sqlite` 存旧线程对话记忆（线程级隔离，新会话用不到），会随评测/跑任务无限膨胀到上百 MB——定期删除（先确认没有进程占用，`rm: Device or resource busy` 说明有 eval/server 进程还连着）。要留的报告拷到 `reports/`。
11. **HITL 人工审批**：前端「报告审批」开关开时，生成 Markdown 前图会中断等审批（`POST /api/approve` 唤醒）；关 = 自动批准。审批等待 5 分钟超时自动取消任务。评测走 auto_approve，不等审批。
12. 全部坑 + 排查方法论：见 `学习日记.md`。

## 当前状态

- 启用的子智能体（4 个）：**网络搜索**（深度检索循环：多角度检索→精读→补检索→带来源汇总）、**知识库检索**（本地 Chroma，无需外部服务）、**物理文献与公式验证**（可精读网页）、**数据库查询**（注册但未配 MySQL 服务，勿让主智能体调用它）。
- 深度循环 prompt 在 `prompt/prompts.yml` 的 `sub_agents.tavily.system_prompt`（工具 `internet_search` + `extract_web_content`）。
- 生成文件落盘前会**确定性归一化公式分隔符**（`$$...$$`/`\(...\)`/`\[...\]` → `$...$`，`tools/markdown_tools.py`），不赌模型守 prompt 规则。
- RAG 管线：文档 → 切分(500字/重叠80) → fastembed(bge-small-zh, dim512) → Chroma(cosine) → top-k 检索。
- 前端：聊天 + WebSocket 实时进度 + 报告下载 + 数学公式渲染 + **人工审批**（HITL）。
- 依赖模型：DeepSeek（`deepseek-chat`）+ Tavily。
