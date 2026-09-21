# -*- coding: utf-8 -*-
"""CLI 入口测试（main.py）。

为什么值得测：`main.py` 是**所有用户接触这个项目的第一步**（README 的"快速开始"
三条命令全在这里），但此前覆盖率为 **0%** —— 一个没跑过的入口，参数改名、
子命令漏接、默认端口写错，都不会被任何测试发现。

这里对三个子命令各打一发，并且**断言参数确实传到了被调方**（而不是只看"没报错"）：
    build → retriever.build_index(force=True)
    ask   → graph.ask(question)
    web   → server.run(host, port)

策略说明：这三条都会启动重活（建索引 / 调模型 / 起服务），因此对**被调方**做替换，
被测的是"CLI 的分发逻辑"本身 —— 那是这个文件唯一存在的理由。
"""
import sys

import pytest


def _run_cli(monkeypatch, argv):
    """以给定 argv 调用 main()，返回捕获的 stdout。"""
    import main as main_mod

    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    main_mod.main()


# ---------- build ----------

def test_cli_build_calls_build_index_with_force(monkeypatch, capsys):
    """`main.py build` 必须重建索引（force=True），而不是"有旧索引就直接返回"。"""
    import retriever

    seen = {}

    def _fake_build(**kwargs):
        seen.update(kwargs)
        return {"meta": {}, "records": []}

    monkeypatch.setattr(retriever, "build_index", _fake_build)
    _run_cli(monkeypatch, ["build"])

    assert seen.get("force") is True, (
        "build 子命令应强制重建索引；否则用户改了文档却看不到变化"
    )


# ---------- ask ----------

def test_cli_ask_passes_question_and_prints_answer(monkeypatch, capsys):
    """`main.py ask "问题"` 应把问题原样传给 graph.ask，并打印回答。"""
    import graph

    seen = {}

    def _fake_ask(question, **kwargs):
        seen["question"] = question
        seen.update(kwargs)
        return "这是测试回答", {"messages": [], "reflection": "{}"}

    monkeypatch.setattr(graph, "ask", _fake_ask)
    _run_cli(monkeypatch, ["ask", "今天天气如何"])

    assert seen["question"] == "今天天气如何"
    out = capsys.readouterr().out
    assert "这是测试回答" in out, f"回答应被打印出来，实际输出：{out!r}"


def test_cli_ask_does_not_swallow_the_answer(monkeypatch, capsys):
    """回答里有换行/特殊字符时也必须完整打印（截断或转义都是缺陷）。"""
    import graph

    monkeypatch.setattr(
        graph, "ask", lambda *a, **k: ("第一行\n第二行", {"messages": [], "reflection": "{}"})
    )
    _run_cli(monkeypatch, ["ask", "多行"])
    out = capsys.readouterr().out
    assert "第一行" in out and "第二行" in out


# ---------- web ----------

def test_cli_web_default_host_and_port(monkeypatch):
    """`main.py web` 的默认值必须与 README 一致（127.0.0.1:8000）。

    这条防的是"文档写 8000、代码默认成别的"这类低级但真实的偏差：
    它不会让任何测试变红，只会让用户按文档访问时打不开。
    """
    import server

    seen = {}
    monkeypatch.setattr(server, "run", lambda host, port: seen.update(host=host, port=port))

    _run_cli(monkeypatch, ["web"])

    assert seen == {"host": "127.0.0.1", "port": 8000}, f"默认值不符：{seen}"


def test_cli_web_accepts_explicit_host_and_port(monkeypatch):
    """显式传入的 host / port 必须生效，且 port 被解析为 int。"""
    import server

    seen = {}
    monkeypatch.setattr(server, "run", lambda host, port: seen.update(host=host, port=port))

    _run_cli(monkeypatch, ["web", "--host", "0.0.0.0", "--port", "9001"])

    assert seen == {"host": "0.0.0.0", "port": 9001}
    assert isinstance(seen["port"], int), "port 必须是 int，不能是字符串"


# ---------- 参数校验 ----------

def test_cli_requires_a_subcommand(monkeypatch, capsys):
    """不带子命令时必须报错退出，而不是静默什么都不做。

    静默退出最糟：用户以为服务起来了，实际进程立刻结束，只能自己去猜原因。
    """
    import main as main_mod

    monkeypatch.setattr(sys, "argv", ["main.py"])
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main()
    assert excinfo.value.code != 0
    assert "usage" in capsys.readouterr().err.lower()


def test_cli_ask_requires_question_argument(monkeypatch, capsys):
    """`ask` 缺少问题参数时必须报错（不能变成"问了空字符串"）。"""
    import main as main_mod

    monkeypatch.setattr(sys, "argv", ["main.py", "ask"])
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main()
    assert excinfo.value.code != 0


def test_cli_rejects_unknown_subcommand(monkeypatch):
    """未知子命令必须报错退出（防止拼错命令后静默走默认分支）。"""
    import main as main_mod

    monkeypatch.setattr(sys, "argv", ["main.py", "buidl"])  # 故意拼错
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main()
    assert excinfo.value.code != 0
