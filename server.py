"""网页问答服务：使用 Python 标准库 http.server，零第三方依赖。

GET  /             -> 问答页面
GET  /health       -> 健康检查
GET  /api/mode     -> 获取当前运行模式
POST /api/mode     -> 切换运行模式（deepseek/openai/offline，无需重启）
POST /ask          -> {question} 返回 {answer, log, reflection}

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

# 有效在线模式（来自 config 的服务商预设 + ollama/offline）
def _valid_modes() -> list[str]:
    from config import PROVIDER_PRESETS
    return list(PROVIDER_PRESETS.keys()) + ["ollama", "offline"]


# 当前运行模式（默认读取 .env 的 LLM_PROVIDER，可通过 /api/mode 动态切换）
# 加锁保护：ThreadingHTTPServer 会并发处理请求，裸全局变量存在竞态
_mode_lock = threading.Lock()
_current_mode: str | None = None


def get_mode() -> str:
    """获取当前模式，首次调用时从 config 读取默认值。"""
    global _current_mode
    with _mode_lock:
        if _current_mode is None:
            from config import LLM_PROVIDER
            _current_mode = LLM_PROVIDER
        return _current_mode


def set_mode(mode: str):
    """设置当前模式。"""
    global _current_mode
    with _mode_lock:
        _current_mode = mode


def is_ollama_available() -> bool:
    """检测本地 Ollama 服务是否可用（http://localhost:11434）。"""
    import socket

    try:
        from config import OLLAMA_BASE_URL
        host = OLLAMA_BASE_URL.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(OLLAMA_BASE_URL.split(":")[-1].split("/")[0])
        with socket.create_connection((host, port), timeout=1):
            return True
    except Exception:  # noqa: BLE001
        return False


def available_modes() -> list[str]:
    """返回当前可用的模式列表（在线服务商 + ollama(可用时) + offline）。"""
    from config import PROVIDER_PRESETS
    modes = list(PROVIDER_PRESETS.keys())
    if is_ollama_available():
        modes.append("ollama")
    modes.append("offline")
    return modes


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
        elif self.path == "/api/mode":
            self._json({"mode": get_mode(), "available_modes": available_modes()})
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path == "/api/mode":
            self._handle_set_mode()
            return
        if self.path == "/api/config":
            self._handle_set_config()
            return
        if self.path == "/ask":
            self._handle_ask()
            return
        self.send_error(404)

    def _handle_set_mode(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            self._json({"error": "请求体过大"}, 413)
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        data = parse_qs(body)
        mode = (data.get("mode") or [""])[0].strip().lower()
        if mode not in _valid_modes():
            self._json({"error": f"无效模式: {mode}，可选 {'/'.join(_valid_modes())}"}, 400)
            return
        set_mode(mode)
        self._json({"mode": get_mode(), "message": f"已切换到 {mode} 模式"})

    def _handle_set_config(self):
        """网页端更换 API Key / 模型配置（支持所有预设服务商）。"""
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            self._json({"error": "请求体过大"}, 413)
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        data = parse_qs(body)
        provider = (data.get("provider") or [""])[0].strip().lower()
        api_key = (data.get("api_key") or [""])[0].strip()
        base_url = (data.get("base_url") or [""])[0].strip()
        model = (data.get("model") or [""])[0].strip()

        from config import PROVIDER_PRESETS
        if provider not in PROVIDER_PRESETS:
            self._json({"error": f"provider 仅支持: {'/'.join(PROVIDER_PRESETS.keys())}"}, 400)
            return
        if not api_key:
            self._json({"error": "API Key 不能为空"}, 400)
            return

        from config import set_runtime_provider_config
        set_runtime_provider_config(provider, api_key, base_url, model)
        self._json(
            {
                "ok": True,
                "message": f"{provider} API Key 已更新（仅本次运行有效）",
                "provider": provider,
            }
        )

    def _handle_ask(self):
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

        # 解析多轮对话历史（JSON 数组，可选）
        history = []
        hist_raw = (data.get("history") or [""])[0].strip()
        if hist_raw:
            try:
                parsed = json.loads(hist_raw)
                if isinstance(parsed, list):
                    # 只保留 user/assistant 角色，限制条数避免过长
                    history = [
                        {"role": m["role"], "content": m["content"]}
                        for m in parsed
                        if m.get("role") in ("user", "assistant") and m.get("content")
                    ][-20:]
            except (json.JSONDecodeError, KeyError, TypeError):
                history = []

        # 单请求超时：子线程执行，主线程等待，超时返回。
        # 用 Event 做完成信号，避免旧版 Timer+setdefault 的竞态：
        # 旧版在 worker 恰好完成时，timeout 标志可能已被写入，导致"已算出却报超时"。
        result_box: dict = {}
        done = threading.Event()

        def _run():
            try:
                # 使用当前动态模式 + 多轮历史（运行时切换，无需重启服务）
                answer, result = ask(question, mode=get_mode(), history=history)
                result_box["ok"] = (answer, result)
            except Exception:  # noqa: BLE001
                result_box["error"] = True
            finally:
                done.set()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        timed_out = not done.wait(timeout=REQUEST_TIMEOUT)

        if timed_out:
            # 注意：worker 是 daemon 线程，超时后无法强制取消模型调用，
            # 但请求立即返回，不会阻塞后续请求；结果由 daemon 线程自行丢弃。
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
