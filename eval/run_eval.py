# Agent 评测运行器：回归断言「子智能体调度」+「生成文件质量」
#
# 用法（项目根目录、venv 激活、服务器已停止）：
#   python -m eval.run_eval                 # 全量跑
#   python -m eval.run_eval --case kb_only  # 只跑指定 case
#   python -m eval.run_eval --retries 2     # 失败重试 2 次（默认 1）
#   python -m eval.run_eval --clean         # 清理通过 case 的 session 目录
#
# 原理：进程内 monkeypatch api.monitor.monitor 单例的实例方法记录全部事件，
#       然后走真实的 run_deep_agent（生产路径，零代码改动）。
#       退出码：0 全过 / 1 有失败 / 2 前置失败或超时。
import argparse
import asyncio
import os
import shutil
import socket
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # 兼容 `python eval/run_eval.py` 直接跑

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# internet_search / extract_web_content 的 monitor 埋点名（tavily_tool.py 里写死的中文标签）
TOOL_LABEL_SEARCH = "网络搜索工具"
TOOL_LABEL_EXTRACT = "网页内容精读工具"
# 外部渲染器（Typora 等）不认的分隔符，生成文件里一律禁止
FORBIDDEN_DELIMITERS = ["$$", r"\(", r"\["]

REQUIRED_ENV = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "LLM_QWEN_MAX", "TAVILY_API_KEY")
KB_DB = ROOT / "kb" / "chroma_db" / "chroma.sqlite3"
PORT = 8000


class Recorder:
    """在 monitor 单例上替换 5 个方法，进程内记录全部事件。

    注意：这些方法是实例属性（普通函数），调用方是 `monitor.report_tool(...)`，
    不会绑定 self——签名必须与原始方法一致（tool_name, args）等。
    """

    _PATCHED = ("report_tool", "report_assistant", "report_task_result",
                "report_session_dir", "_emit")

    def __init__(self):
        self.tools = []        # [(label, args)]
        self.assistants = []   # [子智能体中文名]
        self.results = []      # [最终结果文本]
        self.errors = []       # [_emit("error", msg)]
        self.session_dir = None
        self._monitor = None

    def install(self):
        from api.monitor import monitor
        self._monitor = monitor
        for name in self._PATCHED:
            setattr(monitor, name, getattr(self, "_on_" + name))

    def uninstall(self):
        for name in self._PATCHED:
            try:
                delattr(self._monitor, name)  # 删实例属性 → 回落类方法
            except AttributeError:
                pass

    def _on_report_tool(self, tool_name, args=None):
        self.tools.append((tool_name, args or {}))

    def _on_report_assistant(self, assistant_name, args=None):
        self.assistants.append(assistant_name)

    def _on_report_task_result(self, result):
        self.results.append(result)

    def _on_report_session_dir(self, path):
        self.session_dir = path

    def _on__emit(self, event_type, message, data=None):
        if event_type == "error":
            self.errors.append(str(message))


def load_cases(path):
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["cases"]


def check_preconditions():
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        raise SystemExit(f"[前置失败] 缺少 .env 配置: {missing}")
    if not KB_DB.exists():
        raise SystemExit("[前置失败] 知识库未入库，请先运行: python ingest_kb.py")
    s = socket.socket()
    s.settimeout(0.5)
    try:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            raise SystemExit(f"[前置失败] {PORT} 端口被占用（服务器在跑），请先停止服务器再跑评测")
    finally:
        s.close()


async def run_case(case):
    """跑一次 case，返回 (thread_id, Recorder)。超时视为硬失败并退出整个进程。"""
    from agent.main_agent import run_deep_agent

    thread_id = str(uuid.uuid4())
    rec = Recorder()
    rec.install()
    try:
        # run_deep_agent 内部吞异常走 _emit("error")，不 raise；故超时 = 挂死，
        # 取消可能污染共享 checkpointer 连接 → 直接退出进程，不继续跑其他 case。
        await asyncio.wait_for(
            run_deep_agent(case["query"], thread_id,
                           deep_research=case.get("deep_research", False)),
            timeout=case.get("timeout", 600),
        )
    except asyncio.TimeoutError:
        raise SystemExit(
            f"[超时] case '{case['id']}' 超过 {case.get('timeout', 600)}s，"
            f"可能挂死。已终止整个评测进程（共享 checkpointer 连接可能脏）。"
        )
    finally:
        rec.uninstall()
    return thread_id, rec


