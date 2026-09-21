# -*- coding: utf-8 -*-
"""检索质量评估：把"感觉还行"变成可回归的指标。

## 这个文件解决什么问题

在本文件出现之前，RAG「检索得好不好」是完全无法回归的：改一个 `top_k`、
换一次分词器、调整 RRF 的 k 值，没有任何检查会告诉你质量是升了还是降了。
对一个以文档问答为核心能力的项目，这是最大的测试空白 —— 也是**测试开发岗位
最该占的位置**：把主观判断变成可计算、可门禁的指标。

## 指标定义

- **recall@k**：前 k 条结果里包含期望文档的查询占比。"用户能不能找到"。
- **MRR**（Mean Reciprocal Rank）：期望文档首次出现名次的倒数均值，
  只对命中的查询计分，未命中记 0。它比 recall 更严格 ——
  把正确结果排在第一位，和排在第五位，recall@5 看起来一样，MRR 差很多。

两者一起看：recall 回答"找得到吗"，MRR 回答"排得够靠前吗"。

## 为什么用自包含语料

语料在 `tests/eval/corpus/`，与仓库 `docs/` 解耦。如果用 `docs/`，
任何人改一次项目文档，评估基线就会漂移，"指标从 0.79 掉到 0.71"这种结论
就失去了可比性 —— 你无法区分是检索退化了，还是文档变了。

## 运行方式

评估默认**参与**常规测试，并且**固定走纯 BM25**（无模型、无网络、秒级）—— 本模块
在导入时设置 `DISABLE_SEMANTIC=1`。为什么要钉死：门禁必须能跨机器比较，而装了
`sentence-transformers` 的机器走混合检索、没装的机器走纯 BM25，同一个 commit 会得出
两个不同的 recall/MRR，那样的门禁不如没有（详见 `retriever.semantic_disabled`）。
混合检索的指标要**单独量**，且报告里必须写明模式（`retriever.retrieval_mode()`）。

只跑评估：

    pytest -m eval -v

生成可读报告（写入指定路径，不污染仓库）：

    EVAL_REPORT=tests/eval/eval_report.md pytest -m eval
"""
import json
import os
from pathlib import Path

import pytest

from conftest import forget_app_modules

EVAL_DIR = Path(__file__).resolve().parent / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"
QA_FILE = EVAL_DIR / "qa.yaml"

pytestmark = pytest.mark.eval

# 把评估钉在纯 BM25 上（见模块 docstring）。写成模块级语句而不是 fixture：
# 必须在 retriever 被导入（get_encoder 被调用）之前生效，且不能只作用于某个用例。
os.environ["DISABLE_SEMANTIC"] = "1"


