# 定义一个网络搜索的工具！
# ======================== 导入核心依赖 ========================
# 类型注解：增强代码提示和静态检查能力
from typing import Annotated, Literal
# LangChain 工具装饰器：将普通函数转为 Agent 可调用的工具
from langchain_core.tools import tool
# Tavily 官方客户端：实现网络搜索核心功能
from tavily import TavilyClient

# 系统/第三方依赖
import os  # 系统路径/环境变量处理
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dotenv import load_dotenv  # 加载 .env 文件中的环境变量

# 自定义模块：工具调用埋点监控（需确保 api 模块可导入）
from api.monitor import monitor

# ======================== 初始化配置 ========================
# 加载项目根目录的 .env 文件，读取环境变量（如 TAVILY_API_KEY）
load_dotenv()


# 步骤1： 定义一个TavilyClient对象
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

# 网络请求硬超时（秒）：Tavily SDK 的 search() 是同步阻塞且默认无超时，
# 在 LangGraph 事件循环里调用会占死 to_thread 线程 → 评测/agent 卡死（2026-08-17）
_SEARCH_TIMEOUT = 15


# 步骤2： 定义一个网络搜索工具
@tool
def internet_search(
        query: str,
        topic: Literal[ "news",  "finance",  "general"] = "general",
        max_results: int = 5,
        include_raw_content: bool = False
):
    """
    根据用户问题，进行网络信息收！ 
    注意：主要搜索公开的网络信息！如果指定查询数据库或者rag不能使用此工具！
    :param query: 用户的查询信息
    :param topic: 查询的类型
    :param max_results: 返回的最大条数 
    :param include_raw_content: 是否返回原内容 False 精简 True 详细
    :return: 
    """
    # 每次调用工具，都都会向前端推进调用进度！
    # 参数1： 工具的名字  参数2： 就是调用工具的参数信息
    monitor.report_tool(tool_name="网络搜索工具",
                        args={"query": query, "topic": topic, "max_results": max_results,
                              "include_raw_content": include_raw_content})

    # Tavily SDK search() 是同步阻塞，包线程 + 硬超时 _SEARCH_TIMEOUT 秒：
    # 防止网络黑洞把它占死线程（LangGraph to_thread 卡死评测的根因，见 verify_citations）。
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(
            tavily_client.search, query=query, topic=topic,
            max_results=max_results, include_raw_content=include_raw_content)
        try:
            return fut.result(timeout=_SEARCH_TIMEOUT)
        except FutureTimeout:
            return {"results": [], "timeout": True,
                    "warning": f"网络搜索超时（>{_SEARCH_TIMEOUT}s），已空返回"}


# 步骤3： 定义一个网页内容精读工具（深度检索循环的"精读"环节）
@tool
def extract_web_content(
        urls: Annotated[str, "要精读的网页 URL 列表：多个用英文逗号或换行分隔，最多 5 个"],
        query: Annotated[str, "精读时重点关注的问题，可为空字符串"] = "",
        chunks_per_source: Annotated[int, "每个网页返回的片段数，默认 3"] = 3
):
    """
    精读指定网页的正文内容（markdown），用于深入阅读搜索结果中的权威页面。
    与 internet_search（只返回标题+摘要+URL）不同，此工具返回页面正文，适合追一手资料。
    :param urls: 要精读的网页 URL 列表，逗号或换行分隔，最多 5 个
    :param query: 精读时重点关注的问题（可空），帮助 Tavily 返回更相关片段
    :param chunks_per_source: 每个网页返回的片段数，1-5
    :return: 各网页的 markdown 正文（超长自动截断）
    """
    monitor.report_tool(tool_name="网页内容精读工具",
                        args={"urls": urls, "query": query, "chunks_per_source": chunks_per_source})

    url_list = [u.strip() for u in urls.replace("\n", ",").split(",") if u.strip()][:5]
    if not url_list:
        return "错误：未提供有效的 URL 列表。"

    # Tavily SDK 的 extract() 是同步阻塞、timeout=30 不保证生效——包线程 + 硬超时，
    # 防止黑洞 URL 占死线程（LangGraph to_thread 卡死评测的根因，同 internet_search）。
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(
                tavily_client.extract,
                urls=url_list,
                query=query or None,
                chunks_per_source=max(1, min(chunks_per_source, 5)),
                extract_depth="advanced",
                format="markdown",
                timeout=30,
            )
            try:
                resp = fut.result(timeout=30)
            except FutureTimeout:
                return f"[超时] 网页内容精读超过 30s，已放弃（URLs: {', '.join(url_list)}）"
    except Exception as e:
        return f"网页内容精读失败：{e}"

    parts = []
    for r in resp.get("results", []):
        content = r.get("raw_content") or r.get("content") or ""
        parts.append(f"--- {r.get('url', '')} ---\n{content}")

    body = "\n\n".join(parts)
    if len(body) > 15000:
        body = body[:15000] + "\n...[内容过长已截断]"

    failed = len(resp.get("failed_results", []))
    if failed:
        body += f"\n[注意] {failed} 个 URL 提取失败。"
    return body or "未提取到内容。"














