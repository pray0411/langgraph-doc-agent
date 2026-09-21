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
import os
import re
import threading
from collections import Counter
from pathlib import Path

import config
from config import CHUNK_OVERLAP, CHUNK_SIZE, DOCS_DIR, EMBEDDING_MODEL, INDEX_DIR, INDEX_FILE
from logging_setup import get_logger

logger = get_logger(__name__)

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


def semantic_disabled() -> bool:
    """语义通道是否被显式关闭（环境变量 `DISABLE_SEMANTIC`）。

    为什么需要这个开关 —— 质量门禁必须在这台机器和那台机器上量到**同一个东西**。
    装了 `sentence-transformers` 走混合检索、没装走纯 BM25，两条路径的
    recall / MRR 天然不同：同一个 commit 在"装了 torch 的机器"和"CI 容器"上给出
    不同的指标，门禁就退化成"看你有没有装 torch"。所以评估与 CI 显式钉住纯 BM25，
    混合检索的指标单独量、单独报，并且报告里必须写明是哪条路径。
    """
    return os.getenv("DISABLE_SEMANTIC", "").strip().lower() in ("1", "true", "yes", "on")


def get_encoder():
    """获取句子编码器（懒加载，线程安全）。失败返回 None（调用方回退 BM25）。"""
    global _encoder, _encoder_loaded
    if _encoder_loaded:
        return _encoder
    with _encoder_lock:
        if _encoder_loaded:
            return _encoder
        _encoder_loaded = True
        if semantic_disabled():
            logger.info("语义通道已由 DISABLE_SEMANTIC 关闭，本进程使用纯 BM25")
            _encoder = None
            return _encoder
        try:
            os.environ.setdefault("HF_HOME", str(HF_HOME))
            from sentence_transformers import SentenceTransformer

            _encoder = SentenceTransformer(EMBEDDING_MODEL, cache_folder=str(HF_HOME))
            logger.info("语义检索可用：%s", EMBEDDING_MODEL)
        except Exception as exc:  # noqa: BLE001 - 任何失败都降级
            logger.warning("语义检索不可用，回退纯 BM25：%s", exc)
            _encoder = None
        return _encoder


def retrieval_mode() -> str:
    """返回本进程**实际生效**的检索模式：`"hybrid"`（BM25 + 语义）或 `"bm25"`。

    对外可见的意义很实际：报指标时能说清"这个数字是哪条路径量出来的"。
    改造前 README 直接挂出一个 recall@3=1.00，却没说明语义通道在本机根本没装 ——
    那个数字其实是纯 BM25 的成绩，被当成了混合检索的成绩。
    """
    return "hybrid" if get_encoder() is not None else "bm25"


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
        # 当前 buffer 已满（非空）先收尾
        if buffer:
            chunks.append(buffer)
            buffer = ""
        # 段落本身过长（可能 > chunk_size）：先整体入 buffer，再滑动切分
        # （修复：此前 buffer 为空时段落被直接丢弃，导致长段落内容丢失）
        buffer = para
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
    logger.info("索引完成：%d 个文档 -> %d 个片段 -> %s", len(docs), len(records), INDEX_FILE)
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


# ---------- 上传文档（内存增量索引） ----------

# 用户通过网页上传的文档单独建内存 BM25 索引（不落盘到 index.json），
# 与全局文档索引在 search() 中按同量纲 RRF 分合并排序。
# 文档集小且更新频繁，纯 BM25 足够且确定；语义向量通道保持全局索引专属。
_uploaded: list[dict] = []           # [{source, chunk, tokens}]
_uploaded_lock = threading.Lock()
_uploaded_version = 0                # 变更计数：纳入检索缓存 key，上传/删除后自动失效
_uploaded_scan_done = False          # 是否已从磁盘扫描过（见 ensure_uploaded_index）


