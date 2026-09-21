"""server.py 写入类路由的契约测试（真实 HTTP，起 ThreadingHTTPServer）。

为什么单独一个文件：`test_core.py` 覆盖的是"读"路径（/ask、/api/sessions、
鉴权、CSRF、SSE）。真正会**改磁盘 / 改内存索引 / 起子进程**的是下面这些写路由，
它们此前没有一条端到端用例——而这正是最容易出"接口绿了、数据错了"的地方
（例如只改磁盘不改索引、路径穿越写到目录外、未批准就执行高危命令）。

覆盖清单：
- POST /api/upload            : 正常落盘 + 入索引；超大 body / 超大文件 /
                                非法扩展名 / 路径穿越 / 点文件 / 空内容 /
                                非法 base64 / 非 UTF-8 → 各自状态码
- DELETE /api/uploads/<name>  : 正常删除（磁盘 + 索引）；不存在 → 404；非法名 → 400
- POST /api/uploads/rename    : 改名同步磁盘 + 索引；源不存在 → 404；
                                目标已存在 → 409；同名 → 200；非法名 → 400
- POST /api/uploads/reindex   : 把磁盘上"手动放入"的文件重新纳入索引
- POST /api/run/start         : 破坏性命令 → 400 已拦截；高危命令未批准 →
                                400 NEED_CONFIRM（且不产生进程）；空命令 → 400
- POST /api/run/prepare       : 破坏性 → 403；安全 → 免确认；高危 → need_confirm+nonce
- POST /api/confirm           : 无/伪造/不匹配的 nonce 一律 400；成功后一次性放行
- POST /api/approve           : 已移除 → 404（原"收裸命令即登记"的自助授权接口）
- runterm 集成                : 批准一次性消费（第二次同命令重新要求确认）
- POST /api/run/input|stop    : 缺参数 → 400；会话不存在 → 200 + error 字段
- GET  /api/run/output        : 缺 session_id → 400
- POST /api/run/write         : 路径穿越 → 400；合法写入落在 WRITE_DIR 内
- GET  /api/open              : 缺参数 → 400；路径穿越 / 文件不存在 → 拒绝且不打开
- POST /api/config/test       : 缺 base_url → 400；指向本地服务 → 回传探测结果
- 跨站与鉴权                  : 跨站 POST / DELETE → 403；配 token 后无 token → 401
"""
import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest


def _approve(command: str) -> bool:
    """测试辅助：与前端**完全相同的两步**（服务端签发 challenge → confirm 换批准）。

    不再直接"登记一条批准"——那正是外部评审指出的自助授权原语：旧 /api/approve
    接受裸命令就登记为"用户已批准"，前端甚至自动调了一次，于是闸门形同不存在。
    测试改走真实路径，这条链才被测住。
    """
    import approvals

    return approvals.confirm(approvals.mint(command, "test"), command)[0]


# ---------- 测试夹具：真实服务 + 临时 WRITE_DIR / UPLOADS_DIR ----------

@pytest.fixture()
def web(monkeypatch, tmp_path):
    """起真实 web 服务，并把 WRITE_DIR / UPLOADS_DIR 指向临时目录。

    两个目录都必须重定向到 tmp_path，否则用例会往仓库的 generated/ 里写文件：
    既污染工作区，也让"落盘断言"依赖仓库当前状态（本机有历史文件时必红）。
    """
    from http.server import ThreadingHTTPServer

    import approvals
    import config
    import retriever
    import runterm
    import server as server_mod

    write_dir = tmp_path / "generated"
    uploads_dir = write_dir / "uploads"
    write_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # server / tools / runterm / retriever 都在**调用点** `from config import X`，
    # 因此改模块属性即对所有路径生效（这也是"配置即时生效"修复带来的红利）。
    monkeypatch.setattr(config, "WRITE_DIR", write_dir)
    monkeypatch.setattr(config, "UPLOADS_DIR", uploads_dir)

    retriever.reset_state()
    runterm.reset_state()
    approvals.reset_state()
    server_mod.reset_state()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        yield SimpleNamespace(
            base=f"http://127.0.0.1:{port}",
            write_dir=write_dir,
            uploads_dir=uploads_dir,
            module=server_mod,
        )
    finally:
        srv.shutdown()
        srv.server_close()
        retriever.reset_state()
        runterm.reset_state()
        approvals.reset_state()


# ---------- HTTP 小工具 ----------

