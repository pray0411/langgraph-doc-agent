"""网页问答服务：使用 Python 标准库 http.server，零第三方依赖。

GET  /        -> 问答页面
GET  /health  -> 健康检查
POST /ask     -> {question} 返回 {answer, log, reflection}

加固措施：
- 请求体大小限制（MAX_BODY，防止超大请求）
- 单请求超时（timeout 线程 + 信号式检查）
- 错误信息脱敏（不向客户端暴露内部异常细节）
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from graph import ask

INDEX_HTML = Path(__file__).parent / "static" / "index.html"

# 请求体上限：10 KB（问答请求很小，防止滥用）
MAX_BODY = 10 * 1024
# 单请求超时：60 秒（模型调用 + 工具调用可能较慢）
REQUEST_TIMEOUT = 60


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            html = INDEX_HTML.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif self.path == "/health":
            self._json({"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path != "/ask":
            self.send_error(404)
            return

        # 请求体大小限制
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            self._json({"error": "请求体过大"}, 413)
            return

        body = self.rfile.read(length).decode("utf-8", errors="replace")
        data = parse_qs(body)
        question = (data.get("question") or [""])[0].strip()
        if not question:
            self._json({"error": "问题不能为空"}, 400)
            return

        # 单请求超时：子线程执行，主线程等待，超时返回
        result_box = {}
        timer = threading.Timer(REQUEST_TIMEOUT, lambda: result_box.setdefault("timeout", True))

        def _run():
            try:
                answer, result = ask(question)
                result_box["ok"] = (answer, result)
            except Exception:  # noqa: BLE001
                result_box["error"] = True

        timer.start()
        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=REQUEST_TIMEOUT)
        timer.cancel()

        if "timeout" in result_box:
            self._json({"error": "处理超时，请稍后重试"}, 504)
            return
        if "error" in result_box:
            # 脱敏：不向客户端暴露内部异常细节
            self._json({"error": "服务内部错误，请稍后重试"}, 500)
            return

        answer, result = result_box["ok"]
        self._json(
            {
                "answer": answer,
                "log": result.get("messages", []),
                "reflection": result.get("reflection", ""),
            }
        )

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
