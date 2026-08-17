# ======================== 引用真实性校验工具 ========================
# 背景：deep research 场景最大的实坑是 LLM 编造来源——报告列了 10 个引用，
#       可能 9 个是假的。prompt 里写"禁编造来源"是概率性约束，这里做确定性兜底：
#       对每个 URL 做「存活探活（HTTP）→ 存活者再查关键词是否命中正文」，
#       框架（"prompt 管尽量、代码管一定"）与 `_normalize_formula_delimiters` 同模式。
#
# 约束：
#   - 探活优先 HEAD（轻量）；HEAD 被拒（405/403 等）降级 GET 只读状态码。
#   - 抓正文只读前 4KB 即断（不下载全文）。
#   - 工具永不抛异常：校验失败 = 不可信，返回对应状态即可，不打断 agent 流程。
#
# 卡死防护（关键）：
#   **同步 httpx 在 LangGraph 事件循环里会阻塞线程池——导致评测/agent 卡死**。
#   2026-08-17 实测：verify_citations 进 eval 链路时用同步 httpx.Client，对
#   arxiv 等重定向站点 follow_redirects 会把 5s 超时叠成几十秒，线程被占死不还，
#   主事件循环死等 → run_eval 卡住 1.5h。
#   对策：每个 URL 的探活包进 ThreadPoolExecutor，future.result(timeout=6) 硬超时。
#   即使 to_thread 线程被占死，也最多等 6s 就判定 ❌，绝不阻塞主循环调度。
# ============================================================================
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Annotated

import httpx

from langchain_core.tools import tool
from api.monitor import monitor

# 单个 URL 探活硬超时（秒）：超过直接判 ❌，不等待
_PROBE_TIMEOUT = 6


def _probe_http(client: httpx.Client, url: str):
    """单次探活（同步），返回 (status_code, snippet)。**必须在硬超时壳(ThreadPoolExecutor)里调用。**
    不做内部重试/退避——重试由硬超时壳承担（超时=不可访问），避免同步阻塞叠加。
    """
    # 第一志：HEAD 探活（轻量，不做内容命中）
    try:
        r = client.head(url)
        if r.status_code in (405, 403, 501):  # HEAD 不被该站点支持 → 降级 GET
            pass
        else:
            return r.status_code, ""
    except httpx.HTTPError:
        pass  # 走 GET 兜底
    # 第二志：GET 只读状态码 + 正文头部（关键词命中源）
    try:
        with client.stream("GET", url) as r:
            chunk = b""
            for part in r.iter_bytes():
                chunk += part
                if len(chunk) >= 4096:
                    break
            return r.status_code, chunk.decode("utf-8", errors="replace")[:4096]
    except httpx.HTTPError:
        return None, ""


def _probe_with_timeout(client: httpx.Client, url: str):
    """把 _probe_http 包进线程，硬超时 _PROBE_TIMEOUT 秒：绝不让同步 httpx 占死调用线程。
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_probe_http, client, url)
        try:
            return fut.result(timeout=_PROBE_TIMEOUT)
        except FutureTimeout:
            return None, ""


def _new_client():
    # 连接/读/写都压到 1.5s：黑洞地址（连不上/黑 IP）会在 ~1.5s 内失败，
    # 而不是顶满 httpx 默认 5s——配合外层 6s 硬超时，单 URL 探活控制在 ~3s。
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=1.5, read=1.5, write=1.5, pool=1.5),
    )


@tool
def verify_citations(
        urls: Annotated[str, "要校验的引用 URL 列表，多个用英文逗号、中文逗号或换行分隔"],
        keywords: Annotated[str, "每个 URL 应包含的关键词，与 urls 同序对应、逗号分隔；可空字符串表示只做存活校验"] = ""
):
    """校验引用的 URL 是否真实存在（存活）且正文命中所声称的关键词。

    用于报告收尾前的来源把关：对每个 URL 返回一行状态——
      ✅ 存活·内容命中（页面存在，且正文包含关键词）
      ⚠️ 存活·内容未命中/正文未获取
      ❌ 不可访问（连接失败 / 超时 / 4xx-5xx / URL 无效）
    模型应把结果按真实状态写进报告的来源清单，不得无视校验结果继续引用。
    """
    monitor.report_tool("引用校验工具", {"urls": urls, "keywords": keywords})

    url_list = [u.strip() for u in re.split(r"[,，\n]", urls) if u.strip()]
    kw_list = [k.strip() for k in re.split(r"[,，]", keywords) if k.strip()] if keywords else []
    if not url_list:
        return "错误：未提供有效的 URL 列表。"

    lines = []
    with _new_client() as client:
        for i, url in enumerate(url_list):
            kw = kw_list[i] if i < len(kw_list) else ""
            try:
                status, snippet = _probe_with_timeout(client, url)
            except Exception:
                status, snippet = None, ""
            if status is None:
                lines.append(f"❌ {url} [不可访问：连接失败/超时/无效]")
            elif not (200 <= status < 400):
                lines.append(f"❌ {url} [不可访问：HTTP {status}]")
            elif not kw:
                lines.append(f"✅ {url} [存活]")
            elif not snippet:
                lines.append(f"⚠️ {url} [存活·正文未获取，无法判定内容]")
            elif kw in snippet:
                lines.append(f"✅ {url} [存活·内容命中关键词「{kw}」]")
            else:
                lines.append(f"⚠️ {url} [存活·内容未命中关键词「{kw}」]")

    return "\n".join(lines)