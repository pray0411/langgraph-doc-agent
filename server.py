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
POST /api/run/prepare -> 交互终端启动前分级；高危命令返回 {need_confirm, nonce, command, reason}
POST /api/run/start -> 启动交互式终端进程（{command} -> {session_id}）
POST /api/run/input -> 向终端进程写入输入（{session_id, text}）
GET  /api/run/output?session_id= -> 轮询终端新输出
POST /api/run/stop  -> 终止终端进程
POST /api/confirm   -> 用服务端签发的 nonce 换取一次性批准（{nonce, command}）
                       —— 取代旧的 /api/approve：那个接口接受裸命令直接登记为
                       "用户已批准"，等于把授权做成了自助接口（外部评审 P0-1）。

加固措施：
- 请求体大小限制（MAX_BODY，防止超大请求）
- 单请求超时（timeout 线程 + 信号式检查）
- 错误信息脱敏（不向客户端暴露内部异常细节）
- 来源校验（Origin/Sec-Fetch-Site/Host 三重），见 Handler._origin_ok
- 绑定非回环地址时强制 API Token（未配置则随机生成并打印），见 _enforce_token_for_public_bind
"""
import base64
import json
import os
import queue
import re
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from graph import ask
from logging_setup import audit, get_logger, setup_logging

logger = get_logger(__name__)

INDEX_HTML = Path(__file__).parent / "static" / "index.html"
DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"

# 请求体上限：10 KB（问答请求很小，防止滥用）
MAX_BODY = 10 * 1024
# 单请求超时：60 秒（模型调用 + 工具调用可能较慢）
REQUEST_TIMEOUT = 60
# SSE 流式闲置超时：60 秒内无任何事件推送（token/工具状态）即判定卡死，
# 推 error 事件并断流——防止模型调用或工具挂起时前端"正在思考"无限转圈。
SSE_IDLE_TIMEOUT = 60
# 上传文档：单文件上限 5 MB（base64 后请求体约 6.7 MB，body 上限放宽到 8 MB）
UPLOAD_MAX_FILE = 5 * 1024 * 1024
UPLOAD_MAX_BODY = 8 * 1024 * 1024
# 上传文档允许的扩展名（与 retriever.SUPPORTED_EXTS 保持一致）
UPLOAD_EXTS = {".md", ".txt", ".py", ".rst", ".html"}

# 有效在线模式（来自 config 的服务商预设 + ollama）
def _valid_modes() -> list[str]:
    from config import PROVIDER_PRESETS
    return list(PROVIDER_PRESETS.keys()) + ["ollama"]


def _int_param(qs: dict, name: str, default: int, low: int, high: int) -> int:
    """从查询串取一个被夹紧到 [low, high] 的整数参数（非法值退回默认）。"""
    raw = (qs.get(name) or [""])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


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


_ALLOWED_ORIGIN_CACHE: tuple[float, set[str]] | None = None
_ALLOWED_ORIGIN_TTL = 300.0


def allowed_origin_hosts() -> set[str]:
    """服务端**自己**认可的本地身份 host 集合，供来源校验使用。

    为什么不能用客户端自发的 `Host` 头来比（旧实现的真实缺陷）：
    旧规则是 `Origin 的 hostname == Host 头的 hostname`。在 DNS rebinding 场景下
    攻击者把 `evil.com` 解析到 127.0.0.1，浏览器访问 `http://evil.com`，于是
    `Origin: http://evil.com` 与 `Host: evil.com` **天然相等**，校验必然通过。
    它校验的其实是"客户端自己声称的两个值彼此一致"，而不是"请求来自本机界面"。

    这里改为只信任服务端能独立确认的身份：
    - 回环地址 / localhost / 本机主机名
    - 本机所有网卡地址（兼容局域网 IP 访问）
    - 环境变量 `ALLOWED_ORIGINS`（逗号分隔，供自定义域名 / 反向代理场景显式放行）

    攻击者控制的域名永远不在这个集合里——这才是校验能成立的原因。
    """
    global _ALLOWED_ORIGIN_CACHE
    now = time.time()
    if _ALLOWED_ORIGIN_CACHE is not None and now - _ALLOWED_ORIGIN_CACHE[0] < _ALLOWED_ORIGIN_TTL:
        return _ALLOWED_ORIGIN_CACHE[1]

    hosts = {"127.0.0.1", "localhost", "::1"}
    try:
        hostname = socket.gethostname()
        hosts.add(hostname.lower())
        for info in socket.getaddrinfo(hostname, None):
            hosts.add(str(info[4][0]).lower())
    except OSError:
        pass
    for extra in (os.getenv("ALLOWED_ORIGINS") or "").split(","):
        extra = extra.strip().lower()
        if extra:
            hosts.add(urlsplit("//" + extra).hostname or extra)

    _ALLOWED_ORIGIN_CACHE = (now, hosts)
    return hosts


def reset_origin_cache() -> None:
    """清空来源白名单缓存（测试复位 / 运行中改了 ALLOWED_ORIGINS 后用）。"""
    global _ALLOWED_ORIGIN_CACHE
    _ALLOWED_ORIGIN_CACHE = None


# 连通性自检（/api/config/test）被拒绝的目标。
# 该接口会向客户端传入的 base_url 发请求**并把响应体前 500 字符回显**，
# 于是"默认无鉴权 + 本机可访问内网"组合起来就是一个可用的 SSRF 原语——
# 外部评审据此指出可以拿它探测内网 / 云元数据。这里挡掉最典型、且几乎
# 不可能有正当用途的目标（云实例元数据是 SSRF 拿凭据的首选目标）。
# 说明：**不**封私网地址段——"自检局域网里的自建网关"正是本功能的正当用途，
# 一刀切会把它废掉。残留面已在 README「安全边界」写明。
_BLOCKED_PROBE_HOSTS = {
    "169.254.169.254",          # AWS / Azure / GCP 元数据
    "metadata.google.internal",  # GCP 元数据域名
    "100.100.100.200",           # 阿里云元数据
}


def blocked_probe_target(raw_url: str) -> str:
    """连通性自检的目标地址校验。返回拒绝原因，空串表示放行。"""
    if not raw_url:
        return "地址为空"
    try:
        parts = urlsplit(raw_url if "://" in raw_url else "//" + raw_url)
    except ValueError:
        return "地址无法解析"
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https"):
        return f"不支持的协议 {scheme!r}（仅允许 http/https）"
    host = (parts.hostname or "").lower()
    if not host:
        return "缺少主机名"
    if host in _BLOCKED_PROBE_HOSTS:
        return f"{host} 属于云元数据地址"
    if host.startswith("169.254."):
        return f"{host} 属于链路本地地址段（169.254.0.0/16）"
    return ""


class Handler(BaseHTTPRequestHandler):
    def _auth_ok(self) -> bool:
        """API token 校验：配置了 API_TOKEN 时，要求请求头 X-API-Token 匹配。

        未配置 token（默认）时始终放行，保持本机使用的零配置体验——
        **前提是默认只绑回环地址**（`server.run` 默认 127.0.0.1）。
        若把服务暴露到局域网/公网，必须自行配置 API_TOKEN（README「安全边界」）。
        这一层只管"你是谁"；"请求从哪来"由 `_origin_ok` 负责，两层职责分开。
        """
        from config import API_TOKEN

        if not API_TOKEN:
            return True
        return self.headers.get("X-API-Token", "") == API_TOKEN

    def _origin_ok(self) -> tuple[bool, str]:
        """来源校验：这次状态变更请求是否可能来自本机界面。返回 (是否放行, 原因)。

        恶意网页可用 form 表单（simple request，无 CORS 预检）向本机端口发请求。
        注意这里为什么**不能**指望 nonce 模型兜住：nonce 防的是"凭空授予批准"，
        防不住"由外部页面发起的一整条完整流程" —— 攻击页面可以自己依次走完
        `/api/run/prepare → /api/confirm → /api/run/start`，每一步都合规矩。
        所以"是不是本机界面发起的"必须由这一层独立判定。规则按顺序：

        1. 携带了**已配置且正确**的 API Token → 放行。显式凭据不是"环境凭据"，
           CSRF 的威胁模型不适用（浏览器不会替你把这个头带上）。
        2. 有 Origin → 其 hostname 必须在服务端认可的本地身份集合内
           （见 `allowed_origin_hosts`）。**不再与客户端自发的 Host 头比较**。
        3. 无 Origin → 看 `Sec-Fetch-Site`：显式标记跨站则拒绝。
           现代浏览器对跨站请求至少会带 Origin / Sec-Fetch-Site 之一；
           两者都没有的是 curl 等非浏览器客户端，放行以保持可用性
           （这条残留面已在 README「安全边界」中写明）。
        """
        from config import API_TOKEN

        if API_TOKEN and self.headers.get("X-API-Token", "") == API_TOKEN:
            return True, "token"
        allowed = set(allowed_origin_hosts())
        bound = getattr(getattr(self, "server", None), "server_address", ("",))[0]
        if bound and bound not in ("0.0.0.0", "::", ""):
            allowed.add(str(bound).lower())

        # 先看 Host 头。注意：这里**不是**把它当作可信依据，而是当作可疑信号——
        # 请求带来的 Host 若不是本机已知身份，说明有东西把外部域名解析到了本机
        # 端口（DNS rebinding 的典型特征）。这一层不依赖浏览器是否发送
        # Origin / Sec-Fetch-Site，是覆盖面最广的一道。
        host_header = self.headers.get("Host", "")
        if host_header:
            try:
                host_host = (urlsplit("//" + host_header).hostname or "").lower()
            except ValueError:
                host_host = ""
            if host_host and host_host not in allowed:
                return False, f"Host {host_host} 不是本机已知身份（疑似域名解析劫持）"

        origin = self.headers.get("Origin", "")
        if origin:
            if origin == "null":
                return False, "Origin=null（sandboxed iframe / data: 页面）"
            try:
                origin_host = (urlsplit(origin).hostname or "").lower()
            except ValueError:
                return False, "Origin 无法解析"
            if not origin_host:
                return False, "Origin 缺少 hostname"
            if origin_host in allowed:
                return True, "origin"
            return False, f"来源 {origin_host} 不是本机已知身份"
        site = self.headers.get("Sec-Fetch-Site", "")
        if site and site not in ("same-origin", "none"):
            return False, f"Sec-Fetch-Site={site}"
        return True, "non-browser"

    def _auth_required(self) -> bool:
        """校验 token + 来源；失败时写 401/403 并返回 False。"""
        if not self._auth_ok():
            self._json({"error": "缺少或无效的 API Token（请在 .env 配置 API_TOKEN）"}, 401)
            return False
        if self.command in ("POST", "DELETE", "PUT", "PATCH"):
            ok, reason = self._origin_ok()
            if not ok:
                audit("origin_rejected", method=self.command, path=self.path[:200],
                      reason=reason, origin=self.headers.get("Origin", "")[:200])
                self._json({"error": f"来源校验失败，已拒绝（{reason}）"}, 403)
                return False
        return True

    def do_GET(self):  # noqa: N802
        # 统一解析 path 与 query：原实现直接用 self.path 做等值比较，
        # 一旦带上查询串（如 /api/sessions?q=xx）就匹配不到路由，静默 404。
        _parts = urlsplit(self.path)
        path = _parts.path
        qs = parse_qs(_parts.query)
        if self.path in ("/", "/index.html"):
            html = INDEX_HTML.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # 禁止缓存：index.html 随仓库更新，浏览器缓存旧页面会让用户看不到最新 UI
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif path == "/dashboard":
            # 可观测性面板：把审计日志/降级记录/用量摆到台面上
            if not DASHBOARD_HTML.exists():
                self.send_error(404)
                return
            html = DASHBOARD_HTML.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif path == "/health":
            self._json({"status": "ok"})
        elif path == "/api/mode":
            # 这是"读"接口，但它泄露"本机在跑哪个 provider、有哪些模式可用"
            # （外部评审指出：/api/mode 此前**完全没有鉴权**，是唯一一个
            # 漏掉的读接口）。与 /api/sessions / /api/memory 等保持一致。
            if not self._auth_required():
                return
            self._json({"mode": get_mode(), "available_modes": available_modes()})
        elif path == "/api/sessions":
            if not self._auth_required():
                return
            # 支持 ?q=<关键词>&limit=&offset= 的搜索与分页（原实现只返回固定 50 条）
            self._handle_list_sessions(
                query=(qs.get("q") or [""])[0],
                limit=_int_param(qs, "limit", 50, low=1, high=200),
                offset=_int_param(qs, "offset", 0, low=0, high=100000),
            )
        elif path.startswith("/api/sessions/") and path.endswith("/export"):
            if not self._auth_required():
                return
            thread_id = path[len("/api/sessions/"):-len("/export")]
            if thread_id:
                self._handle_session_export(thread_id, (qs.get("format") or ["md"])[0])
                return
            self.send_error(404)
        elif path.startswith("/api/sessions/") and path.endswith("/messages"):
            if not self._auth_required():
                return
            # /api/sessions/<thread_id>/messages
            thread_id = path[len("/api/sessions/"):-len("/messages")]
            if thread_id:
                self._handle_session_messages(thread_id)
                return
            self.send_error(404)
        elif path == "/api/memory":
            if not self._auth_required():
                return
            self._handle_memory_list()
        elif path == "/api/uploads":
            if not self._auth_required():
                return
            self._handle_uploads_list()
        elif path == "/api/audit":
            # 供 dashboard 读取最近审计记录（只读，不改状态）
            if not self._auth_required():
                return
            self._handle_audit_tail(limit=_int_param(qs, "limit", 100, low=1, high=1000))
        elif path.startswith("/api/run/output"):
            if not self._auth_required():
                return
            self._handle_run_output()
        elif path.startswith("/api/open"):
            if not self._auth_required():
                return
            # /api/open 会调系统默认程序打开文件——这是**有副作用的 GET**。
            # _auth_required 只对 POST/DELETE/PUT/PATCH 做来源校验，而跨站
            # `<img src="http://127.0.0.1:8000/api/open?file=x">` 正好靠 GET 绕过。
            # 这里显式补一次来源校验（现代浏览器对跨站 <img> 会带
            # `Sec-Fetch-Site: cross-site`，会被拒）。
            ok, reason = self._origin_ok()
            if not ok:
                audit("origin_rejected", method="GET", path="/api/open",
                      reason=reason, origin=self.headers.get("Origin", "")[:200])
                self._json({"error": f"来源校验失败，已拒绝（{reason}）"}, 403)
                return
            self._handle_open_file()
        else:
            self.send_error(404)

    def do_DELETE(self):  # noqa: N802
        if not self._auth_required():
            return
        if not self._body_ok():
            return
        # /api/memory?key=<urlencoded>：删除一条全局记忆
        if urlsplit(self.path).path == "/api/memory":
            qs = parse_qs(urlsplit(self.path).query)
            key = (qs.get("key") or [""])[0]
            if key:
                self._handle_memory_delete(key)
                return
            self._json({"error": "缺少 key 参数（DELETE /api/memory?key=<键>）"}, 400)
            return
        # 形如 /api/sessions/<thread_id>
        prefix = "/api/sessions/"
        if self.path.startswith(prefix):
            thread_id = self.path[len(prefix):]
            if thread_id:
                self._handle_delete_session(thread_id)
                return
        # 形如 /api/uploads/<name>
        uprefix = "/api/uploads/"
        if self.path.startswith(uprefix):
            name = unquote(self.path[len(uprefix):])
            if name:
                self._handle_upload_delete(name)
                return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        # 请求体大小在分发入口就拦掉，回 413 而不是让上层误报 400「缺少参数」
        if not self._body_ok():
            return
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
        if self.path == "/api/run/prepare":
            # 交互终端启动前的分级 + 挑战签发（必须先展示命令再执行）
            if not self._auth_required():
                return
            self._handle_run_prepare()
            return

        if self.path == "/api/confirm":
            # 用服务端签发的 nonce 换取一次性批准（取代旧的 /api/approve 自助登记）
            if not self._auth_required():
                return
            self._handle_confirm()
            return

        if self.path == "/api/upload":
            if not self._auth_required():
                return
            self._handle_upload()
            return
        if self.path == "/api/uploads/rename":
            if not self._auth_required():
                return
            self._handle_upload_rename()
            return
        if self.path == "/api/uploads/reindex":
            if not self._auth_required():
                return
            self._handle_uploads_reindex()
            return
        if self.path == "/api/config/test":
            if not self._auth_required():
                return
            self._handle_config_test()
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
        audit("config_update", provider=provider, base_url=base_url or "(preset)",
              model=model or "(preset)")
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
            self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
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

    def _handle_list_sessions(self, query: str = "", limit: int = 50, offset: int = 0):
        """列出会话，支持关键词搜索与分页。

        原实现只返回固定 50 条且无法检索；会话一多就只能靠肉眼翻。
        这里的关键词在「标题 + 会话历史正文」上做大小写不敏感匹配。
        """
        from graph import list_sessions

        sessions = list_sessions(limit=max(limit + offset, 50))
        keyword = (query or "").strip().lower()
        if keyword:
            sessions = [
                s for s in sessions
                if keyword in (s.get("title") or "").lower()
                or keyword in (s.get("thread_id") or "").lower()
            ]
        total = len(sessions)
        page = sessions[offset:offset + limit]
        self._json({
            "sessions": page,
            "total": total,
            "limit": limit,
            "offset": offset,
            "query": query or "",
        })

    def _handle_session_export(self, thread_id: str, fmt: str = "md"):
        """导出会话为 Markdown 或 JSON（便于存档/贴进 issue/做回归语料）。"""
        from graph import get_session_messages

        messages = get_session_messages(thread_id, provider=get_mode())
        fmt = (fmt or "md").lower()
        if fmt == "json":
            payload = json.dumps(
                {"thread_id": thread_id, "messages": messages},
                ensure_ascii=False, indent=2,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{thread_id}.json"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        lines = [f"# 会话 {thread_id}", ""]
        for m in messages:
            who = "我" if m.get("role") == "user" else "助手"
            lines.append(f"**{who}**：{m.get('content', '')}")
            tools = m.get("tools") or []
            if tools:
                lines.append(f"> 调用工具：{', '.join(tools)}")
            lines.append("")
        payload = "\n".join(lines).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{thread_id}.md"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_audit_tail(self, limit: int = 100):
        """读取最近 N 条安全审计记录（dashboard 用；只读开销小）。"""
        from logging_setup import _default_log_dir

        path = Path(_default_log_dir()) / "audit.log"
        if not path.exists():
            self._json({"entries": [], "log_path": str(path)})
            return
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            self._json({"error": f"读取审计日志失败: {exc}"}, 500)
            return
        entries = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        entries.reverse()  # 最新在前
        self._json({"entries": entries, "log_path": str(path), "total_lines": len(lines)})

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

    # ---------- 全局记忆 ----------

    def _handle_memory_list(self):
        """列出全局记忆（供前端展示/排查；也方便用户管控 AI 记住了什么）。"""
        from memory import list_memory

        self._json({"memory": list_memory()})

    def _handle_memory_delete(self, key: str):
        """删除一条全局记忆（用户主动遗忘）。"""
        from memory import forget

        ok = forget(key)
        if not ok:
            self._json({"error": f"记忆里没有 key={key}"}, 404)
            return
        self._json({"ok": True, "key": key, "message": f"已遗忘 {key}"})

    # ---------- 文档上传 ----------

    def _upload_dir(self) -> Path:
        """上传文件目录：config.UPLOADS_DIR（默认 WRITE_DIR/uploads，不存在则创建）。

        目录来源收敛到 config，是为了让 http 层（落盘）与检索层（重建索引）
        指向同一个位置 —— 两边各写一份路径正是"文件在、检索不到"的成因之一。
        """
        from config import UPLOADS_DIR

        d = Path(UPLOADS_DIR).resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _clean_upload_name(name: str) -> str:
        """清洗上传文件名：拒绝路径分隔符/相对路径，仅保留合法 basename 与允许的扩展名。"""
        raw = name or ""
        # 先拒绝任何路径成分（/ \ ..），再取 basename，杜绝 ../../evil.md 逃逸
        if not raw or raw in (".", "..") or "/" in raw or "\\" in raw or raw.startswith("."):
            return None
        if Path(raw).suffix.lower() not in UPLOAD_EXTS:
            return None
        return raw

    def _handle_upload(self):
        """接收上传文档（name + base64 content），保存并加入检索索引。"""
        length = int(self.headers.get("Content-Length", 0))
        if length > UPLOAD_MAX_BODY:
            self._json({"error": "上传文件过大（上限 5 MB）"}, 413)
            return
        try:
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            data = parse_qs(body)
        except Exception:  # noqa: BLE001
            self._json({"error": "请求体解析失败"}, 400)
            return

        name = self._clean_upload_name((data.get("name") or [""])[0])
        if not name:
            self._json({"error": f"文件名不合法或扩展名不受支持（仅支持 {'/'.join(sorted(UPLOAD_EXTS))}）"}, 400)
            return
        try:
            content = base64.b64decode((data.get("content") or [""])[0], validate=True)
        except Exception:  # noqa: BLE001
            self._json({"error": "文件内容编码无效"}, 400)
            return
        if not content:
            self._json({"error": "文件内容为空"}, 400)
            return
        if len(content) > UPLOAD_MAX_FILE:
            self._json({"error": "上传文件过大（上限 5 MB）"}, 413)
            return

        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            self._json({"error": "仅支持 UTF-8 文本文件（.md/.txt/.py/.rst/.html）"}, 400)
            return

        from retriever import add_uploaded_file

        target = self._upload_dir() / name
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            self._json({"error": f"保存失败: {exc}"}, 500)
            return
        chunks = add_uploaded_file(name, text)
        audit("upload", name=name, size=len(content), chunks=chunks)
        self._json({
            "ok": True,
            "name": name,
            "size": len(content),
            "chunks": chunks,
            "message": f"已上传 {name}（{len(content)} 字节，{chunks} 个片段），可直接提问基于该文档的问题",
        })

    def _handle_uploads_list(self):
        """列出已上传文档（含内存索引片段数与磁盘大小）。"""

        from retriever import list_uploaded_files

        indexed = {u["name"]: u["chunks"] for u in list_uploaded_files()}
        items = []
        for p in sorted(self._upload_dir().glob("*")):
            if not p.is_file():
                continue
            try:
                size = p.stat().st_size
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
            except OSError:
                size, mtime = 0, ""
            items.append({
                "name": p.name,
                "size": size,
                "chunks": indexed.get(p.name, 0),
                "uploaded_at": mtime,
            })
        self._json({"uploads": items})

    def _handle_upload_delete(self, name: str):
        """删除已上传文档（磁盘文件 + 内存索引）。"""
        cleaned = self._clean_upload_name(name)
        if not cleaned:
            self._json({"error": "文件名不合法"}, 400)
            return
        from retriever import remove_uploaded_file

        target = self._upload_dir() / cleaned
        removed_index = remove_uploaded_file(cleaned)
        removed_file = False
        try:
            if target.exists():
                target.unlink()
                removed_file = True
        except OSError:
            pass
        if not (removed_index or removed_file):
            self._json({"error": f"未找到上传文档 {cleaned}"}, 404)
            return
        self._json({"ok": True, "name": cleaned, "message": f"已删除 {cleaned}"})

    def _handle_upload_rename(self):
        """重命名已上传文档（磁盘文件 + 内存索引同步改名）。

        易漏点：只改磁盘文件而不动内存索引，会出现"列表里名字变了、
        检索仍命中旧 source"，反之亦然。这里两处一起改。
        """
        from retriever import add_uploaded_file, remove_uploaded_file

        data = self._read_form()
        old = self._clean_upload_name((data.get("name") or [""])[0])
        new = self._clean_upload_name((data.get("new_name") or [""])[0])
        if not old or not new:
            self._json({"error": "name / new_name 不合法或扩展名不受支持"}, 400)
            return
        if old == new:
            self._json({"ok": True, "name": new, "message": "新旧名称相同，无需改名"})
            return
        src = self._upload_dir() / old
        dst = self._upload_dir() / new
        if not src.exists():
            self._json({"error": f"未找到上传文档 {old}"}, 404)
            return
        if dst.exists():
            self._json({"error": f"目标名称已存在: {new}"}, 409)
            return
        try:
            text = src.read_text(encoding="utf-8")
            dst.write_text(text, encoding="utf-8")
            src.unlink()
        except (OSError, UnicodeDecodeError) as exc:
            self._json({"error": f"重命名失败: {exc}"}, 500)
            return
        remove_uploaded_file(old)
        chunks = add_uploaded_file(new, text)
        audit("upload_rename", old_name=old, new_name=new, chunks=chunks)
        self._json({"ok": True, "name": new, "chunks": chunks,
                    "message": f"已重命名 {old} → {new}（{chunks} 个片段）"})

    def _handle_uploads_reindex(self):
        """从磁盘重建上传文档的内存索引（手动兜底入口）。

        正常路径下 `retriever` 会惰性对齐（见 ensure_uploaded_index），
        这个接口用于"用户手动改了 uploads 目录里的文件"之后强制对齐。
        """
        from retriever import ensure_uploaded_index, list_uploaded_files

        loaded = ensure_uploaded_index(self._upload_dir(), force=True)
        files = list_uploaded_files()
        audit("uploads_reindex", loaded=loaded, chunks=sum(f["chunks"] for f in files))
        self._json({
            "ok": True,
            "reindexed": loaded,
            "files": files,
            "message": f"已重建 {loaded} 个文档、{sum(f['chunks'] for f in files)} 个片段的索引",
        })

    def _handle_config_test(self):
        """连通性自检：用当前（或传入）配置向网关发一次最小请求，报告是否通。

        为什么值得单独做一个接口：自定义 OpenAI 兼容网关最容易踩的坑是
        "地址填错/模型名不对/Key 无效"，而这类错误在正式提问时才爆发，
        排查成本高。这里把它变成一个可以主动点的按钮。
        """
        import urllib.error
        import urllib.request

        from config import get_provider_config

        data = self._read_form()
        provider = (data.get("provider") or [get_mode()])[0].strip().lower()
        base_url = (data.get("base_url") or [""])[0].strip()
        api_key = (data.get("api_key") or [""])[0].strip()

        pcfg = get_provider_config(provider)
        base_url = base_url or (pcfg.get("base_url") or "")
        api_key = api_key or (pcfg.get("api_key") or "")
        if not base_url:
            self._json({"ok": False, "error": "缺少 base_url"}, 400)
            return

        blocked_reason = blocked_probe_target(base_url)
        if blocked_reason:
            audit("gateway_probe", provider=provider, base_url=base_url, ok=False,
                  blocked=True, reason=blocked_reason)
            self._json({"ok": False, "error": f"目标地址被拒绝：{blocked_reason}"}, 400)
            return

        url = base_url.rstrip("/") + "/models"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = 200 <= resp.status < 300
                body = resp.read(4096).decode("utf-8", errors="replace")
            audit("gateway_probe", provider=provider, base_url=base_url, ok=ok,
                  status=resp.status)
            self._json({"ok": ok, "status": resp.status, "url": url,
                        "message": "网关连通" if ok else "网关返回非 2xx",
                        "body_preview": body[:500]})
        except urllib.error.HTTPError as exc:
            audit("gateway_probe", provider=provider, base_url=base_url, ok=False,
                  status=exc.code)
            self._json({"ok": False, "status": exc.code, "url": url,
                        "message": f"网关可达但返回 {exc.code}（通常是 Key 或模型名问题）"})
        except Exception as exc:  # noqa: BLE001
            audit("gateway_probe", provider=provider, base_url=base_url, ok=False,
                  error=str(exc)[:200])
            self._json({"ok": False, "url": url, "message": f"无法连通: {exc}"}, 200)

    def _body_ok(self) -> bool:
        """请求体大小校验：在读取前拦掉超限请求并回 **413**。

        原实现由 `_read_form` 静默返回 `{}`，上层于是把它报成
        400「缺少参数」——客户端完全看不出真正原因是"请求体太大"
        （外部评审指出）。这里把判定提到分发入口，错误码才说得清。
        """
        raw = self.headers.get("Content-Length")
        if raw is None:
            return True
        try:
            length = int(raw)
        except (TypeError, ValueError):
            self._json({"error": f"Content-Length 非法: {raw!r}"}, 400)
            return False
        if length > MAX_BODY:
            self._json({"error": f"请求体过大：{length} 字节，上限 {MAX_BODY} 字节"}, 413)
            return False
        return True

    def _read_form(self) -> dict:
        """读取表单并限制大小。失败返回 {}（大小校验见 `_body_ok`）。"""
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
        from urllib.parse import parse_qs as _pq
        from urllib.parse import urlparse

        import runterm

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
            # 建目录后**重新解析并再校验一次**。
            # resolve-then-check 之间存在 TOCTOU 窗口：若某一级父目录在 mkdir
            # 之后、write 之前被换成指向外部的符号链接，第一次校验就失效了；
            # 另外"父目录本身是符号链接、但目标文件当时还不存在"的写法，
            # 单次 resolve 也可能漏判。这里补一次，把窗口收窄到
            # "重新解析 → 写入"之间这一小段（外部评审指出）。
            final = target.resolve()
            if not final.is_relative_to(write_root):
                self._json({"error": "路径超出允许目录（父目录符号链接指向外部）"}, 400)
                return
            final.write_text(content, encoding="utf-8")
            self._json({"ok": True, "path": str(final)})
        except OSError as exc:
            self._json({"error": f"写入失败: {exc}"}, 500)

    def _handle_confirm(self):
        """用户确认高危命令：用**服务端签发**的 nonce 换取一次性批准。

        为什么不是"收一条命令就直接登记"（旧 /api/approve 的做法，外部评审 P0-1）：
        那样等于把"授予批准"做成了自助接口 —— 任何能发请求的一方（前端自己、
        本机其它进程）都能凭空把任意命令登记为"用户已批准"。前端甚至**自动**
        调了一次，于是后端那道闸在真实路径上永远为真地通过。
        现在 nonce 只能由服务端签发（工具层 mint / run/prepare），
        确认时还校验"被确认的命令与展示给用户的命令哈希一致"，无法张冠李戴。
        """
        import approvals

        data = self._read_form()
        nonce = (data.get("nonce") or [""])[0].strip()
        command = (data.get("command") or [""])[0]
        ok, reason = approvals.confirm(nonce, command)
        if not ok:
            audit("approval_rejected", nonce_prefix=nonce[:8], reason=reason,
                  command_preview=command[:120])
            self._json({"ok": False, "error": f"确认失败：{reason}"}, 400)
            return
        self._json({"ok": True, "command": command, "reason": reason})

    def _handle_run_prepare(self):
        """交互终端启动前：分级 + 必要时签发确认挑战。

        前端必须先调这里，并把返回的 command **原样展示**给用户；
        高危命令只有拿到 need_confirm/nonce 才能继续走 /api/confirm。
        于是"前端自己拼一条命令就直接跑"不再成立：没有服务端签发的 nonce，
        confirm 必然失败。
        """
        import approvals
        import runterm

        data = self._read_form()
        command = (data.get("command") or [""])[0].strip()
        if not command:
            self._json({"error": "命令不能为空"}, 400)
            return
        level, reason = runterm.classify(command)
        if level == "blocked":
            audit("runterm_prepare", outcome="blocked", command_preview=command[:120],
                  detail=reason)
            self._json({"error": f"⛔ 已拦截：命令{reason}"}, 403)
            return
        if level == "safe":
            audit("runterm_prepare", outcome="safe", command_preview=command[:120])
            self._json({"ok": True, "level": "safe", "command": command})
            return
        nonce = approvals.mint(command, reason)
        self._json({"need_confirm": True, "level": "high", "nonce": nonce,
                    "command": command, "reason": reason})

    def _handle_open_file(self):
        """前端点击文件名时用系统默认程序打开 WRITE_DIR 内文件。"""
        from urllib.parse import parse_qs as _pq
        from urllib.parse import urlparse

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

    def log_message(self, *args):  # noqa: N802
        """访问日志：恢复输出，但**脱敏**。

        修复的问题：原实现是直接 `pass` —— 所有 HTTP 访问日志被静音，
        出了问题（哪个接口 500、谁在刷 /ask）事后完全查不到。
        但直接恢复默认格式又会把 `X-API-Token` 等敏感头写进日志，
        所以这里改为走 logger 并只记录方法、路径与状态码。
        """
        try:
            # BaseHTTPRequestHandler 调用形式：log_message(format, *args)
            message = args[0] % args[1:] if len(args) > 1 else (args[0] if args else "")
        except Exception:  # noqa: BLE001
            message = " ".join(str(a) for a in args)
        message = re.sub(r"(X-API-Token|token|api_key)=?[^\s&]*", r"\1=***", str(message),
                         flags=re.IGNORECASE)
        logger.info("http %s", message)


def reset_state() -> None:
    """重置进程级状态（测试复位用）：当前模式 + 来源白名单缓存。"""
    global _current_mode
    with _mode_lock:
        _current_mode = None
    reset_origin_cache()


# 视为"本机"的绑定地址：这些地址只在本机可达，不必强制 Token
_LOOPBACK_BIND_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def enforce_token_for_public_bind(host: str) -> str:
    """绑定非回环地址时强制 API Token：未配置就**随机生成并打印**。

    外部评审的点：默认空 token + 文档里的 `docker run -p 0.0.0.0:8000:8000`
    暴露到局域网之后，局域网内任何设备都能先 POST /api/run/prepare、
    再 POST /api/confirm、然后 POST /api/run/start。README 此前只写"**推荐**
    开启 API_TOKEN"——而"推荐"在默认路径上永远不会被打开。安全默认值必须是
    **已开启**：回环绑定（默认）保持零配置体验；一旦对外绑定就自动生成，
    用户从启动日志里取用（前端 ⚙ 面板填入）。
    """
    import secrets

    import config

    if host in _LOOPBACK_BIND_HOSTS:
        return config.API_TOKEN
    if config.API_TOKEN:
        return config.API_TOKEN
    token = secrets.token_urlsafe(32)
    config.API_TOKEN = token
    audit("api_token_autogenerated", bind_host=host, reason="non_loopback_bind")
    print("=" * 72)
    print(f"[安全] 服务绑定在 {host}（非回环地址），且未配置 API_TOKEN。")
    print(f"[安全] 已自动生成临时 Token：{token}")
    print("[安全] 客户端需带请求头 X-API-Token；重启后会重新生成。")
    print("[安全] 需要长期固定请显式配置 .env: API_TOKEN=<你的值>")
    print("=" * 72)
    return token


def run(host: str = "127.0.0.1", port: int = 8000):
    """启动 web 服务（阻塞）。日志同时写控制台与 logs/app.log。"""
    enforce_token_for_public_bind(host)
    log_dir = setup_logging()
    logger.info("网页服务已启动: http://%s:%s", host, port)
    logger.info("应用日志: %s/app.log ；安全审计日志: %s/audit.log", log_dir, log_dir)

    # 启动即把磁盘上的上传文档恢复到内存索引：
    # 否则"重启后上传的文件还在、却检索不到"（retriever 侧虽已惰性兜底，
    # 这里显式做一次可以让启动日志直接体现恢复了几份文档）
    try:
        from retriever import ensure_uploaded_index

        restored = ensure_uploaded_index()
        if restored:
            logger.info("已恢复 %d 个上传文档的检索索引", restored)
    except Exception:  # noqa: BLE001 - 索引恢复失败不应阻塞服务启动
        logger.exception("恢复上传文档索引失败（服务继续启动）")

    # 后台线程定期清理闲置终端会话（防内存泄漏）
    def _sweep_loop():
        import runterm

        while True:
            time.sleep(120)
            try:
                n = runterm.sweep_stale()
                if n:
                    logger.info("清理 %d 个闲置终端会话", n)
            except Exception:  # noqa: BLE001
                logger.exception("清理闲置终端会话失败")

    threading.Thread(target=_sweep_loop, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