def _send(req, timeout=15):
    """发请求，返回 (状态码, 响应体文本)，HTTPError 也当正常返回值。"""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _post(base, endpoint, data, token=None, origin=None):
    body = urllib.parse.urlencode(data).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Connection": "close"}
    if token:
        headers["X-API-Token"] = token
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(base + endpoint, data=body, method="POST", headers=headers)
    return _send(req)


def _delete(base, endpoint, token=None, origin=None):
    headers = {"Connection": "close"}
    if token:
        headers["X-API-Token"] = token
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(base + endpoint, method="DELETE", headers=headers)
    return _send(req)


def _get(base, endpoint, token=None):
    headers = {"Connection": "close"}
    if token:
        headers["X-API-Token"] = token
    req = urllib.request.Request(base + endpoint, method="GET", headers=headers)
    return _send(req)


def _upload(base, name, content_bytes, *, declared_length=None, token=None, origin=None):
    """构造 /api/upload 请求。

    declared_length：显式覆盖 Content-Length（用于在不真正发送 8 MB 的前提下
    触发"请求体过大"分支——服务端在读取前就按该 header 拒绝）。
    """
    payload = urllib.parse.urlencode({
        "name": name,
        "content": base64.b64encode(content_bytes).decode(),
    }).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Connection": "close"}
    if declared_length is not None:
        headers["Content-Length"] = str(declared_length)
    if token:
        headers["X-API-Token"] = token
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(base + "/api/upload", data=payload, method="POST",
                                headers=headers)
    return _send(req)


def _uploaded_names():
    from retriever import list_uploaded_files

    return {u["name"] for u in list_uploaded_files()}


# ---------- /api/upload ----------

def test_upload_valid_file_lands_on_disk_and_index(web):
    """合法上传：文件落盘 + 进入内存索引（两个位置都要对）。"""
    status, body = _upload(
        web.base, "note.md", "# 标题\n\n这是上传的文档，内容关于混合检索（BM25 + 向量）。".encode()
    )
    data = json.loads(body)
    assert status == 200, body
    assert data["ok"] is True
    assert data["chunks"] >= 1, "至少切出一个片段"

    # 1) 磁盘上确实存在，且内容按 UTF-8 原样写入
    on_disk = web.uploads_dir / "note.md"
    assert on_disk.exists()
    assert "混合检索" in on_disk.read_text(encoding="utf-8")

    # 2) 内存索引里也查得到（防止"文件在、检索不到"）
    assert "note.md" in _uploaded_names()


def test_uploads_list_reports_disk_size_and_index_chunks(web):
    """GET /api/uploads：同时反映磁盘大小与索引片段数。"""
    _upload(web.base, "a.md", "内容甲，关于部署与容器化。".encode())
    status, body = _get(web.base, "/api/uploads")
    data = json.loads(body)
    assert status == 200
    items = {i["name"]: i for i in data["uploads"]}
    assert "a.md" in items
    assert items["a.md"]["size"] > 0
    assert items["a.md"]["chunks"] >= 1


@pytest.mark.parametrize("bad_name", [
    "../evil.md",       # 相对路径穿越
    "sub/dir.md",       # 含正斜杠
    "sub\\dir.md",      # 含反斜杠（Windows 分隔符）
    ".hidden.md",       # 点文件
    "evil.exe",         # 扩展名不在白名单
    "noext",            # 无扩展名
    "",                 # 空名
])
def test_upload_rejects_illegal_names(web, bad_name):
    """文件名清洗：路径成分 / 点文件 / 非白名单扩展名一律 400。"""
    status, body = _upload(web.base, bad_name, b"content")
    assert status == 400, f"{bad_name!r} 应被拒绝，实际 {status}"
    assert "文件名不合法" in json.loads(body)["error"]
    # 关键：越界名不能真的落盘
    assert not any(web.uploads_dir.iterdir())


def test_upload_rejects_empty_content(web):
    """空内容 → 400（避免往索引里塞空文档）。"""
    status, body = _upload(web.base, "empty.md", b"")
    assert status == 400
    assert "为空" in json.loads(body)["error"]


def test_upload_rejects_invalid_base64(web):
    """content 不是合法 base64 → 400。"""
    status, body = _post(web.base, "/api/upload",
                         {"name": "x.md", "content": "!!!not-base64!!!"})
    assert status == 400
    assert "编码无效" in json.loads(body)["error"]


