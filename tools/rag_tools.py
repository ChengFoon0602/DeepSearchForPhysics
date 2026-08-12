# 本地 RAG 知识库工具：Chroma 向量库 + fastembed 本地嵌入，无需任何外部服务（无 Docker、无守护进程）
# 数据由 ingest_kb.py 入库到 kb/chroma_db/；此文件只负责「列出文档」和「检索片段」两个工具。
import threading
from pathlib import Path

from langchain_core.tools import tool

from api.monitor import monitor

# ---------- 存储单例（懒加载） ----------
# 与 main_agent 的 get_main_agent() 同理：初始化 Chroma / fastembed 较重（要加载 ONNX 嵌入模型），
# 推迟到第一次真正调用工具时才建立，避免 import 即加载拖慢服务器启动。加锁防并发双建。
_collection = None
_embedder = None
_lock = threading.Lock()


def _get_kb():
    """懒加载 (collection, embedder)。embedder 负责把 query 也嵌入成同一空间的向量。"""
    global _collection, _embedder
    if _collection is None:
        with _lock:
            if _collection is None:
                import chromadb
                from fastembed import TextEmbedding

                db_dir = Path(__file__).parents[1] / "kb" / "chroma_db"
                client = chromadb.PersistentClient(path=str(db_dir))
                _collection = client.get_or_create_collection(
                    "kb_collection", metadata={"hnsw:space": "cosine"}
                )
                _embedder = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5")
    return _collection, _embedder


def _empty_hint() -> str:
    return "知识库为空或未建库。请先在项目根目录运行：python ingest_kb.py"


@tool
def list_knowledge_documents() -> str:
    """
    列出本地知识库中当前已入库的全部文档及各自的片段数量。
    调用此工具可以知道知识库里有哪几篇文档，从而判断能否从中检索到用户需要的信息。
    返回结果：文档名 + 片段数；知识库为空时给出建库提示。
    """
    monitor.report_tool(tool_name="知识库文档列表：list_knowledge_documents")
    try:
        collection, _ = _get_kb()
        if collection.count() == 0:
            return _empty_hint()
        data = collection.get(include=["metadatas"])
        from collections import Counter

        counts = Counter(m.get("source", "未知来源") for m in (data["metadatas"] or []))
        lines = "\n".join(f"- {name}: {n} 个片段" for name, n in sorted(counts.items()))
        return f"本地知识库当前文档（共 {collection.count()} 个片段）：\n{lines}"
    except Exception as e:
        return f"查询知识库文档列表失败：{str(e)}。{_empty_hint()}"


@tool
def search_knowledge_base(query: str, top_k: int = 5) -> str:
    """
    在本地知识库中做向量相似度检索，返回与 query 最相关的文档原始片段。
    :param query: 检索问题或关键词（越贴近库中措辞，命中越好）
    :param top_k: 返回片段数量，默认 5
    :return: 命中的原始片段 + 来源文档 + 相似度，不做概括性总结，交由调用方整合
    """
    monitor.report_tool(
        tool_name="知识库检索工具：search_knowledge_base",
        args={"query": query, "top_k": top_k},
    )
    try:
        collection, embedder = _get_kb()
        if collection.count() == 0:
            return _empty_hint()

        # 用与入库相同的 bge-small-zh 模型嵌入 query，再在 collection 中查最近邻
        query_embedding = list(embedder.embed([query]))[0].tolist()
        res = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        if not res["documents"] or not res["documents"][0]:
            return "未检索到与问题相关的内容，可换更贴近文档措辞的关键词重试。"

        parts = []
        for i, (doc, meta, dist) in enumerate(
            zip(res["documents"][0], res["metadatas"][0], res["distances"][0]), 1
        ):
            source = meta.get("source", "未知来源")
            similarity = max(0.0, 1 - dist)  # cosine 空间下 distance = 1 - cos 相似度
            parts.append(f"--- 片段 {i}（来源：{source}，相似度 {similarity:.3f}）---\n{doc}")
        return "\n\n".join(parts)
    except Exception as e:
        return f"知识库检索失败：{str(e)}。{_empty_hint()}"
