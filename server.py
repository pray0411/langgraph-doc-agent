"""网页问答服务：使用 Python 标准库 http.server，零第三方依赖。

GET  /             -> 问答页面
GET  /health       -> 健康检查
GET  /api/mode     -> 获取当前运行模式
GET  /api/sessions -> 列出历史会话（thread_id/标题/时间）
GET  /api/sessions/<thread_id>/messages -> 读取会话历史消息
DELETE /api/sessions/<thread_id> -> 删除会话
POST /api/mode     -> 切换运行模式（deepseek/openai/ollama，无需重启）
POST /api/config   -> 运行时更换 API Key / 模型配置
POST /ask          -> {question, thread_id?} 返回 {answer, log, reflection, sources, thread_id}
POST /ask/stream   -> SSE 流式问答（token 增量 + 工具状态 + 最终 sources）
POST /api/run/start -> 启动交互式终端进程（{command} -> {session_id}）
POST /api/run/input -> 向终端进程写入输入（{session_id, text}）
GET  /api/run/output?session_id= -> 轮询终端新输出
POST /api/run/stop  -> 终止终端进程

加固措施：
- 请求体大小限制（MAX_BODY，防止超大请求）
- 单请求超时（timeout 线程 + 信号式检查）
- 错误信息脱敏（不向客户端暴露内部异常细节）
"""
import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from graph import ask

INDEX_HTML = Path(__file__).parent / "static" / "index.html"

# 请求体上限：10 KB（问答请求很小，防止滥用）
MAX_BODY = 10 * 1024
# 单请求超时：60 秒（模型调用 + 工具调用可能较慢）
REQUEST_TIMEOUT = 60
# SSE 流式闲置超时：60 秒内无任何事件推送（token/工具状态）即判定卡死，
# 推 error 事件并断流——防止模型调用或工具挂起时前端"正在思考"无限转圈。
SSE_IDLE_TIMEOUT = 60

# 有效在线模式（来自 config 的服务商预设 + ollama）
def _valid_modes() -> list[str]:
    from config import PROVIDER_PRESETS
    return list(PROVIDER_PRESETS.keys()) + ["ollama"]


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
    from urllib.parse import urlsplit

    try:
        from config import OLLAMA_BASE_URL
        parts = urlsplit(OLLAMA_BASE_URL)
        host = parts.hostname or "localhost"
        port = parts.port or 11434
        with socket.create_connection((host, port), timeout=1):
            return True
    except Exception:  # noqa: BLE001
        return False


def available_modes() -> list[str]:
    """返回当前可用的模式列表（在线服务商 + ollama(可用时)）。"""
    from config import PROVIDER_PRESETS
    modes = list(PROVIDER_PRESETS.keys())
    if is_ollama_available():
        modes.append("ollama")
    return modes


