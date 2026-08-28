"""文档加载、切分与检索模块。

- load_documents: 读取 docs/ 下所有 .md/.txt/.py 文件
- split_text: 按段落/长度做重叠切分（chunk）
- build_index: 切分并保存为 JSON 索引（BM25 统计 + embedding 向量）
- search: 混合检索——BM25（jieba 分词）+ 语义向量（sentence-transformers）RRF 融合

版本历史：
- V1: TF-IDF + 余弦相似度
- V2: jieba 分词 + BM25
- V3（当前）: BM25 + embedding 语义检索的 RRF 融合
  embedding 不可用（未安装/模型加载失败/未配置）时自动回退纯 BM25，
  语义检索可用时能理解同义改写（如"架构"与"分层设计"），大幅提升文档问答质量。
"""
import json
import math
import re
import threading
from collections import Counter
from pathlib import Path

from config import CHUNK_OVERLAP, CHUNK_SIZE, DOCS_DIR, EMBEDDING_MODEL, INDEX_DIR, INDEX_FILE

SUPPORTED_EXTS = {".md", ".txt", ".py", ".rst", ".html"}

# 索引格式版本：索引结构变化时 +1，旧索引会被自动重建
INDEX_VERSION = 3

# BM25 参数（经典取值）
BM25_K1 = 1.5
BM25_B = 0.75

# RRF（Reciprocal Rank Fusion）融合参数
RRF_K = 60

# 语义检索相关性下限：cosine 低于该值的片段视为与查询语义无关（排除出融合）
SEMANTIC_MIN_COSINE = 0.2

# 模型缓存目录（sentence-transformers 下载的模型存放处）
HF_HOME = Path(__file__).parent / "models"

# jieba 可选：未安装时降级到 bigram 分词
try:
    import jieba

    jieba.setLogLevel(60)  # 关掉 jieba 的加载日志
    _HAS_JIEBA = True
except ImportError:  # pragma: no cover - 降级路径
    _HAS_JIEBA = False

# embedding 编码器（懒加载单例；不可用时为 None，检索回退纯 BM25）
# 加锁防并发：web 服务多线程首次查询可能同时触发模型加载
_encoder = None
_encoder_loaded = False
_encoder_lock = threading.Lock()


def get_encoder():
    """获取句子编码器（懒加载，线程安全）。失败返回 None（调用方回退 BM25）。"""
    global _encoder, _encoder_loaded
    if _encoder_loaded:
        return _encoder
    with _encoder_lock:
        if _encoder_loaded:
            return _encoder
        _encoder_loaded = True
        try:
            import os
            os.environ.setdefault("HF_HOME", str(HF_HOME))
            from sentence_transformers import SentenceTransformer

            _encoder = SentenceTransformer(EMBEDDING_MODEL, cache_folder=str(HF_HOME))
            print(f"[embedding] 语义检索可用：{EMBEDDING_MODEL}")
        except Exception as exc:  # noqa: BLE001 - 任何失败都降级
            print(f"[embedding] 语义检索不可用，回退纯 BM25：{exc}")
            _encoder = None
        return _encoder


def encode_texts(texts: list[str]) -> list[list[float]] | None:
    """批量编码文本；encoder 不可用时返回 None。"""
    enc = get_encoder()
    if enc is None:
        return None
    return [v.tolist() for v in enc.encode(texts, show_progress_bar=False)]


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
    """加载文档、切分、计算 BM25 统计与 embedding 向量并保存索引。"""
    if INDEX_FILE.exists() and not force:
        return load_index()
    docs = load_documents(docs_dir)
    if not docs:
        raise ValueError(f"文档目录 {docs_dir} 下没有可索引的文件")

    records = []
    all_tokenized: list[list[str]] = []
    chunks_all: list[str] = []
    for doc in docs:
        for chunk in split_text(doc["content"]):
            tokens = _tokenize(chunk)
            all_tokenized.append(tokens)
            chunks_all.append(chunk)
            records.append({"source": doc["path"], "chunk": chunk, "tokens": tokens})

    # 语义向量（可选）：encoder 可用则编码并写入记录，否则记录无 vec 字段
    vectors = encode_texts(chunks_all)
    if vectors is not None:
        for rec, vec in zip(records, vectors):
            rec["vec"] = vec

    meta = _compute_bm25_meta(all_tokenized)
    meta["embedding_model"] = EMBEDDING_MODEL if vectors is not None else None
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
        # 兼容旧格式（V1: 纯 records 列表；V2: {meta, records} 无向量）与新格式（V3）
        if isinstance(data, list) or data.get("meta", {}).get("version") != INDEX_VERSION:
            return build_index(force=True)
        return data
    except (json.JSONDecodeError, AttributeError):
        return build_index(force=True)


