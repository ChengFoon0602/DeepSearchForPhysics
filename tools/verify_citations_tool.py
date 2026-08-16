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
# ============================================================================
import re
import time
from typing import Annotated

import httpx

from langchain_core.tools import tool
from api.monitor import monitor


def _probe(client: httpx.Client, url: str):
    """探活单个 URL，返回 (status_code, snippet)。

    snippet 是 GET 到的正文头部（≤4KB），用于关键词命中；探活失败返回 (None, "")。
    重试 3 次、1s/2s 退避（复用 reasoning_model 的中转站重试模板）。
    """
    # 第一志：HEAD 探活（轻量，不做内容命中）
    for attempt in range(3):
        try:
            r = client.head(url)
            if r.status_code in (405, 403, 501):  # HEAD 不被该站点支持 → 降级
                break
            return r.status_code, ""
        except httpx.HTTPError:
            if attempt < 2:
                time.sleep(1 + attempt)
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


@tool
def verify_citations(
        urls: Annotated[str, "要校验的引用 URL 列表，多个用英文逗号、中文逗号或换行分隔"],
        keywords: Annotated[str, "每个 URL 应包含的关键词，与 urls 同序对应、逗号分隔；可空字符串表示只做存活校验"] = ""
):
    """校验引用的 URL 是否真实存在（存活）且正文命中所声称的关键词。

    用于报告收尾前的来源把关：对每个 URL 返回一行状态——
      ✅ 存活·内容命中（页面存在，且正文包含关键词）
      ⚠️ 存活·内容未命中（页面存在，但正文没找到关键词）
      ❌ 不可访问（连接失败 / 超时 / 4xx-5xx / URL 无效）
    模型应把结果按真实状态写进报告的来源清单，不得无视校验结果继续引用。
    """
    monitor.report_tool("引用校验工具", {"urls": urls, "keywords": keywords})

    url_list = [u.strip() for u in re.split(r"[,，\n]", urls) if u.strip()]
    kw_list = [k.strip() for k in re.split(r"[,，]", keywords) if k.strip()] if keywords else []
    if not url_list:
        return "错误：未提供有效的 URL 列表。"

    lines = []
    with httpx.Client(follow_redirects=True, timeout=5) as client:
        for i, url in enumerate(url_list):
            kw = kw_list[i] if i < len(kw_list) else ""
            try:
                status, snippet = _probe(client, url)
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