def test_upload_rejects_non_utf8_content(web):
    """非 UTF-8 字节 → 400（提示仅支持文本扩展名）。"""
    status, body = _upload(web.base, "bin.md", b"\xff\xfe\x00\x01not-utf8")
    assert status == 400
    assert "UTF-8" in json.loads(body)["error"]


def test_upload_rejects_oversize_body(web):
    """Content-Length 超过 UPLOAD_MAX_BODY → 413（读取前即拒绝）。"""
    status, _ = _upload(web.base, "big.md", b"x",
                        declared_length=web.module.UPLOAD_MAX_BODY + 1)
    assert status == 413


def test_upload_rejects_oversize_content(web, monkeypatch):
    """解码后内容超过 UPLOAD_MAX_FILE → 413（下压阈值以免真的造 5 MB）。"""
    monkeypatch.setattr(web.module, "UPLOAD_MAX_FILE", 8)
    status, body = _upload(web.base, "big.md", b"0123456789")  # 10 字节 > 8
    assert status == 413
    assert "过大" in json.loads(body)["error"]


# ---------- DELETE /api/uploads/<name> ----------

def test_upload_delete_removes_disk_and_index(web):
    """删除上传文档：磁盘文件与内存索引必须同时消失。"""
    _upload(web.base, "d.md", "要删掉的内容，关于安全审计。".encode())
    assert (web.uploads_dir / "d.md").exists()

    status, body = _delete(web.base, "/api/uploads/d.md")
    assert status == 200 and json.loads(body)["ok"] is True
    assert not (web.uploads_dir / "d.md").exists()
    assert "d.md" not in _uploaded_names()


def test_upload_delete_missing_returns_404(web):
    status, _ = _delete(web.base, "/api/uploads/ghost.md")
    assert status == 404


def test_upload_delete_illegal_name_returns_400(web):
    status, body = _delete(web.base, "/api/uploads/evil.exe")
    assert status == 400
    assert "文件名不合法" in json.loads(body)["error"]


# ---------- POST /api/uploads/rename ----------

def test_upload_rename_syncs_disk_and_index(web):
    """改名：磁盘文件名与内存索引 source 必须一起改（只改一边就会"名不对实"）。"""
    _upload(web.base, "old.md", "重命名测试内容，关于检索质量评估。".encode())
    status, body = _post(web.base, "/api/uploads/rename",
                         {"name": "old.md", "new_name": "new.md"})
    data = json.loads(body)
    assert status == 200, body
    assert data["name"] == "new.md"
    assert not (web.uploads_dir / "old.md").exists()
    assert (web.uploads_dir / "new.md").exists()

    names = _uploaded_names()
    assert "new.md" in names
    assert "old.md" not in names


def test_upload_rename_missing_source_returns_404(web):
    status, _ = _post(web.base, "/api/uploads/rename",
                      {"name": "ghost.md", "new_name": "x.md"})
    assert status == 404


def test_upload_rename_target_exists_returns_409(web):
    """目标名已存在 → 409，且不能覆盖已有文件。"""
    _upload(web.base, "a.md", "甲".encode())
    _upload(web.base, "b.md", "乙".encode())
    status, _ = _post(web.base, "/api/uploads/rename",
                      {"name": "a.md", "new_name": "b.md"})
    assert status == 409
    assert (web.uploads_dir / "a.md").exists()
    assert (web.uploads_dir / "b.md").read_text(encoding="utf-8") == "乙"


def test_upload_rename_same_name_is_noop(web):
    _upload(web.base, "same.md", "同".encode())
    status, body = _post(web.base, "/api/uploads/rename",
                         {"name": "same.md", "new_name": "same.md"})
    assert status == 200
    assert "无需改名" in json.loads(body)["message"]


def test_upload_rename_illegal_new_name_returns_400(web):
    _upload(web.base, "src.md", "内容".encode())
    status, _ = _post(web.base, "/api/uploads/rename",
                      {"name": "src.md", "new_name": "hack.exe"})
    assert status == 400


# ---------- POST /api/uploads/reindex ----------

def test_uploads_reindex_picks_up_manually_placed_file(web):
    """磁盘上有文件、内存索引没有时，reindex 应把它重新纳入（手动兜底入口）。"""
    (web.uploads_dir / "manual.md").write_text("手动放入的文档，关于可观测性。", encoding="utf-8")

    from retriever import reset_state as reset_retriever

    reset_retriever()  # 模拟"索引被清空 / 服务重启"后的状态

    status, body = _post(web.base, "/api/uploads/reindex", {})
    data = json.loads(body)
    assert status == 200, body
    assert data["ok"] is True
    assert data["reindexed"] >= 1
    assert "manual.md" in {f["name"] for f in data["files"]}


