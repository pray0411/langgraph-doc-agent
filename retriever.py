"""文档加载与切分模块。

- load_documents: 读取 docs/ 下所有 .md/.txt/.py 文件
- split_text: 按段落/长度做重叠切分（chunk）
- build_index: 切分并保存为 JSON 索引（含 TF-IDF 向量）
"""
import json
import math
import re
from collections import Counter
from pathlib import Path

from config import CHUNK_OVERLAP, CHUNK_SIZE, DOCS_DIR, INDEX_DIR, INDEX_FILE

SUPPORTED_EXTS = {".md", ".txt", ".py", ".rst", ".html"}


# ---------- 加载 ----------

def load_documents(docs_dir: Path = DOCS_DIR) -> list[dict]:
    """读取文档目录下所有支持的文件，返回 [{path, content}]。"""
    docs_dir = Path(docs_dir)
    if not docs_dir.exists():
        raise FileNotFoundError(f"文档目录不存在: {docs_dir}")
    docs = []
    for path in sorted(docs_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = path.read_text(encoding="utf-8", errors="ignore")
            if content.strip():
                docs.append({"path": str(path), "content": content})
    return docs


# ---------- 切分 ----------

def _split_by_paragraph(text: str, chunk_size: int, overlap: int) -> list[str]:
    """按段落切分，超过 chunk_size 的段落再按长度滑动切分。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        if len(buffer) + len(para) + 1 <= chunk_size:
            buffer = f"{buffer}\n{para}".strip()
            continue
        if buffer:
            chunks.append(buffer)
            buffer = para
        # 段落本身过长时滑动切分
        while len(buffer) > chunk_size:
            chunks.append(buffer[:chunk_size])
            buffer = buffer[chunk_size - overlap:]
    if buffer:
        chunks.append(buffer)
    return chunks


def split_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """将一篇文档切分为带重叠的 chunk 列表。"""
    return _split_by_paragraph(text, chunk_size, overlap)


# ---------- TF-IDF ----------

def _tokenize(text: str) -> list[str]:
    """中文按单字+双字词、英文按单词切分，统一小写。"""
    text = text.lower()
    tokens: list[str] = []
    # 英文单词 / 数字
    tokens += re.findall(r"[a-z0-9_]+", text)
    # 中文：双字词（bigram）
    zh = re.findall(r"[\u4e00-\u9fff]+", text)
    for seg in zh:
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens += [seg[i : i + 2] for i in range(len(seg) - 1)]
    return tokens


def _compute_tfidf(chunks: list[str]) -> list[dict]:
    """为每个 chunk 计算 TF-IDF 稀疏向量，返回 [{chunk, tokens, vec}]。"""
    doc_count = len(chunks)
    df: Counter = Counter()
    tokenized = []
    for chunk in chunks:
        tokens = _tokenize(chunk)
        tokenized.append(tokens)
        df.update(set(tokens))

    vectors = []
    for tokens in tokenized:
        tf = Counter(tokens)
        vec = {}
        for token, count in tf.items():
            idf = math.log((doc_count + 1) / (df[token] + 1)) + 1.0
            vec[token] = count * idf
        vectors.append(vec)
    return vectors


def cosine_similarity(vec_a: dict, vec_b: dict) -> float:
    """计算两个稀疏向量的余弦相似度。"""
    if not vec_a or not vec_b:
        return 0.0
    common = set(vec_a) & set(vec_b)
    if not common:
        return 0.0
    dot = sum(vec_a[t] * vec_b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------- 索引 ----------

def build_index(docs_dir: Path = DOCS_DIR, force: bool = False) -> dict:
    """加载文档、切分、计算 TF-IDF 并保存索引。"""
    if INDEX_FILE.exists() and not force:
        return load_index()
    docs = load_documents(docs_dir)
    if not docs:
        raise ValueError(f"文档目录 {docs_dir} 下没有可索引的文件")

    records = []
    for doc in docs:
        chunks = split_text(doc["content"])
        vectors = _compute_tfidf(chunks)
        for chunk, vec in zip(chunks, vectors):
            records.append(
                {
                    "source": doc["path"],
                    "chunk": chunk,
                    "vec": vec,
                }
            )
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[index] 索引完成：{len(docs)} 个文档 -> {len(records)} 个片段 -> {INDEX_FILE}")
    return records


def load_index() -> list[dict]:
    if not INDEX_FILE.exists():
        return build_index(force=True)
    return json.loads(INDEX_FILE.read_text(encoding="utf-8"))


# ---------- 检索 ----------

# 最低相似度阈值：低于该值的片段视为"与问题无关"，不返回（避免硬塞无关上下文）
MIN_SCORE = 0.05


def search(query: str, top_k: int = 3, min_score: float = MIN_SCORE) -> list[dict]:
    """对查询做 TF-IDF 向量化并与索引中所有 chunk 计算相似度。

    - 只返回相似度 >= min_score 的片段（低于阈值视为无关，拒答）
    - 返回按相似度降序的 top_k 个
    """
    records = load_index()
    q_vec = _compute_tfidf([query])[0]
    scored = [
        {"score": cosine_similarity(q_vec, r["vec"]), "source": r["source"], "chunk": r["chunk"]}
        for r in records
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    # 阈值过滤：低于最低相似度的结果丢弃
    filtered = [s for s in scored if s["score"] >= min_score]
    return filtered[:top_k]
