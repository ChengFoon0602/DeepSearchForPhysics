# ingest_kb.py — 本地知识库入库脚本
# 用法: python ingest_kb.py   （在项目根目录运行）
# 遍历 kb/docs/ 下的 .md/.txt/.pdf → 切分(chunking) → fastembed 嵌入 → 写入 Chroma 向量库
# 重跑 = 清空重建（幂等刷新）：删除旧 collection 后重新入库，保证与 kb/docs/ 一致
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # 保证以项目根目录为基准

from langchain_text_splitters import RecursiveCharacterTextSplitter
import chromadb
from fastembed import TextEmbedding

DOCS_DIR = Path(__file__).parent / "kb" / "docs"
DB_DIR = Path(__file__).parent / "kb" / "chroma_db"
COLLECTION_NAME = "kb_collection"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
CHUNK_SIZE = 500      # 每片约 500 字
CHUNK_OVERLAP = 80    # 相邻片重叠 80 字，避免切在句中被切断关键信息

# 支持的源文档后缀
SUPPORTED_SUFFIX = {".md", ".txt", ".pdf"}


def load_text(path: Path) -> str:
    """读取单个文档的纯文本。.md/.txt 直接读 UTF-8；.pdf 用 pypdf 抽取文本。"""
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    return path.read_text(encoding="utf-8")


def main():
    files = sorted(
        p for p in DOCS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIX
    )
    if not files:
        print(f"【提示】{DOCS_DIR} 为空，请先放入 .md/.txt/.pdf 文档再入库。")
        return
    print(f"发现 {len(files)} 个文档：{', '.join(p.name for p in files)}")

    # 1. 切分：把每篇文档切成有重叠的 chunk，记录每个 chunk 的来源文件名
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )
    chunks, sources = [], []
    for f in files:
        text = load_text(f)
        pieces = splitter.split_text(text)
        print(f"  {f.name}: {len(text)} 字 → {len(pieces)} 个 chunk")
        chunks.extend(pieces)
        sources.extend([f.name] * len(pieces))
    print(f"共 {len(chunks)} 个 chunk，开始嵌入……")

    # 2. 嵌入：fastembed 批量计算向量。
    #    首次运行会从 HuggingFace 下载模型（~90MB），需要网络（建议开虚拟网卡）；
    #    之后模型缓存在本地，可完全离线。
    embedder = TextEmbedding(model_name=EMBEDDING_MODEL)
    embeddings = list(embedder.embed(chunks))
    dim = len(embeddings[0])
    print(f"嵌入完成，向量维度 {dim}")

    # 3. 写入 Chroma（清空重建，保证幂等）：
    #    hnsw:space=cosine 让 collection.query 的 distance = 1 - 余弦相似度
    client = chromadb.PersistentClient(path=str(DB_DIR))
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    ids = [f"{i:06d}" for i in range(len(chunks))]
    metadatas = [{"source": s, "chunk_index": i} for i, s in enumerate(sources)]
    collection.add(ids=ids, documents=chunks, metadatas=metadatas, embeddings=embeddings)
    print(f"✅ 已写入 {len(chunks)} 条向量 → {DB_DIR}（collection: {COLLECTION_NAME}）")


if __name__ == "__main__":
    main()