def _load_qa() -> dict:
    """读取评估集（PyYAML 缺失时给出明确的安装提示，而不是技巧性的 ImportError）。"""
    try:
        import yaml
    except ImportError:  # pragma: no cover - 环境缺依赖时的提示路径
        pytest.skip("需要 PyYAML 读取评估集：pip install pyyaml")
    return yaml.safe_load(QA_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def eval_index(tmp_path_factory):
    """在固定语料上构建索引，供本模块所有用例复用（只建一次，省时间）。"""
    index_dir = tmp_path_factory.mktemp("eval-index")
    os.environ["INDEX_DIR"] = str(index_dir)
    os.environ["DOCS_DIR"] = str(CORPUS_DIR)
    forget_app_modules()

    import config
    import retriever

    # 语料必须真的存在，否则"指标很好"只是因为检索到了别的东西
    assert CORPUS_DIR.is_dir(), f"评估语料缺失：{CORPUS_DIR}"
    docs = retriever.load_documents(CORPUS_DIR)
    assert len(docs) >= 5, f"评估语料太少（{len(docs)} 篇），指标没有意义"

    retriever.build_index(docs_dir=CORPUS_DIR, force=True)
    yield retriever
    forget_app_modules()


def _evaluate(retriever, qa: dict, top_k: int = 5) -> list[dict]:
    """对全部查询跑一遍检索，记录每条的名次与是否命中。"""
    results = []
    for item in qa["queries"]:
        hits = retriever.search(item["question"], top_k=top_k)
        sources = [Path(h["source"]).name for h in hits]
        expected = item["expected"]
        rank = sources.index(expected) + 1 if expected in sources else 0
        results.append({
            "id": item["id"],
            "question": item["question"],
            "expected": expected,
            "rank": rank,
            "top1": sources[0] if sources else "(无结果)",
            "note": item.get("note", ""),
        })
    return results


def _metrics(results: list[dict]) -> dict:
    """由逐条结果计算 recall@k 与 MRR。"""
    total = len(results)
    assert total, "评估集为空"
    metrics = {}
    for k in (1, 3, 5):
        hit = sum(1 for r in results if 0 < r["rank"] <= k)
        metrics[f"recall@{k}"] = hit / total
    metrics["mrr"] = sum(1 / r["rank"] for r in results if r["rank"]) / total
    metrics["hit_rate@5"] = sum(1 for r in results if r["rank"]) / total
    return metrics


def test_retrieval_meets_quality_thresholds(eval_index, capsys):
    """检索质量门禁：跌破阈值即失败，并打印**是哪些查询退化了**。

    只报一个"0.83 < 0.90"是不够的 —— 那只能告诉你坏了，不能告诉你坏在哪。
    因此失败信息里会列出未命中/排名靠后的查询及其考察点。
    """
    qa = _load_qa()
    results = _evaluate(eval_index, qa)
    metrics = _metrics(results)
    thresholds = qa["thresholds"]

    report = json.dumps(metrics, ensure_ascii=False, indent=2)
    with capsys.disabled():
        # 指标必须带模式标签：不写清"哪条检索路径"的成绩，数字就没有意义
        print(
            f"\n[检索评估] 模式={eval_index.retrieval_mode()} "
            f"{len(results)} 条查询\n{report}"
        )

    failures = []
    if metrics["recall@3"] < thresholds["recall_at_3"]:
        failures.append(
            f"recall@3 = {metrics['recall@3']:.2f} < "
            f"门禁 {thresholds['recall_at_3']:.2f}"
        )
    if metrics["mrr"] < thresholds["mrr"]:
        failures.append(f"MRR = {metrics['mrr']:.2f} < 门禁 {thresholds['mrr']:.2f}")

    if failures:
        missed = [
            f"  {r['id']} rank={r['rank'] or '未命中'} 期望={r['expected']} "
            f"实际Top1={r['top1']} | {r['question']}（考察：{r['note']}）"
            for r in results if r["rank"] > 3 or r["rank"] == 0
        ]
        pytest.fail(
            "检索质量跌破门禁：\n"
            + "\n".join(f"  - {f}" for f in failures)
            + f"\n\n指标：{report}\n\n退化或未命中的查询：\n"
            + ("\n".join(missed) if missed else "  （无）")
        )


def test_eval_is_pinned_to_bm25_mode(eval_index):
    """门禁必须钉在**确定的一条路径**上 —— 这里断言评估跑在纯 BM25 上。

    守的是"静默降级"这个坑的两面：
    1. 语义依赖缺失时检索会**无声地**变成纯 BM25，报出的指标如果不标模式，
       就会被当成混合检索的成绩（本项目 README 曾这样挂出 recall@3=1.00）；
    2. 反过来，如果谁去掉了这里的钉死，指标会随"本机装没装 torch"漂移，
       它会立刻失败，而不是让门禁悄悄换一套基准。
    """
    assert eval_index.semantic_disabled() is True, "评估应通过 DISABLE_SEMANTIC 钉住纯 BM25"
    assert eval_index.retrieval_mode() == "bm25", (
        f"评估实际跑在 {eval_index.retrieval_mode()} 模式下，指标不可跨机器比较"
    )


def test_eval_corpus_is_complete(eval_index):
    """评估集自检：每条 expected 都必须是语料里真实存在的文件。

    这条防的是"评估集本身写错了"：如果 expected 写了一个不存在的文件名，
    那条查询会永远算未命中，于是**越来越严的门禁会被当成产品退化**去排查，
    浪费大量时间。评估集的正确性必须由断言保证，而不是靠人盯着看。
    """
    qa = _load_qa()
    available = {p.name for p in CORPUS_DIR.glob("*") if p.is_file()}
    missing = sorted({q["expected"] for q in qa["queries"]} - available)
    assert missing == [], (
        f"评估集引用了不存在的语料文件：{missing}；"
        f"语料实际包含：{sorted(available)}"
    )


def test_eval_set_covers_every_corpus_document(eval_index):
    """评估集要覆盖每一篇语料：没有查询覆盖的文档等于没有质量约束。

    如果某篇文档没有任何查询指向它，那么它被检索系统完全忽略也不会被任何指标
    发现 —— 这正是"指标看着不错但用户就是搜不到"的成因。
    """
    qa = _load_qa()
    covered = {q["expected"] for q in qa["queries"]}
    available = {p.name for p in CORPUS_DIR.glob("*") if p.is_file()}
    uncovered = sorted(available - covered)
    assert uncovered == [], f"以下语料没有任何评估查询覆盖：{uncovered}"


def test_eval_query_ids_are_unique(eval_index):
    """query id 必须唯一（否则报告里无法定位是哪条退化）。"""
    qa = _load_qa()
    ids = [q["id"] for q in qa["queries"]]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert duplicates == [], f"评估集存在重复的 query id：{duplicates}"


def test_retrieval_is_deterministic(eval_index):
    """同一查询重复检索必须给出完全相同的结果（可回归的前提）。

    如果检索本身不确定（例如依赖集合遍历顺序、或未固定随机种子），
    那么指标会随机波动，门禁就变成了抽奖 —— 没人会再去相信它。

    注意这里清掉了检索缓存再比对：否则第二次调用只是命中缓存，
    测的是"缓存能返回同一个对象"，而不是"检索结果是确定的"。
    """
    qa = _load_qa()
    for item in qa["queries"][:8]:
        first = [(h["source"], h["score"]) for h in eval_index.search(item["question"], top_k=5)]
        eval_index.reset_state()  # 清掉检索缓存，强制真正重算
        second = [(h["source"], h["score"]) for h in eval_index.search(item["question"], top_k=5)]
        assert first == second, f"查询 {item['id']} 的检索结果不稳定：{first} vs {second}"


def test_irrelevant_query_returns_nothing(eval_index):
    """完全无关的查询不应返回结果（宁可不答，也不要凑数）。"""
    hits = eval_index.search("量子纠缠退相干实验的低温恒温器校准流程", top_k=5)
    assert hits == [], f"无关查询不该命中任何片段，实际：{hits[:2]}"


def test_report_can_be_written_when_requested(eval_index, tmp_path):
    """支持输出可读报告（用于贴给评审看"改了什么、指标怎么动"）。

    默认**不写**任何文件到仓库，避免测试产生提交物；只有显式设置 EVAL_REPORT
    时才落盘 —— 这样"跑测试"这个动作的副作用是可预期的。
    """
    qa = _load_qa()
    results = _evaluate(eval_index, qa)
    metrics = _metrics(results)

    target = Path(os.environ.get("EVAL_REPORT") or (tmp_path / "eval_report.md"))
    lines = [
        "# 检索质量评估报告",
        "",
        f"- 查询数：{len(results)}",
        f"- 语料：`tests/eval/corpus/`（{len(list(CORPUS_DIR.glob('*')))} 篇）",
        f"- **检索模式：`{eval_index.retrieval_mode()}`**"
        "（本报告固定为纯 BM25；混合检索指标需另行测量并单独标注）",
        "",
        "## 指标",
        "",
        "| 指标 | 数值 | 门禁 |",
        "|---|---|---|",
        f"| recall@1 | {metrics['recall@1']:.2f} | — |",
        f"| recall@3 | {metrics['recall@3']:.2f} | {qa['thresholds']['recall_at_3']:.2f} |",
        f"| recall@5 | {metrics['recall@5']:.2f} | — |",
        f"| MRR | {metrics['mrr']:.2f} | {qa['thresholds']['mrr']:.2f} |",
        "",
        "## 逐条明细",
        "",
        "| id | 问题 | 期望文档 | 首位名次 | Top1 实际 |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['question']} | {r['expected']} | "
            f"{r['rank'] or '未命中'} | {r['top1']} |"
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert target.exists() and "recall@3" in target.read_text(encoding="utf-8")
