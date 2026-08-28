"""文档加载、切分与检索模块。

- load_documents: 读取 docs/ 下所有 .md/.txt/.py 文件
- split_text: 按段落/长度做重叠切分（chunk）
- build_index: 切分并保存为 JSON 索引（含 BM25 统计信息）
- search: 对查询做 jieba 分词（降级 bigram）后按 BM25 打分召回

V2 变更（相对旧版 TF-IDF + 余弦相似度）：
1. 中文分词升级为 jieba（未安装时自动降级为单字+双字 bigram）
2. 排序改为 BM25（k1=1.5, b=0.75），对长文档更公平，检索质量明显优于 TF-IDF
3. 索引文件带版本号，旧格式索引会自动重建（load_index 检测）
"""
import json
import math
import re
from collections import Counter
from pathlib import Path

from config import CHUNK_OVERLAP, CHUNK_SIZE, DOCS_DIR, INDEX_DIR, INDEX_FILE

SUPPORTED_EXTS = {".md", ".txt", ".py", ".rst", ".html"}

# 索引格式版本：索引结构变化时 +1，旧索引会被自动重建
INDEX_VERSION = 2

# BM25 参数（经典取值）
BM25_K1 = 1.5
BM25_B = 0.75

# jieba 可选：未安装时降级到 bigram 分词
try:
    import jieba

    jieba.setLogLevel(60)  # 关掉 jieba 的加载日志
    _HAS_JIEBA = True
except ImportError:  # pragma: no cover - 降级路径
    _HAS_JIEBA = False


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


# ---------- 分词 ----------

# 中文停用词：与检索无关的高频虚词，分词后按整词匹配过滤。
# 不加的话，小索引下 "的/了" 等词的 idf 接近 0 仍为正，会让无关查询误命中。
STOPWORDS = frozenset(
    [
        "的", "了", "吗", "呢", "吧", "啊", "哦", "嗯", "呀", "么", "嘛",
        "是", "在", "与", "和", "或", "及", "就", "都", "而", "其", "之",
        "这个", "那个", "这些", "那些", "怎么", "什么", "为什么", "如何", "怎样",
        "请问", "一下", "帮我", "我想", "你", "你们", "他", "她", "它", "我们",
    ]
)


def _tokenize(text: str) -> list[str]:
    """中文用 jieba 分词、英文按单词切分，统一小写并过滤停用词。

    jieba 未安装时自动降级：中文按单字+双字 bigram。
    返回: token 列表（可重复，保留词频）。
    """
    text = text.lower()
    tokens: list[str] = []
    # 英文单词 / 数字
    tokens += re.findall(r"[a-z0-9_]+", text)
    # 中文
    zh_segs = re.findall(r"[\u4e00-\u9fff]+", text)
    if _HAS_JIEBA:
        for seg in zh_segs:
            tokens += [w for w in jieba.cut(seg) if w.strip() and w not in STOPWORDS]
    else:  # 降级：单字 + 双字 bigram
        for seg in zh_segs:
            if len(seg) == 1:
                if seg not in STOPWORDS:
                    tokens.append(seg)
            else:
                tokens += [seg[i : i + 2] for i in range(len(seg) - 1)]
    return tokens


# ---------- BM25 ----------

def _bm25_idf(n_docs: int, df: int) -> float:
    """BM25 的 idf：文档频率越高权重越低；df 接近 N 时为负（惩罚通用词）。"""
    return math.log(1 + (n_docs - df + 0.5) / (df + 0.5))


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    n_docs: int,
    df: Counter,
    avg_dl: float,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> float:
    """单文档的 BM25 打分（查询 token 去重后求和）。"""
    if not query_tokens or not doc_tokens:
        return 0.0
    tf = Counter(doc_tokens)
    dl = len(doc_tokens)
    norm = 1 - b + b * dl / avg_dl if avg_dl > 0 else 1.0
    score = 0.0
    for t in set(query_tokens):
        if t not in tf:
            continue
        f = tf[t]
        score += _bm25_idf(n_docs, df.get(t, 0)) * (f * (k1 + 1)) / (f + k1 * norm)
    return score


# ---------- 索引 ----------

def _compute_bm25_meta(tokenized_chunks: list[list[str]]) -> dict:
    """从所有 chunk 的 token 列表计算 BM25 需要的全局统计。"""
    n_docs = len(tokenized_chunks)
    df: Counter = Counter()
    total_len = 0
    for tokens in tokenized_chunks:
        df.update(set(tokens))
        total_len += len(tokens)
    avg_dl = total_len / n_docs if n_docs else 0.0
    return {"version": INDEX_VERSION, "n_docs": n_docs, "avg_dl": avg_dl, "df": dict(df)}


def build_index(docs_dir: Path = DOCS_DIR, force: bool = False) -> dict:
    """加载文档、切分、计算 BM25 统计并保存索引。"""
    if INDEX_FILE.exists() and not force:
        return load_index()
    docs = load_documents(docs_dir)
    if not docs:
        raise ValueError(f"文档目录 {docs_dir} 下没有可索引的文件")

    records = []
    all_tokenized: list[list[str]] = []
    for doc in docs:
        for chunk in split_text(doc["content"]):
            tokens = _tokenize(chunk)
            all_tokenized.append(tokens)
            records.append({"source": doc["path"], "chunk": chunk, "tokens": tokens})

    meta = _compute_bm25_meta(all_tokenized)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(
        json.dumps({"meta": meta, "records": records}, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[index] 索引完成：{len(docs)} 个文档 -> {len(records)} 个片段 -> {INDEX_FILE}")
    return {"meta": meta, "records": records}


def load_index() -> dict:
    """加载索引；格式/版本不匹配或损坏时自动重建。"""
    if not INDEX_FILE.exists():
        return build_index(force=True)
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        # 兼容旧格式（V1: 纯 records 列表，TF-IDF）与新格式（V2: {meta, records}）
        if isinstance(data, list) or data.get("meta", {}).get("version") != INDEX_VERSION:
            return build_index(force=True)
        return data
    except (json.JSONDecodeError, AttributeError):
        return build_index(force=True)


# ---------- 检索 ----------

# 最低相似度阈值：BM25 语义下，与查询无任何共现词的文档分数 <= 0
MIN_SCORE = 0.0


def search(query: str, top_k: int = 3, min_score: float = MIN_SCORE) -> list[dict]:
    """对查询分词后与索引中所有 chunk 计算 BM25 分数。

    - 只返回分数 >= min_score 的片段（默认 0.0：无共现即无关）
    - 返回按分数降序的 top_k 个
    """
    data = load_index()
    meta = data["meta"]
    records = data["records"]
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []

    scored = []
    for r in records:
        score = _bm25_score(
            q_tokens, r["tokens"], meta["n_docs"], Counter(meta["df"]), meta["avg_dl"]
        )
        # 严格大于：0 分（与查询无任何共现词）视为无关，即使 min_score=0 也不返回
        if score > min_score:
            scored.append({"score": score, "source": r["source"], "chunk": r["chunk"]})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