# ---------- /api/run/* 与高危命令审批 ----------

def test_run_start_empty_command_returns_400(web):
    status, _ = _post(web.base, "/api/run/start", {"command": ""})
    assert status == 400


def test_run_start_blocked_command_returns_400(web):
    """破坏性命令（rm -rf /）走黑名单，直接 400，且不产生任何进程。"""
    import runterm

    status, body = _post(web.base, "/api/run/start", {"command": "rm -rf /"})
    assert status == 400
    assert "已拦截" in json.loads(body)["error"]
    assert runterm.list_active() == 0


def test_run_start_high_risk_needs_confirm_and_spawns_nothing(web):
    """高危命令未批准 → 400 NEED_CONFIRM，且不启动进程。"""
    import runterm

    status, body = _post(web.base, "/api/run/start", {"command": "del __nope__.txt"})
    assert status == 400
    assert "NEED_CONFIRM" in json.loads(body)["error"]
    assert runterm.list_active() == 0, "未批准的请求不得起子进程"


def test_prepare_confirm_then_run_start_reaches_runterm(web, monkeypatch):
    """真实三步：prepare 拿 nonce → confirm 换批准 → run/start 才放行。

    第 1 步必须用**真实的** `runterm.start`：审批门禁就实现在它内部，
    提前把它换成桩会把被测对象一起换掉（这一步曾写成"先打桩再断言 400"，
    于是断言的是桩的返回值，测了个空 —— 门禁没被覆盖到）。
    只有第 4 步（验证"HTTP 层 → runterm"这段接线）才打桩，避免真起子进程。
    真实 `runterm.start` 在批准后确实放行，由
    `test_security_model.py::test_runterm_enforces_queue_and_line_limits` 覆盖。
    """
    import runterm

    cmd = "del __nope__.txt"
    # 1) 直接 start：没有任何批准 → 拒绝，且不起进程（真实门禁）
    s0, _ = _post(web.base, "/api/run/start", {"command": cmd})
    assert s0 == 400
    assert runterm.list_active() == 0, "未批准的请求不得起子进程"

    # 2) prepare：高危 → 结构化 need_confirm + 服务端签发的 nonce
    s1, b1 = _post(web.base, "/api/run/prepare", {"command": cmd})
    d1 = json.loads(b1)
    assert s1 == 200 and d1["need_confirm"] is True and d1["nonce"]
    assert d1["command"] == cmd, "要展示给用户的命令必须由服务端返回"

    # 3) confirm：用 nonce 换取一次性批准
    s2, b2 = _post(web.base, "/api/confirm", {"nonce": d1["nonce"], "command": cmd})
    assert s2 == 200 and json.loads(b2)["ok"] is True

    # 4) start 这才真的放行（此处才打桩：只验证 HTTP 层到 runterm 的接线）
    seen = []
    monkeypatch.setattr(runterm, "start",
                        lambda c: (seen.append(c) or {"session_id": "s-test"}))
    s3, b3 = _post(web.base, "/api/run/start", {"command": cmd})
    assert s3 == 200, b3
    assert json.loads(b3)["session_id"] == "s-test"
    assert seen == [cmd]


def test_confirm_without_server_nonce_cannot_grant_approval(web):
    """核心回归（外部评审 P0-1）：**不存在"自助授予批准"的路径**。

    旧 /api/approve 接受任意命令并直接登记为"用户已批准"——前端甚至自动调了
    一次，于是后端那道闸在真实使用路径上永远为真地通过。
    现在批准必须先有服务端签发的 nonce，且 nonce 与命令哈希绑定：
    伪装成旧调用形态、伪造 nonce、空 nonce 都必须失败。
    """
    import approvals
    import runterm

    cmd = "del __nope__.txt"
    for payload in (
        {"command": cmd},                              # 旧接口的调用形态（裸命令）
        {"command": cmd, "nonce": "forged-nonce"},     # 伪造 nonce
        {"command": cmd, "nonce": ""},                 # 空 nonce
    ):
        status, body = _post(web.base, "/api/confirm", payload)
        assert status == 400, f"{payload} 不该被接受：{body}"
        assert json.loads(body)["ok"] is False

    # 确认全失败之后，直接 start 依然被拒、依然没有进程、也不该留下批准记录
    s, b = _post(web.base, "/api/run/start", {"command": cmd})
    assert s == 400 and "NEED_CONFIRM" in json.loads(b)["error"]
    assert runterm.list_active() == 0
    assert approvals.pending_count() == 0, "不得留下任何批准记录"