class Handler(BaseHTTPRequestHandler):
    def _auth_ok(self) -> bool:
        """API token 校验：配置了 API_TOKEN 时，要求请求头 X-API-Token 匹配。

        未配置 token（默认）时始终放行，保持本机使用的零配置体验。
        """
        from config import API_TOKEN

        if not API_TOKEN:
            return True
        return self.headers.get("X-API-Token", "") == API_TOKEN

    def _csrf_ok(self) -> bool:
        """CSRF 防护：状态变更请求（POST/DELETE/PUT/PATCH）必须来自本站。

        恶意网页可用 form 表单（simple request，无 CORS 预检）向本机端口
        发请求——若不校验，浏览器会替你调用 /api/run/start 执行任意命令。
        Origin 头由浏览器设置且跨站无法伪造。校验规则（精确 hostname 比较，
        杜绝 `127.0.0.1.evil.com` 这类子串匹配绕过）：
        - Origin 缺失 → 非浏览器客户端（curl/脚本），放行
        - Origin == "null" → sandboxed iframe/data: 页面，拒绝
        - Origin hostname == 请求 Host hostname → 同源放行
          （同源比对天然兼容局域网 IP / 自定义域名访问，不再硬编码回环）
        - 其余一律拒绝（跨站）
        """
        origin = self.headers.get("Origin", "")
        if not origin:
            return True  # 非浏览器客户端
        if origin == "null":
            return False
        try:
            origin_host = urlsplit(origin).hostname or ""
        except ValueError:
            return False
        if not origin_host:
            return False
        host_header = self.headers.get("Host", "")
        if host_header:
            try:
                host_host = urlsplit("//" + host_header).hostname or ""
            except ValueError:
                host_host = ""
            return host_host == origin_host
        # 无 Host 头（HTTP/1.0 客户端）：仅放行本机回环来源
        return origin_host in ("127.0.0.1", "localhost", "::1")

    def _auth_required(self) -> bool:
        """校验 token + CSRF；失败时写 401/403 并返回 False。"""
        if not self._auth_ok():
            self._json({"error": "缺少或无效的 API Token（请在 .env 配置 API_TOKEN）"}, 401)
            return False
        if self.command in ("POST", "DELETE", "PUT", "PATCH") and not self._csrf_ok():
            self._json({"error": "跨站请求被拒绝（CSRF 防护）"}, 403)
            return False
        return True

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            html = INDEX_HTML.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # 禁止缓存：index.html 随仓库更新，浏览器缓存旧页面会让用户看不到最新 UI
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif self.path == "/health":
            self._json({"status": "ok"})
        elif self.path == "/api/mode":
            self._json({"mode": get_mode(), "available_modes": available_modes()})
        elif self.path == "/api/sessions":
            if not self._auth_required():
                return
            self._handle_list_sessions()
        elif self.path.startswith("/api/sessions/") and self.path.endswith("/messages"):
            if not self._auth_required():
                return
            # /api/sessions/<thread_id>/messages
            prefix = "/api/sessions/"
            suffix = "/messages"
            thread_id = self.path[len(prefix):-len(suffix)]
            if thread_id:
                self._handle_session_messages(thread_id)
                return
            self.send_error(404)
        elif self.path.startswith("/api/run/output"):
            if not self._auth_required():
                return
            self._handle_run_output()
        elif self.path.startswith("/api/open"):
            if not self._auth_required():
                return
            self._handle_open_file()
        else:
            self.send_error(404)

    def do_DELETE(self):  # noqa: N802
        if not self._auth_required():
            return
        # 形如 /api/sessions/<thread_id>
        prefix = "/api/sessions/"
        if self.path.startswith(prefix):
            thread_id = self.path[len(prefix):]
            if thread_id:
                self._handle_delete_session(thread_id)
                return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path == "/api/mode":
            if not self._auth_required():
                return
            self._handle_set_mode()
            return
        if self.path == "/api/config":
            if not self._auth_required():
                return
            self._handle_set_config()
            return
        if self.path == "/ask":
            if not self._auth_required():
                return
            self._handle_ask()
            return
        if self.path == "/ask/stream":
            if not self._auth_required():
                return
            self._handle_ask_stream()
            return
        if self.path == "/api/run/start":
            if not self._auth_required():
                return
            self._handle_run_start()
            return
        if self.path == "/api/run/input":
            if not self._auth_required():
                return
            self._handle_run_input()
            return
        if self.path == "/api/run/stop":
            if not self._auth_required():
                return
            self._handle_run_stop()
            return
        if self.path == "/api/run/write":
            if not self._auth_required():
                return
            self._handle_run_write()
            return
        if self.path == "/api/approve":
            if not self._auth_required():
                return
            self._handle_approve()
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

        # 会话标识：前端传入 thread_id 则沿用（多轮记忆），否则生成新会话
        thread_id = (data.get("thread_id") or [""])[0].strip()
        if not thread_id:
            thread_id = uuid.uuid4().hex
            is_new_thread = True
        else:
            is_new_thread = False

        # 单请求超时：子线程执行，主线程等待，超时返回。
        # 用 Event 做完成信号，避免旧版 Timer+setdefault 的竞态：
        # 旧版在 worker 恰好完成时，timeout 标志可能已被写入，导致"已算出却报超时"。
        result_box: dict = {}
        done = threading.Event()

        def _run():
            try:
                # checkpointer 按 thread_id 自动续上历史，无需前端回传 history
                answer, result = ask(question, mode=get_mode(), thread_id=thread_id)
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
                "sources": result.get("sources", []),
                "usage": result.get("usage"),
                "cost": result.get("cost"),
                "thread_id": thread_id,
                "is_new_thread": is_new_thread,
            }
        )

    def _handle_ask_stream(self):
        """SSE 流式问答：逐 token 推送回答，含工具调用状态与最终来源。"""
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

        thread_id = (data.get("thread_id") or [""])[0].strip()
        if not thread_id:
            thread_id = uuid.uuid4().hex

        # SSE 响应头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def sse(event: dict):
            self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        # 生成器改在子线程运行、经队列投递：主线程按 SSE_IDLE_TIMEOUT 等待事件，
        # 超时（模型/工具挂起、无任何输出）即推 error 事件断流，前端转圈必然停止。
        # 注：超时后 daemon 子线程无法被强制取消，模型调用仍会在后台跑完（同 /ask
        # 的文档化限制），但连接已关闭，不会阻塞后续请求。
        from graph import ask_stream

        event_q: queue.Queue = queue.Queue()

        def _producer():
            try:
                for event in ask_stream(question, mode=get_mode(), thread_id=thread_id):
                    event_q.put(event)
            except Exception:  # noqa: BLE001
                event_q.put({"type": "error", "message": "服务内部错误"})
            finally:
                event_q.put(None)  # 结束哨兵

        threading.Thread(target=_producer, daemon=True).start()

        while True:
            try:
                event = event_q.get(timeout=SSE_IDLE_TIMEOUT)
            except queue.Empty:
                try:
                    sse({"type": "error", "message": "响应超时：模型或工具超过 60 秒无输出，已中断"})
                except Exception:  # noqa: BLE001
                    pass
                return
            if event is None:
                break
            try:
                sse(event)
            except Exception:  # noqa: BLE001
                return  # 客户端断开，停止投递
        try:
            # 末尾补 thread_id（前端需要它续会话）
            sse({"type": "meta", "thread_id": thread_id})
        except Exception:  # noqa: BLE001
            pass

    def _handle_list_sessions(self):
        from graph import list_sessions

        self._json({"sessions": list_sessions()})

    def _handle_session_messages(self, thread_id: str):
        from graph import get_session_messages

        self._json({
            "thread_id": thread_id,
            "messages": get_session_messages(thread_id, provider=get_mode()),
        })

    def _handle_delete_session(self, thread_id: str):
        from graph import delete_session

        delete_session(thread_id)
        self._json({"ok": True, "thread_id": thread_id})

    def _read_form(self) -> dict:
        """读取表单并限制大小。失败返回 {}。"""
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            return {}
        try:
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            return parse_qs(body)
        except Exception:  # noqa: BLE001
            return {}

    def _handle_run_start(self):
        """启动交互式子进程，返回 session_id。"""
        import runterm

        data = self._read_form()
        command = (data.get("command") or [""])[0].strip()
        if not command:
            self._json({"error": "命令不能为空"}, 400)
            return
        result = runterm.start(command)
        if "error" in result:
            self._json(result, 400)
            return
        self._json(result)

    def _handle_run_input(self):
        """向交互式子进程 stdin 写入输入。"""
        import runterm

        data = self._read_form()
        session_id = (data.get("session_id") or [""])[0].strip()
        text = (data.get("text") or [""])[0]
        if not session_id:
            self._json({"error": "缺少 session_id"}, 400)
            return
        self._json(runterm.send_input(session_id, text))

    def _handle_run_output(self):
        """轮询拉取子进程新输出。"""
        import runterm
        from urllib.parse import urlparse, parse_qs as _pq

        qs = _pq(urlparse(self.path).query)
        session_id = (qs.get("session_id") or [""])[0]
        if not session_id:
            self._json({"error": "缺少 session_id"}, 400)
            return
        self._json(runterm.poll(session_id))

    def _handle_run_stop(self):
        """终止交互式子进程。"""
        import runterm

        data = self._read_form()
        session_id = (data.get("session_id") or [""])[0].strip()
        if not session_id:
            self._json({"error": "缺少 session_id"}, 400)
            return
        self._json(runterm.stop(session_id))

    def _handle_run_write(self):
        """把代码写入 generated/ 临时文件（交互终端用），带路径安全校验。"""
        from pathlib import Path
        from config import WRITE_DIR

        data = self._read_form()
        path = (data.get("path") or [""])[0].strip()
        content = (data.get("content") or [""])[0]
        if not path:
            self._json({"error": "缺少 path"}, 400)
            return
        write_root = Path(WRITE_DIR).resolve()
        target = (write_root / path).resolve()
        if not target.is_relative_to(write_root):
            self._json({"error": "路径超出允许目录"}, 400)
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self._json({"ok": True, "path": str(target)})
        except OSError as exc:
            self._json({"error": f"写入失败: {exc}"}, 500)

    def _handle_approve(self):
        """登记用户对高危命令的批准（前端确认弹窗后调用）。"""
        import approvals

        data = self._read_form()
        command = (data.get("command") or [""])[0].strip()
        if not command:
            self._json({"error": "缺少 command"}, 400)
            return
        approvals.approve(command)
        self._json({"ok": True, "command": command})

    def _handle_open_file(self):
        """前端点击文件名时用系统默认程序打开 WRITE_DIR 内文件。"""
        from urllib.parse import urlparse, parse_qs as _pq

        qs = _pq(urlparse(self.path).query)
        filename = (qs.get("file") or [""])[0].strip()
        if not filename:
            self._json({"error": "缺少 file 参数"}, 400)
            return
        from tools import open_in_browser

        result = open_in_browser.invoke({"file_path": filename})
        self._json({"ok": True, "message": result})

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

    # 后台线程定期清理闲置终端会话（防内存泄漏）
    def _sweep_loop():
        import runterm

        while True:
            time.sleep(120)
            try:
                n = runterm.sweep_stale()
                if n:
                    print(f"[runterm] 清理 {n} 个闲置会话")
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_sweep_loop, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
