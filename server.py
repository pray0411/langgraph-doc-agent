"""网页问答服务：使用 Python 标准库 http.server，零第三方依赖。

GET  /        -> 问答页面
POST /ask     -> {question} 返回 {answer, log, reflection}
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from graph import ask

INDEX_HTML = Path(__file__).parent / "static" / "index.html"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            html = INDEX_HTML.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path == "/ask":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = parse_qs(body)
            question = (data.get("question") or [""])[0].strip()
            if not question:
                self._json({"error": "问题不能为空"}, 400)
                return
            try:
                answer, result = ask(question)
                self._json(
                    {
                        "answer": answer,
                        "log": result.get("messages", []),
                        "reflection": result.get("reflection", ""),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)
        else:
            self.send_error(404)

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 精简日志
        pass


def run(host: str = "127.0.0.1", port: int = 8000):
    print(f"网页服务已启动: http://{host}:{port}")
    print("按 Ctrl+C 停止。")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