def check_case(case, thread_id, rec):
    """对一次运行做断言，返回失败项列表（空 = PASS）。"""
    failures = []

    # 1. 子智能体调度（子集语义：create_deep_agent 会自动加 general-purpose）
    dispatched = set(rec.assistants)
    for name in case.get("expected_subagents", []):
        if name not in dispatched:
            failures.append(f"未调度子智能体: {name}")

    # 2. 生成文件 + 分隔符检查（对所有 .md 都扫，无论 expect_file）
    # 目录名现在是 session_{时间戳}_{thread前6位}，直接用 monitor 上报的 session_dir（真实路径），别重建
    session_dir = Path(rec.session_dir) if rec.session_dir else (ROOT / "output" / f"session_{thread_id}")
    md_files = sorted(session_dir.rglob("*.md")) if session_dir.exists() else []
    if case.get("expect_file", False) and not md_files:
        failures.append("未生成任何 .md 文件")
    for f in md_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for pat in FORBIDDEN_DELIMITERS:
            if pat in text:
                failures.append(f"{f.name} 含非法分隔符 {pat!r}")

    # 3. 深度循环指标（全程聚合计数：并行子智能体无法可靠按 agent 归属）
    labels = [t for t, _ in rec.tools]
    if "min_internet_search" in case and labels.count(TOOL_LABEL_SEARCH) < case["min_internet_search"]:
        failures.append(
            f"internet_search 次数不足: {labels.count(TOOL_LABEL_SEARCH)} < {case['min_internet_search']}"
        )
    if "min_extract" in case and labels.count(TOOL_LABEL_EXTRACT) < case["min_extract"]:
        failures.append(
            f"extract 次数不足: {labels.count(TOOL_LABEL_EXTRACT)} < {case['min_extract']}"
        )

    # 4. 执行期错误（DeepSeek 断连 / 图异常，run_deep_agent 会静默吞掉）
    if rec.errors:
        failures.append("执行期错误: " + rec.errors[0][:200])

    return failures


async def run_suite(cases, retries):
    """在同一个事件循环里跑完全部 case。
    必须共用一个循环：get_main_agent() 只构建一次并缓存在全局，
    AsyncSqliteSaver 连接绑定在首个循环上，换循环会报
    "coroutine attached to a different event loop"。"""
    results = {}  # id -> (passed, failures, attempts, pass_session_dir)
    for case in cases:
        cid = case["id"]
        passed, failures, attempts, pass_session = False, [], 0, None
        for attempt in range(1 + retries):
            attempts += 1
            print(f"\n▶ [{cid}] 第 {attempt + 1} 次尝试（thread 全新）...")
            thread_id, rec = await run_case(case)
            failures = check_case(case, thread_id, rec)
            dispatched = "、".join(dict.fromkeys(rec.assistants)) or "(无)"
            labels = [t for t, _ in rec.tools]
            tool_counts = "、".join(dict.fromkeys(labels)) or "(无)"
            print(f"  调度子智能体: {dispatched}")
            print(f"  调用工具: {tool_counts}")
            if not failures:
                passed, pass_session = True, rec.session_dir
                print("  PASS")
                break
            print(f"  失败项: {'; '.join(failures)}")
        results[cid] = (passed, failures, attempts, pass_session)
    return results


def main():
    parser = argparse.ArgumentParser(description="Deep Search Pro Agent 评测")
    parser.add_argument("--case", help="只跑指定 case id")
    parser.add_argument("--retries", type=int, default=1, help="失败重试次数（默认 1）")
    parser.add_argument("--clean", action="store_true", help="清理通过 case 的 session 目录")
    args = parser.parse_args()

    check_preconditions()

    cases = load_cases(ROOT / "eval" / "cases.yml")
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"[错误] 未找到 case: {args.case}")
            sys.exit(1)

    print("=" * 70)
    print(f"开始评测：{len(cases)} 个 case，每个最多 {1 + args.retries} 次尝试")
    print("=" * 70)

    # 整个评测共用一个事件循环（见 run_suite docstring）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        results = loop.run_until_complete(run_suite(cases, args.retries))
    except SystemExit as e:
        print(f"\n{e}")
        sys.exit(2)
    finally:
        loop.close()

    # 汇总表
    print("\n" + "=" * 70)
    print("结果汇总")
    print("=" * 70)
    all_pass = True
    cleaned = 0
    for cid, (passed, failures, attempts, pass_session) in results.items():
        tag = "PASS" if passed else "FAIL"
        all_pass &= passed
        detail = "、".join(failures) if failures else "OK"
        print(f"  [{tag}] {cid:20s} (尝试 {attempts} 次)  {detail}")
        if args.clean and passed and pass_session:
            dir_ = Path(pass_session)
            if dir_.exists():
                shutil.rmtree(dir_)
                cleaned += 1

    if args.clean:
        print(f"\n[--clean] 已清理 {cleaned} 个通过 case 的 session 目录")
    print("=" * 70)
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