def ensure_uploaded_index(uploads_dir: Path | str | None = None, force: bool = False) -> int:
    """从磁盘上的 uploads 目录重建内存索引（幂等，进程内默认只扫一次）。

    修复的问题：`_uploaded` 是**纯内存**索引，而全项目只有 HTTP 层的上传接口
    (`server._handle_upload`) 会往里写，启动流程从不扫描 uploads 目录。后果是
    "上传文件 → 重启服务 → 文件还在盘上、列表里也还在，但 chunks 为 0 且
    永远检索不到"，用户会以为文件丢了。

    这里把"内存索引从磁盘恢复"变成检索/列表路径的前置动作：任何一次
    `search()` 或 `list_uploaded_files()` 都会先确保索引与磁盘对齐，
    不再依赖调用方记得在启动时初始化。

    返回本次纳入索引的文档数。
    """
    global _uploaded_scan_done
    with _uploaded_lock:
        if _uploaded_scan_done and not force:
            return len({r["source"] for r in _uploaded})
        _uploaded_scan_done = True

    target = Path(uploads_dir) if uploads_dir else Path(config.UPLOADS_DIR)
    if not target.is_dir():
        return 0

    loaded = 0
    for path in sorted(target.glob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTS:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("跳过无法读取的上传文档 %s：%s", path.name, exc)
            continue
        add_uploaded_file(path.name, content)
        loaded += 1
    if loaded:
        logger.info("已从 %s 恢复 %d 个上传文档的内存索引", target, loaded)
    return loaded


def reset_state() -> None:
    """重置上传索引与缓存（测试复位用）。"""
    global _uploaded_version, _uploaded_scan_done, _encoder, _encoder_loaded
    with _uploaded_lock:
        _uploaded.clear()
        _uploaded_version += 1
        _uploaded_scan_done = False
    _search_cache.clear()
    with _encoder_lock:
        _encoder = None
        _encoder_loaded = False


def _uploaded_bm25_meta() -> tuple[int, Counter, float]:
    """上传文档集合的 BM25 全局统计 (n_docs, df, avg_dl)。"""
    n = len(_uploaded)
    df: Counter = Counter()
    total = 0
    for r in _uploaded:
        df.update(set(r["tokens"]))
        total += len(r["tokens"])
    return n, df, (total / n if n else 0.0)


def add_uploaded_file(name: str, content: str) -> int:
    """把上传文档加入内存索引（同名替换），返回切分出的片段数。"""
    global _uploaded_version
    source = f"uploads/{name}"
    chunks = split_text(content)
    with _uploaded_lock:
        _uploaded[:] = [r for r in _uploaded if r["source"] != source]
        for c in chunks:
            _uploaded.append({"source": source, "chunk": c, "tokens": _tokenize(c)})
        _uploaded_version += 1
    return len(chunks)


def remove_uploaded_file(name: str) -> bool:
    """从内存索引移除上传文档，返回是否命中。"""
    global _uploaded_version
    source = f"uploads/{name}"
    with _uploaded_lock:
        before = len(_uploaded)
        _uploaded[:] = [r for r in _uploaded if r["source"] != source]
        if len(_uploaded) != before:
            _uploaded_version += 1
            return True
    return False


def list_uploaded_files() -> list[dict]:
    """列出已索引的上传文档（名称 + 片段数）。"""
    ensure_uploaded_index()
    counts: Counter = Counter(r["source"] for r in _uploaded)
    return [
        {"name": src.split("/", 1)[1], "chunks": n} for src, n in sorted(counts.items())
    ]


def _search_uploaded(q_tokens: list[str], top_k: int, min_score: float) -> list[dict]:
    """上传文档纯 BM25 检索，返回与全局通道同量纲的 RRF 融合分。"""
    ensure_uploaded_index()
    if not _uploaded or not q_tokens:
        return []
    n_docs, df, avg_dl = _uploaded_bm25_meta()
    scores = [_bm25_score(q_tokens, r["tokens"], n_docs, df, avg_dl) for r in _uploaded]
    if not any(s > 0 for s in scores):
        return []
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    fused = []
    for rank, idx in enumerate(order):
        if scores[idx] <= 0:
            continue
        score = 1.0 / (RRF_K + rank)
        if score > min_score:
            fused.append({"score": round(score, 6), "source": _uploaded[idx]["source"], "chunk": _uploaded[idx]["chunk"]})
    return fused[:top_k]


# ---------- 检索 ----------

# 最低分数阈值：混合检索融合分；无任何通道命中的片段不参与融合
# 从 config 读取（用户可通过 .env 的 MIN_SCORE 配置），勿在模块级硬编码覆盖
MIN_SCORE = config.MIN_SCORE


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
# 索引文件 mtime 变化（重建/新增文档）或上传文档变化（_uploaded_version）时自动失效。
_search_cache: dict[tuple, list[dict]] = {}
_SEARCH_CACHE_MAX = 128


def _cache_key(query: str, top_k: int, min_score: float, index_mtime: float, uploaded_version: int) -> tuple:
    return (query, top_k, min_score, index_mtime, uploaded_version)


def search(query: str, top_k: int = 3, min_score: float = MIN_SCORE) -> list[dict]:
    """混合检索：全局文档（BM25 + 语义 RRF）+ 上传文档（BM25）合并。

    - 全局通道：BM25（jieba 分词）+ 语义向量（sentence-transformers）RRF 融合
    - 上传通道：用户上传文档的纯 BM25（同量纲 RRF 分，与全局公平竞争）
    - 语义通道不可用（索引无向量 / encoder 失败）：全局回退纯 BM25
    - 返回按融合分数降序的 top_k 个
    - 结果按 (query, 索引 mtime, 上传版本) 内存缓存，索引/上传变化自动失效
    """
    try:
        index_mtime = INDEX_FILE.stat().st_mtime if INDEX_FILE.exists() else 0.0
    except OSError:
        index_mtime = 0.0
    key = _cache_key(query, top_k, min_score, index_mtime, _uploaded_version)
    cached = _search_cache.pop(key, None)
    if cached is not None:
        # 命中即移到队尾：dict 保序，pop + 重新赋值即为 LRU 的"最近使用"
        _search_cache[key] = cached
        return cached

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
    global_result = fused[:top_k]

    # 合并上传文档通道（用户上传的文档优先参与排序，量纲一致）
    uploaded_result = _search_uploaded(q_tokens, top_k, min_score)
    if uploaded_result:
        merged = {r["source"]: r for r in global_result}
        for r in uploaded_result:
            if r["source"] not in merged or r["score"] > merged[r["source"]]["score"]:
                merged[r["source"]] = r
        result = sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:top_k]
    else:
        result = global_result

    # 写缓存（LRU 逐出）
    # 修复的问题：原实现是"满了就 _search_cache.clear() 全清"，高并发下会出现
    # 周期性抖动——刚算好的一批热门查询被一次性丢光，下一轮全部重算。
    # 现在按"最久未使用"逐条淘汰，缓存利用率恒定。
    _search_cache[key] = result
    while len(_search_cache) > _SEARCH_CACHE_MAX:
        _search_cache.pop(next(iter(_search_cache)))
    return result