# ---------- 检索 ----------

# 最低分数阈值：混合检索融合分；无任何通道命中的片段不参与融合
MIN_SCORE = 0.0


def _cosine(vec_a: list[float], vec_b: list[float]) -> float:
    """两个向量的余弦相似度。"""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(x * y for x, y in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(x * x for x in vec_a))
    norm_b = math.sqrt(sum(y * y for y in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# 检索结果内存缓存：同一查询在索引未变时直接复用（小文档集场景避免重复全量计算）。
# 索引文件 mtime 变化（重建/新增文档）时自动失效。
_search_cache: dict[tuple, list[dict]] = {}
_SEARCH_CACHE_MAX = 128


def _cache_key(query: str, top_k: int, min_score: float, index_mtime: float) -> tuple:
    return (query, top_k, min_score, index_mtime)


def search(query: str, top_k: int = 3, min_score: float = MIN_SCORE) -> list[dict]:
    """混合检索：BM25（jieba 分词）+ 语义向量（sentence-transformers）RRF 融合。

    - 两条通道各自对全部片段打分并排名
    - 语义通道可用时：任一通道命中的片段参与 RRF 融合（k=60）
    - 语义通道不可用（索引无向量 / encoder 失败）：回退纯 BM25
    - 返回按融合分数降序的 top_k 个
    - 结果按 (query, 索引 mtime) 内存缓存，索引重建后自动失效
    """
    try:
        index_mtime = INDEX_FILE.stat().st_mtime if INDEX_FILE.exists() else 0.0
    except OSError:
        index_mtime = 0.0
    key = _cache_key(query, top_k, min_score, index_mtime)
    if key in _search_cache:
        return _search_cache[key]

    data = load_index()
    meta = data["meta"]
    records = data["records"]
    q_tokens = _tokenize(query)
    if not q_tokens and not query.strip():
        return []

    # 通道 1：BM25 分数
    df = Counter(meta["df"])
    bm25_scores = [
        _bm25_score(q_tokens, r["tokens"], meta["n_docs"], df, meta["avg_dl"]) for r in records
    ]

    # 通道 2：语义相似度（encoder 可用且索引有向量时）
    q_vec = None
    if records and "vec" in records[0]:
        q_vec = encode_texts([query])[0] if get_encoder() is not None else None
    cosine_scores = [_cosine(q_vec, r.get("vec", [])) if q_vec else 0.0 for r in records]

    semantic_ok = q_vec is not None

    def rank_desc(scores: list[float]) -> list[int]:
        """降序排名（分数越高排名越前；并列取相同名次）。"""
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        ranks = [0] * len(scores)
        for pos, idx in enumerate(order):
            ranks[idx] = pos
        return ranks

    bm25_ranks = rank_desc(bm25_scores) if any(s > 0 for s in bm25_scores) else None
    cosine_ranks = rank_desc(cosine_scores) if semantic_ok else None

    fused = []
    for i, r in enumerate(records):
        # 相关性判定：BM25 有共现（>0）或 语义相似度达标；都无则跳过
        bm25_hit = bm25_scores[i] > 0
        cosine_hit = semantic_ok and cosine_scores[i] >= SEMANTIC_MIN_COSINE
        if not (bm25_hit or cosine_hit):
            continue

        score = 0.0
        if bm25_ranks is not None and bm25_hit:
            score += 1.0 / (RRF_K + bm25_ranks[i])
        if cosine_ranks is not None and cosine_hit:
            score += 1.0 / (RRF_K + cosine_ranks[i])

        if score > min_score:
            fused.append({"score": round(score, 6), "source": r["source"], "chunk": r["chunk"]})

    fused.sort(key=lambda x: x["score"], reverse=True)
    result = fused[:top_k]

    # 写缓存（简单 FIFO 上限）
    if len(_search_cache) >= _SEARCH_CACHE_MAX:
        _search_cache.clear()
    _search_cache[key] = result
    return result