def test_confirm_rejects_command_swap(web):
    """nonce 与命令绑定：拿 A 命令的 nonce 去批准 B 命令必须失败。"""
    cmd_a = "del __a__.txt"
    _, b1 = _post(web.base, "/api/run/prepare", {"command": cmd_a})
    nonce = json.loads(b1)["nonce"]
    status, body = _post(web.base, "/api/confirm",
                         {"nonce": nonce, "command": "del __b__.txt"})
    assert status == 400
    assert "不匹配" in json.loads(body)["error"]


def test_prepare_rejects_destructive_command(web):
    """prepare 对破坏性命令直接 403，且不签发任何挑战。"""
    import approvals

    status, body = _post(web.base, "/api/run/prepare", {"command": "rm -rf /"})
    assert status == 403
    assert "已拦截" in json.loads(body)["error"]
    assert approvals.challenge_count() == 0


def test_prepare_safe_command_needs_no_confirmation(web):
    """只读命令 prepare 直接放行（level=safe），不弹窗。"""
    status, body = _post(web.base, "/api/run/prepare", {"command": "dir"})
    data = json.loads(body)
    assert status == 200 and data["ok"] is True and data["level"] == "safe"


def test_confirm_empty_command_returns_400(web):
    status, _ = _post(web.base, "/api/confirm", {"nonce": "x", "command": ""})
    assert status == 400


def test_run_input_missing_session_returns_400(web):
    status, _ = _post(web.base, "/api/run/input", {"session_id": "", "text": "x"})
    assert status == 400


def test_run_input_unknown_session_reports_error(web):
    status, body = _post(web.base, "/api/run/input", {"session_id": "nope", "text": "x"})
    assert status == 200
    assert "会话不存在" in json.loads(body).get("error", "")


def test_run_stop_missing_session_returns_400(web):
    status, _ = _post(web.base, "/api/run/stop", {"session_id": ""})
    assert status == 400


def test_run_stop_unknown_session_is_idempotent(web):
    """停止不存在的会话应幂等成功（否则前端重复点会报错）。"""
    status, body = _post(web.base, "/api/run/stop", {"session_id": "nope"})
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_run_output_missing_session_returns_400(web):
    status, _ = _get(web.base, "/api/run/output")
    assert status == 400


# ---------- POST /api/run/write ----------

def test_run_write_rejects_path_escape(web):
    """写文件必须夹在 WRITE_DIR 内，../ 逃逸 → 400。"""
    status, body = _post(web.base, "/api/run/write",
                         {"path": "../outside.py", "content": "print('escaped')"})
    assert status == 400
    # 断言"被拒绝"这个不变式，而不是某句具体文案：越界原因有多种
    # （`..` 段、绝对路径、盘符、UNC），文案由守卫给出，不该被测试钉死。
    err = json.loads(body)["error"]
    assert ("超出允许目录" in err) or ("越界" in err) or ("不接受" in err), err
    # 逃逸目标绝不能真的被创建
    assert not (web.write_dir.parent / "outside.py").exists()


def test_run_write_writes_inside_write_dir(web):
    status, body = _post(web.base, "/api/run/write",
                         {"path": "sub/demo.py", "content": "print(1)\n"})
    data = json.loads(body)
    assert status == 200 and data["ok"] is True
    written = Path(data["path"])
    assert written.is_relative_to(web.write_dir.resolve())
    assert written.read_text(encoding="utf-8") == "print(1)\n"


def test_run_write_missing_path_returns_400(web):
    status, _ = _post(web.base, "/api/run/write", {"path": "", "content": "x"})
    assert status == 400


# ---------- GET /api/open ----------

def test_open_missing_param_returns_400(web):
    status, _ = _get(web.base, "/api/open")
    assert status == 400


def test_open_path_escape_is_refused(web):
    """路径穿越：返回拒绝说明，且绝不调用系统打开（否则等于任意文件外泄入口）。"""
    q = urllib.parse.quote("../secret.txt")
    status, body = _get(web.base, f"/api/open?file={q}")
    assert status == 200
    assert "拒绝打开" in json.loads(body)["message"]


