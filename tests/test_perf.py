"""性能与并发基线（待办 C3）。

为什么值得做：功能测试只能告诉你"对不对"，不能告诉你"慢没慢"。检索延迟、SSE
首字延迟、并发写 checkpointer 这三处是这个项目**唯一真实存在的性能风险面**：
- 检索是纯 Python BM25（没上倒排），语料一涨就可能悄悄变成"每次查询扫全量"；
- SSE 首字延迟是用户感知最强的一项，最容易在重构里被牺牲；
- checkpointer 是单文件 SQLite，是唯一有真实并发争用的地方。

设计（两条门禁并存，避免"基线数字换台机器就红"）：

1. **绝对上限**（默认门禁）：宽松到在慢速 CI 上也不误报，只抓**灾难性回归**
   （例如误把 BM25 退化成 O(n²)、缓存失效导致每次查询重扫全量）。
2. **相对基线**（可选门禁）：若 repo 里存在 `tests/perf_baseline.json`，则改用
   "不超过基线 1.5×" 判定。基线由开发者在**已知机器**上生成并提交：

       PERF_WRITE_BASELINE=1 python -m pytest tests/test_perf.py -m slow -q -s

   没有基线文件时自动退回绝对上限，因此 CI 不依赖任何机器特定数字。

另外补一条**机器无关**的不变量门禁：同一查询重复命中缓存时的延迟必须显著低于
首次（未命中）延迟——这能直接抓住"缓存被误删/键计算错导致缓存永不命中"的回归，
而不受机器快慢影响。

标记：全部 `slow`，本地可用 `-m "not slow"` 跳过。
"""
import json
import os
import random
import statistics
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

BASELINE_PATH = Path(__file__).parent / "perf_baseline.json"
TOLERANCE = 1.5  # 相对基线允许的退化倍数

# 语料规模：足够大到能暴露"每次查询扫全量"这类退化，又足够小到秒级构建
CORPUS_DOCS = 150
DOC_CHARS = 900

# 绝对上限（毫秒）：宽松，只抓灾难性回归
CEIL_RETRIEVAL_P95_MS = 800.0
CEIL_CONCURRENT_TOTAL_MS = 15000.0
CEIL_SSE_FIRST_TOKEN_MS = 4000.0


# ---------- 语料与基线工具 ----------

