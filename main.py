"""命令行问答工具。

用法:
    python main.py build         # 构建文档索引
    python main.py ask "问题"    # 提问（单轮）
    python main.py web           # 启动网页服务 (默认 127.0.0.1:8000)
    python main.py web --port 9000

提示: Windows 命令行中文乱码时，请使用 `python -X utf8 main.py ...`
"""
import argparse
import sys


def cmd_build():
    from retriever import build_index
    build_index(force=True)


def cmd_ask(question: str):
    from graph import ask
    answer, _ = ask(question, show_log=True)
    print("\n==== 回答 ====")
    print(answer)


def cmd_web(host: str, port: int):
    from server import run
    run(host, port)


def main():
    parser = argparse.ArgumentParser(description="通用 AI Agent（LangGraph ReAct）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="构建文档索引")
    p_build.set_defaults(func=cmd_build)

    p_ask = sub.add_parser("ask", help="命令行提问")
    p_ask.add_argument("question", help="要问的问题")
    p_ask.set_defaults(func=cmd_ask)

    p_web = sub.add_parser("web", help="启动网页问答服务")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8000)
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args()
    if args.command == "ask":
        args.func(args.question)
    elif args.command == "web":
        args.func(args.host, args.port)
    else:
        args.func()


if __name__ == "__main__":
    sys.exit(main())