def test_open_missing_file_is_reported(web):
    """目录内的合法名但文件不存在：报告不存在（不抛异常、不打开）。"""
    q = urllib.parse.quote("ghost.html")
    status, body = _get(web.base, f"/api/open?file={q}")
    assert status == 200
    assert "文件不存在" in json.loads(body)["message"]


# ---------- POST /api/config/test ----------

def test_config_test_missing_base_url_returns_400(web):
    """未知服务商没有预设 base_url → 400。"""
    status, body = _post(web.base, "/api/config/test", {"provider": "unknown_provider_zzz"})
    assert status == 400
    assert "base_url" in json.loads(body)["error"]


def test_config_test_probes_reachable_gateway(web):
    """指向本测试服务：/models 不存在 → 可达但非 2xx，回传状态码 404。"""
    status, body = _post(web.base, "/api/config/test",
                         {"provider": "deepseek", "base_url": web.base, "api_key": "k"})
    data = json.loads(body)
    assert status == 200
    assert data["ok"] is False
    assert data.get("status") == 404
    assert data["url"].endswith("/models")


# ---------- 跨站与鉴权（写路由的重点防线） ----------

def test_upload_csrf_rejects_cross_origin(web):
    """跨站 POST /api/upload 必须 403（否则恶意页面可往你机器上塞文件）。"""
    status, body = _upload(web.base, "x.md", b"hi", origin="http://evil.com")
    assert status == 403
    assert "来源校验失败" in body
    assert not any(web.uploads_dir.iterdir())


def test_delete_csrf_rejects_cross_origin(web):
    status, body = _delete(web.base, "/api/uploads/x.md", origin="http://evil.com")
    assert status == 403
    assert "来源校验失败" in body


def test_upload_requires_token_when_configured(web, monkeypatch):
    """配置 API_TOKEN 后：无 token / 错 token → 401，正确 token → 200。"""
    import config

    monkeypatch.setattr(config, "API_TOKEN", "s3cret")

    s1, _ = _upload(web.base, "x.md", b"hi")
    assert s1 == 401
    s2, _ = _upload(web.base, "x.md", b"hi", token="wrong")
    assert s2 == 401
    s3, body = _upload(web.base, "ok.md", b"hi", token="s3cret")
    assert s3 == 200, body


# ---------- runterm 审批门禁（单元级：不经过 HTTP） ----------

def test_runterm_start_gate_approval_and_one_time_consume(monkeypatch, tmp_path):
    """runterm.start 的完整门禁：未批准拒绝 → 批准放行 → 一次性消费。

    这是"模型/前端能否绕过用户确认"的核心防线，必须独立验证：
    - 未批准：返回 NEED_CONFIRM，且**不消费**（用户还没点确认）
    - 批准后：放行并**消费**（同一条命令的批准只生效一次）
    - 再调用：重新要求确认（不能拿一条批准重复执行）
    """
    import approvals
    import config
    import runterm

    monkeypatch.setattr(config, "WRITE_DIR", tmp_path)
    approvals.reset_state()
    runterm.reset_state()
    try:
        cmd = "del __nope__.txt"

        r1 = runterm.start(cmd)
        assert "error" in r1 and "NEED_CONFIRM" in r1["error"]

        _approve(cmd)
        r2 = runterm.start(cmd)
        assert "session_id" in r2, f"批准后应放行，实际 {r2}"

        r3 = runterm.start(cmd)
        assert "error" in r3 and "NEED_CONFIRM" in r3["error"], "批准必须一次性消费"
    finally:
        runterm.reset_state()
        approvals.reset_state()


def test_runterm_start_blocked_command_is_refused(monkeypatch, tmp_path):
    """runterm 同样套用黑名单：破坏性命令即使"已批准"也拒绝。"""
    import approvals
    import config
    import runterm

    monkeypatch.setattr(config, "WRITE_DIR", tmp_path)
    approvals.reset_state()
    runterm.reset_state()
    try:
        cmd = "rm -rf /"
        _approve(cmd)  # 即便有批准记录
        result = runterm.start(cmd)
        assert "error" in result
        assert "已拦截" in result["error"]
        assert runterm.list_active() == 0
    finally:
        runterm.reset_state()
        approvals.reset_state()