def _build_corpus(docs_dir: Path) -> list[str]:
    """写入 CORPUS_DOCS 篇文档，返回一组取自语料的查询串。"""
    docs_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20260921)  # 固定种子：语料确定，延迟才可比
    topics = ["架构", "检索", "部署", "安全", "测试", "内存", "工具", "前端"]
    queries: list[str] = []
    for i in range(CORPUS_DOCS):
        topic = topics[i % len(topics)]
        paras = []
        for j in range(6):
            paras.append(
                f"第 {j} 段：本文档编号 {i} 讨论主题 {topic} 的第 {j} 个方面，"
                f"涉及关键词 kw{i}_{j} 与 kx{rng.randint(0, 999)}，"
                f"并说明它与 {topics[(i + j) % len(topics)]} 之间的关系。"
            )
        body = "\n\n".join(paras)
        if len(body) < DOC_CHARS:
            body += "\n\n" + ("补充说明。" * ((DOC_CHARS - len(body)) // 5 + 1))
        (docs_dir / f"doc_{i:03d}.md").write_text(f"# 文档 {i}\n\n{body}\n", encoding="utf-8")
        # 查询取自语料词汇，保证能命中，测的是"检索耗时"而不是"无命中早退"
        queries.append(f"主题 {topic} 关键词 kw{i}_{i % 6}")
    return queries


@pytest.fixture()
def perf_index():
    """在隔离目录里建好语料与全局索引，返回 (queries, docs_dir)。"""
    import retriever

    docs_dir = Path(os.environ["DOCS_DIR"])
    queries = _build_corpus(docs_dir)

    t0 = time.perf_counter()
    retriever.build_index(docs_dir, force=True)
    build_ms = (time.perf_counter() - t0) * 1000
    print(f"\n[perf] 索引构建：{CORPUS_DOCS} 篇 / {build_ms:.0f} ms")
    return queries, docs_dir


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[k]


def _load_baseline() -> dict:
    if BASELINE_PATH.exists():
        try:
            return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # 基线损坏时退回绝对上限，不阻断
            return {}
    return {}


def _maybe_write_baseline(metrics: dict) -> None:
    if os.environ.get("PERF_WRITE_BASELINE"):
        BASELINE_PATH.write_text(
            json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"\n[perf] 已写入基线 {BASELINE_PATH}")


def _gate(name: str, measured_ms: float, ceiling_ms: float, baseline: dict) -> None:
    """双门禁：有基线按 1.5× 比，否则按绝对上限。"""
    if name in baseline:
        limit = float(baseline[name]) * TOLERANCE
        assert measured_ms <= limit, (
            f"{name} 相对基线退化：{baseline[name]:.1f} ms → {measured_ms:.1f} ms"
            f"（超过 {TOLERANCE}× 阈值 {limit:.1f} ms）"
        )
    else:
        assert measured_ms <= ceiling_ms, (
            f"{name} 超过绝对上限：{measured_ms:.1f} ms > {ceiling_ms:.1f} ms"
        )


# ---------- 1. 检索延迟 ----------

def test_retrieval_latency_within_budget(perf_index):
    """检索 P50/P95 延迟：既报数也设门禁。"""
    import retriever

    queries, _ = perf_index
    latencies: list[float] = []
    for q in queries:
        t0 = time.perf_counter()
        retriever.search(q, top_k=3)
        latencies.append((time.perf_counter() - t0) * 1000)

    p50, p95 = _pct(latencies, 50), _pct(latencies, 95)
    metrics = {"retrieval_p50_ms": round(p50, 3), "retrieval_p95_ms": round(p95, 3)}
    print(f"\n[perf] 检索 n={len(latencies)} P50={p50:.2f}ms P95={p95:.2f}ms")
    _maybe_write_baseline(metrics)

    baseline = _load_baseline()
    _gate("retrieval_p95_ms", p95, CEIL_RETRIEVAL_P95_MS, baseline)


def test_repeated_query_hits_cache(perf_index):
    """机器无关的不变量：同查询命中缓存必须显著快于首次。

    这条比"绝对毫秒数"更能长期有效——它直接锁住"缓存还在工作"这件事，
    换台机器、换个 CI 都成立。缓存被误删/键算错（永不命中）会立刻失败。
    """
    import retriever

    _, _ = perf_index
    q = "主题 架构 关键词 kw0_0"

    t0 = time.perf_counter()
    retriever.search(q, top_k=3)  # 首次：可能未命中缓存
    first_ms = (time.perf_counter() - t0) * 1000

    cached: list[float] = []
    for _ in range(50):
        t0 = time.perf_counter()
        retriever.search(q, top_k=3)
        cached.append((time.perf_counter() - t0) * 1000)

    cached_p95 = _pct(cached, 95)
    print(f"\n[perf] 检索缓存 首次={first_ms:.2f}ms 命中P95={cached_p95:.3f}ms")
    # 缓存命中应至少快 10 倍（首次含一次全量扫描，量级差异足够大，不会误报）
    assert cached_p95 * 10 < first_ms, (
        f"重复查询未走缓存：首次 {first_ms:.3f}ms vs 命中 {cached_p95:.3f}ms"
    )


# ---------- 2. 并发检索 ----------

def test_concurrent_search_is_consistent_within_budget(perf_index):
    """并发检索：全部成功、结果与串行一致、总耗时受控（线程安全 + 无死锁）。"""
    import retriever

    queries, _ = perf_index
    targets = queries[:80]
    # 串行基准结果
    expected = {q: retriever.search(q, top_k=3) for q in targets}

    # 关键：清掉检索缓存，否则并发线程只会全部命中缓存、根本不会同时进入计算路径，
    # 这条用例就退化成了"测 dict 读"。清缓存后各线程会真的并发计算 + 并发写缓存，
    # 才能暴露竞态（缓存 dict 在并发写下的行为）。
    retriever.reset_state()

    errors: list[BaseException] = []

    def worker(chunk: list[str]):
        try:
            for q in chunk:
                assert retriever.search(q, top_k=3) == expected[q], "并发下结果与串行不一致"
        except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
            errors.append(exc)

    buckets = [targets[i::4] for i in range(4)]  # 4 个互不重叠的桶
    threads = [threading.Thread(target=worker, args=(b,)) for b in buckets]

    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    total_ms = (time.perf_counter() - t0) * 1000

    print(f"\n[perf] 并发检索 {len(threads)} 线程 × {len(targets)} 查询 总耗时={total_ms:.0f}ms")
    assert not any(t.is_alive() for t in threads), "并发检索出现死锁（线程未结束）"
    assert errors == [], f"并发检索出错：{errors[:1]}"
    _gate("concurrent_total_ms", total_ms, CEIL_CONCURRENT_TOTAL_MS, _load_baseline())


# ---------- 3. SSE 首字延迟 ----------

def test_sse_first_token_latency(fake_llm):
    """流式问答：首字延迟受控，且事件序列以 start 开头、以 done 结尾。"""
    import graph

    fake_llm.push_text("这是流式回答的示例内容，用于测量首字延迟。" * 3)

    t0 = time.perf_counter()
    first_token_ms = None
    types: list[str] = []
    for event in graph.ask_stream("你好，请介绍一下项目", thread_id="perf-sse"):
        types.append(event["type"])
        if first_token_ms is None and event["type"] == "token":
            first_token_ms = (time.perf_counter() - t0) * 1000
    total_ms = (time.perf_counter() - t0) * 1000

    print(f"\n[perf] SSE 首字={first_token_ms:.1f}ms 全量={total_ms:.0f}ms 事件={types}")
    assert types[0] == "start", f"首个事件应为 start，实际 {types[0]}"
    assert types[-1] == "done", f"末个事件应为 done，实际 {types[-1]}"
    assert first_token_ms is not None, "未产生任何 token 事件"
    _gate("sse_first_token_ms", first_token_ms, CEIL_SSE_FIRST_TOKEN_MS, _load_baseline())


# ---------- 4. 统计工具自检（防止门禁本身写错） ----------

def test_percentile_helper_is_sane():
    """`_pct` 是上面所有门禁的度量基础，先证明它没算错。"""
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _pct(values, 50) == 3.0
    assert _pct(values, 50) == statistics.median(values)  # 交叉验证：P50 == 中位数
    assert _pct(values, 95) == 5.0
    assert _pct(values, 100) == 5.0
    assert _pct([5.0], 95) == 5.0, "单元素列表不应越界"
    # 单调性：分位数必须随 p 单调不减
    seq = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    vals = [_pct(seq, p) for p in (10, 50, 90, 100)]
    assert vals == sorted(vals)
