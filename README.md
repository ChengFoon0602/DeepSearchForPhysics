<p align="center">
  <h1 align="center">🤖 Deep Search For Physics</h1>
  <p align="center"><b>物理教研多智能体协作系统 —— 网络搜索（深度调研）+ 本地 RAG + 公式验证 + 文档生成</b></p>
  <p align="center">
    <img src="https://img.shields.io/badge/Python-3.12+-blue.svg" alt="Python">
    <img src="https://img.shields.io/badge/FastAPI-0.129.2-green.svg" alt="FastAPI">
    <img src="https://img.shields.io/badge/LangChain-1.2.10-orange.svg" alt="LangChain">
    <img src="https://img.shields.io/badge/deepagents-0.4.3-purple.svg" alt="deepagents">
    <img src="https://img.shields.io/badge/Chroma-本地向量库-brightgreen.svg" alt="Chroma">
    <img src="https://img.shields.io/badge/评测-回归测试-yellow.svg" alt="eval">
  </p>
</p>

---

## 🎯 这个项目是什么

基于 [waseens/Deep Search Pro](https://github.com/waseens/deep-search-pro)（MIT）扩展的多智能体协作系统，定位**物理教研**场景：主智能体像团队负责人一样调度多个专家子智能体，完成网络调研、本地讲义检索、公式验证，最终生成带来源的 Markdown 报告。

**在本体的基础上新增了这些能力：**

| 新增能力 | 说明 |
|---------|------|
| **本地 RAG 知识库** | 替换"注册但不可用"的 RAGFlow —— 纯 pip 的 Chroma + fastembed 管线，**无需部署任何服务端**，离线可用 |
| **深度搜索循环** | 网络搜索升级为"多角度检索 → 精读网页 → 缺口分析 → 定向补检索 → 带来源汇总"的迭代循环 |
| **前端「深度调研」开关** | 像市面 AI 产品一样，一个开关决定浅层单轮（省）/ 完整深度循环（费） |
| **公式分隔符确定性归一化** | 生成文件落盘前自动把 `$$...$$`/`\(...\)`/`\[...\]` 统一成 `$...$`——不赌模型守 prompt，任何渲染器都能显示公式 |
| **Agent 回归评测** | `eval/` 把历史 bug 固化成断言（子智能体调度 + 文件分隔符），每次改 prompt 跑一遍防回归 |
| **对话持久化** | AsyncSqliteSaver：同一线程的对话跨重启不丢 |

---

## 🧠 你能从这个项目中学到什么

| 知识点 | 体现的位置 |
|--------|-----------|
| **多智能体编排（orchestrator）** | `agent/main_agent.py` — 主智能体按需求调度 4 个专家子智能体，每个子智能体 = 一个 `{name/description/system_prompt/tools}` 字典 |
| **事件循环约束与懒加载** | `agent/main_agent.py` — `AsyncSqliteSaver` 只能在运行中的事件循环构造，倒逼把"模块级构建图"改成"首次任务时懒加载 + `asyncio.Lock` 防并发双建" |
| **协程级并发隔离** | `api/context.py` — FastAPI 多请求共享线程，用 `ContextVar` 而非全局变量，避免用户数据串台 |
| **跨循环异步通信** | `api/monitor.py` — WebSocket 推送判断"同一事件循环用 `create_task`、跨循环用 `run_coroutine_threadsafe`" |
| **Agent 行为评测（概率系统测试）** | `eval/run_eval.py` — monkeypatch monitor 单例记录真实调用、**子集语义**断言（框架自动加 general-purpose）、失败重试一次兜底模型非确定性 |
| **prompt 概率性服从 vs 确定性兜底** | `tools/markdown_tools.py` — prompt 约束"尽量"，工具层归一化"一定"（公式分隔符落盘前强制统一） |
| **深度检索循环（Deep Research 工程化）** | `tools/tavily_tool.py` + `prompt/prompts.yml` — 多角度检索→精读→缺口分析→定向补检索→带来源汇总，带预算上限 |
| **RAG 全链路设计** | `ingest_kb.py` + `tools/rag_tools.py` — 切分(500/80)→嵌入(bge-small-zh)→Chroma(cosine)→top-k 检索，幂等重建 |
| **检查点持久化** | AsyncSqliteSaver — 对话跨重启不丢，也是未来 human-in-the-loop 中断续跑的基础 |
| **WebSocket 实时监控链路** | `api/monitor.py` — 工具埋点→单例→事件循环归属判断→定向推送，前端流水线随真实调度点亮 |

---

## 🏗️ 架构一览

```
用户请求 (POST /api/task) + 深度调研开关
    │
    ▼
api/server.py          ← FastAPI 路由 + WebSocket，后台 asyncio 执行
    │
    ▼
agent/main_agent.py    ← 主智能体：懒加载构建、异步流式执行、调度子智能体
    │
    ├──→ 子智能体 1: 网络搜索        (tools/tavily_tool.py)  搜索→精读→补检索
    ├──→ 子智能体 2: 知识库检索      (tools/rag_tools.py)   本地 Chroma，无服务端
    ├──→ 子智能体 3: 物理文献·公式验证 (tools/tavily_tool.py)  出处溯源 + 5 步验证
    └──→ 子智能体 4: 数据库查询      (tools/db_tools.py)   注册但未配 MySQL，勿用
    │
    └──→ 主智能体自己调: 生成 Markdown（公式归一化）→ 转 PDF
    │
    ▼ (每个步骤都通过 WebSocket 实时推送)
api/monitor.py         ← 埋点监控 → 前端流水线实时点亮
```

**数据流说明**

1. 用户通过 `POST /api/task` 发请求，可携带 `deep_research` 开关
2. 主智能体分析需求，决定调用哪些子智能体（并在委托里标注开关状态）
3. 网络搜索助手按模式执行：**浅层**（1~2 次搜索即收）或**深度**（多角度→精读→补检索→带来源汇总）
4. 知识库助手查本地 Chroma 向量库（讲义/笔记/学习日记）
5. 主智能体整合，生成 Markdown 报告——落盘前自动归一化公式分隔符
6. WebSocket 实时推进度到前端，前端流水线节点随真实调度点亮

---

## 🔬 设计决策与工程难点

这些不是"能用"就行的地方——每个都踩过坑、有取舍、有方法论。

### 1. 框架约束倒逼架构：事件循环与懒加载

`AsyncSqliteSaver`（langgraph 检查点持久化）的 `__init__` 会调用 `asyncio.get_running_loop()`，**只能在运行中的事件循环里构造**。项目原本在模块级构建图，一跑就报 `RuntimeError: no running event loop`。

解法不是硬凑，而是顺着约束改架构：图改成**懒加载**——第一次跑任务时（已在 uvicorn 事件循环内）才建 SQLite 连接、建图，`asyncio.Lock` 防并发双建。框架的"设计约束"逼出了更正确的结构，而不是 bug。

### 2. 并发隔离：ContextVar 而不是全局变量

FastAPI 下多个请求跑在**同一个线程的不同协程**里。用全局变量，用户 A 的数据会被用户 B 覆盖。`ContextVar` 是 asyncio 协程级变量，每个请求链路互不干扰——`api/context.py` 存了当前会话的 session_dir 和 thread_id，工具和 WebSocket 各取所需。

### 3. 跨循环异步通信：create_task vs run_coroutine_threadsafe

工具可能在**不同的事件循环/线程**里调用 monitor（后台任务 vs FastAPI 主循环）。`api/monitor.py` 判断：同一循环 → `loop.create_task()` 直接推；跨循环 → `asyncio.run_coroutine_threadsafe()` 线程安全地投递，否则会报"协程在错误的循环中运行"。

### 4. 概率系统怎么测：Agent 回归评测

LLM 行为是**概率性**的，传统单元测试断言不了。`eval/` 的做法：
- monkeypatch `api.monitor` 单例的记录方法，**零生产代码改动**，跑的就是真实 `run_deep_agent` 路径；
- 断言用**子集语义**（`create_deep_agent` 会自动加 `general-purpose` 子智能体，精确集合必然 flaky）；
- 失败重试一次兜底模型随机性；
- 这个评测**立竿见影**——深度搜索改造后立刻抓到一个回归：模型又写了 `$$...$$`（学习日记问题 19）。

### 5. prompt 是概率的，兜底要确定性

"严禁使用 `$$...$$`"写进 prompt，模型还是会有概率违反（实测改造后 13 处独立公式全用了 `$$`）。正解是**分层**：prompt 管"尽量"，工具层管"一定"——`generate_markdown` 落盘前把 `$$...$$`/`\(...\)`/`\[...\]` 确定性归一化成 `$...$`，任何渲染器都能显示公式。

### 6. 深度调研的成本控制：两段式 + 前端开关

深度检索循环很烧钱（一次可能 8 次搜索 + 5 次精读）。设计成**模式A（深度）/ 模式B（浅层）两段式 prompt**，前端一个开关透传到 `run_deep_agent`，再经主智能体委托标注传给网络搜索助手。诚实的取舍：这是 prompt 层的强约束而非硬编码上限——若要做确定性省钱需在工具层禁精读（留作进阶项，README 写清楚边界）。

### 7. 信息获取的工程化：把"Deep Research"变成循环

不是"一次搜索返回"，而是把调研流程固化成循环：**广度检索（多角度）→ 精读权威页（extract）→ 缺口分析 → 定向补检索 → 带来源汇总**。带预算上限（搜索 ≤ 8、精读 ≤ 5），信息足够必须停、不为凑轮数而检索。

> 全部踩坑与排查方法论（21 个问题）见 `学习日记.md`——记录"怎么发现、怎么定位、怎么修"，可迁移的经验都在那里。

---

## 🚀 5 分钟跑起来

### 环境要求

- Python 3.12+（Windows 上建议用 `python -m venv`）
- 一个 OpenAI 兼容的 LLM API Key（DeepSeek / 阿里云百炼 / OpenAI 均可）
- Tavily API Key（[免费额度注册](https://tavily.com)）
- 首次跑知识库需要一次网络（下载嵌入模型 ~90MB），之后完全离线

### 第一步：克隆 + 装依赖

```bash
git clone https://github.com/ChengFoon0602/DeepSearchForPhysics.git
cd DeepSearchForPhysics
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 第二步：配环境变量

```bash
cp .env.example .env
```

编辑 `.env`，必填 4 项：

```env
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_QWEN_MAX=deepseek-chat        # 模型名写在这里
TAVILY_API_KEY=tvly-xxxxxxxxxxxx
```

> 📌 数据库（MySQL）的 key **不要填**——服务未配置，主智能体不会被引导去调它。

### 第三步：知识库入库（可选，但推荐）

```bash
python ingest_kb.py    # 把 kb/docs/ 下的文档切分→嵌入→写本地 Chroma；重跑=清空重建（幂等）
```

> 首次运行会从 HuggingFace 下载 `bge-small-zh` 模型 ~90MB，需一次网络。下载失败可设 `HF_ENDPOINT=https://hf-mirror.com` 换镜像。

### 第四步：启动

```bash
python -m api.server    # ★ 必须用 -m，不要用 python api/server.py（sys.path 问题）
```

访问 `http://localhost:8000/` 打开前端聊天页，`/docs` 看 Swagger。

### 第五步：试一试

- 输入框旁有 **「深度调研」开关**：关=浅层单轮（省），开=完整深度检索循环（费，适合综述/调研）
- 问「验证复摆周期公式 $T=2\pi\sqrt{I/(mgd)}$ 并生成 Markdown 报告」→ 网络搜索 + 知识库检索 + 物理文献验证三路并行，报告落盘可下载
- 问「我们项目之前踩过哪些坑」→ 从本地知识库（学习日记）检索出答案

---

## 🧪 Agent 回归评测

历史 bug（跳过网络搜索、`$$...$$` 不渲染）都固化成断言，改 prompt 后跑一遍防回归：

```bash
python -m eval.run_eval              # 全量 3 case
python -m eval.run_eval --case kb_only   # 只跑单个 case
```

- **要求**：先停服务器（评测和 server 共用 `output/checkpoints.sqlite`）
- **原理**：monkeypatch `api.monitor` 单例记录事件 → 走真实 `run_deep_agent` → 断言子智能体调度 + 生成文件无非法分隔符
- **退出码**：0 全过 / 1 有失败 / 2 前置失败或超时
- 语料：`eval/cases.yml`（compound_pendulum 守两个历史 bug、kb_only 守知识库、deep_research 守深度循环）

---

## 📖 推荐阅读顺序

| 顺序 | 文件 | 重点看什么 |
|------|------|-----------|
| 1️⃣ | `agent/llm.py` | 模型初始化，抗上游抖动（max_retries/timeout） |
| 2️⃣ | `prompt/prompts.yml` | 提示词怎么约束 Agent（多源强制、两段式检索、公式规则） |
| 3️⃣ | `agent/subagents/network_search_agent.py` | 子智能体 = 字典配置 |
| 4️⃣ | `tools/tavily_tool.py` | @tool 写法 + 埋点 + 网页精读工具 |
| 5️⃣ | `ingest_kb.py` + `tools/rag_tools.py` | 本地 RAG 完整管线（切分→嵌入→检索） |
| 6️⃣ | `agent/main_agent.py` | **核心**：主智能体创建、调度、流式执行 |
| 7️⃣ | `eval/run_eval.py` | **进阶**：如何对 Agent 系统做回归评测 |
| 8️⃣ | `api/server.py` + `api/monitor.py` | FastAPI + WebSocket 实时推送 |

---

## 📁 项目文件速查

```
deep-search-pro/
│
├── agent/                          # 🤖 智能体层（核心）
│   ├── llm.py                      # 模型初始化（max_retries/timeout 抗抖动）
│   ├── prompts.py                  # YAML 提示词加载
│   ├── main_agent.py               # ★ 主智能体 + 异步执行引擎 + 深度开关透传
│   └── subagents/                  # 子智能体（每个就是一个字典）
│       ├── network_search_agent.py
│       ├── database_query_agent.py
│       ├── knowledge_base_agent.py
│       └── physics_lit_agent.py
│
├── api/                            # 🌐 Web 接口层
│   ├── server.py                   # FastAPI 入口（deep_research 透传）
│   ├── context.py                  # ContextVar 协程隔离
│   └── monitor.py                  # 监控 + WebSocket 连接池
│
├── tools/                          # 🔧 工具函数（7 个 @tool）
│   ├── tavily_tool.py              # 网络搜索 + 网页精读
│   ├── rag_tools.py                # 本地 Chroma 知识库检索（懒加载单例）
│   ├── db_tools.py                 # 数据库查询（注册但未配 MySQL）
│   ├── markdown_tools.py           # 生成 Markdown（公式分隔符归一化）
│   ├── pdf_tools.py                # Markdown → PDF
│   └── upload_file_read_tool.py    # 读取上传文件
│
├── eval/                           # 🧪 回归评测
│   ├── run_eval.py                 # 评测运行器（monkeypatch + 断言）
│   └── cases.yml                   # 语料（3 case）
│
├── ingest_kb.py                    # 知识库入库脚本（幂等：重跑=清空重建）
├── kb/                             # 知识库源文档（人维护）+ 向量库（生成的）
│   ├── docs/                       # 复摆笔记 + 学习日记（demo 场景）
│   └── chroma_db/                  # Chroma 持久化（重建即可，不入库）
├── prompt/prompts.yml              # 提示词配置（唯一的行为开关）
├── rawflow/                        # 原 RAGFlow SDK 参考实现（不再被引用）
├── utils/                          # 路径安全 + Word 转 PDF
├── requirements.txt                # 依赖清单（版本锁定）
├── web/index.html                  # 前端聊天页（含深度调研开关）
└── .env.example                    # 环境变量模板
```

---

## 🔧 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| Agent 框架 | **deepagents** (LangChain 官方) | 多智能体编排 |
| LLM 接入 | LangChain + OpenAI 兼容协议 | 一套代码适配多种模型 |
| Web 框架 | FastAPI + Uvicorn | 异步 HTTP + 原生 WebSocket |
| 搜索引擎 | Tavily API | 搜索 + 网页精读（extract） |
| 向量库 | **Chroma**（本地嵌入式） | 无服务端，API 与 Pinecone/Qdrant 一致 |
| 嵌入模型 | fastembed + BAAI/bge-small-zh-v1.5 | 中文，dim 512，ONNX 离线 |
| 文档生成 | markdown + WeasyPrint/Word COM | MD 生成 + 转 PDF |
| 对话持久化 | AsyncSqliteSaver | 检查点落盘，跨重启不丢 |

---

## ❓ FAQ

### Q: 为什么选 deepagents 而不是自己写编排逻辑？

**A:** 自己写编排要处理状态管理、tool_call 路由、流式输出、错误恢复等一堆事。`deepagents` 把这些都封装好了，你只需要定义子智能体的 name / description / tools，框架帮你调度。对学习来说，先理解"用框架能做什么"，之后再看源码理解"框架怎么做的"。

### Q: 知识库用的是 RAGFlow 吗？

**A:** 不是。项目原本注册了 RAGFlow 但要部署 Docker 服务端才能用。现已替换为**纯 pip 的本地 Chroma 管线**（`chromadb` + `fastembed`），不需要任何服务端进程。数据流：文档 → 切分(500/80) → 嵌入(bge-small-zh) → Chroma(cosine) → top-k 检索。`kb/chroma_db/` 由 `ingest_kb.py` 重建。

### Q: 「深度调研」开关关着会怎样？

**A:** 网络搜索助手走**浅层单轮**（最多 3 次搜索、不精读不追轮）——省 Tavily credit。开启则走**完整深度循环**（多角度检索→精读→缺口→补检索→带来源汇总，搜索 ≤ 8 次）。开关是 prompt 层的强约束，不是硬编码预算上限——若要确定性省钱需在工具层禁精读（留作进阶）。

### Q: 为什么生成的 md 里公式都是 `$...$`？

**A:** LLM 对 prompt 规则是概率性服从，`$$...$$` 在一些渲染器（Typora 等）里显示不了。项目在 `generate_markdown` 落盘前做**确定性归一化**：不管模型怎么写，`$$...$$`/`\(...\)`/`\[...\]` 一律转成 `$...$`。prompt 管"尽量"，代码管"一定"。

### Q: 为什么用 ContextVar 而不是全局变量？

**A:** FastAPI 下多个请求跑在同一个线程的不同协程里。如果用全局变量，用户 A 的数据会被用户 B 覆盖（串台）。ContextVar 是 Python 为 asyncio 设计的协程级变量，每个请求链路互不干扰。

### Q: 数据库查询助手怎么没启用？

**A:** 需要 MySQL 服务端 + 建表。项目目前没配，所以子智能体虽注册但主智能体不会被引导去调它（prompt 里也声明了勿用）。想启用：装 MySQL、建药企 demo 表、填 `.env`。

---

## 📄 License

基于 [waseens/Deep Search Pro](https://github.com/waseens/deep-search-pro)（MIT）扩展，本仓库同样 MIT。可用、可改、可分叉。
