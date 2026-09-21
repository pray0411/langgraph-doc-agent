"""Pray 桌面端：内嵌系统 WebView 加载本地 Web UI（pywebview）。

用法:
    python -X utf8 desktop.py            # 启动桌面窗口（端口自动选空闲）
    python -X utf8 desktop.py --port 9000
    python -X utf8 desktop.py --check    # 无窗口自检：起服务→请求首页→退出（CI/测试用）
    python -X utf8 desktop.py --no-update-check  # 跳过启动时的版本检查

实现：
- 后台线程运行与 main.py web 相同的 ThreadingHTTPServer（复用 server.Handler，
  含鉴权/CSRF/SSE 全能力），前端零改动（同源访问，Origin 校验天然通过）
- pywebview 用系统 WebView（Windows = Edge WebView2，Win10/11 自带）加载页面，
  无 Chromium/Electron 体积；关窗即关服务退出
- 更新检查：启动后异步查询 GitHub Releases 最新版（对比 APP_VERSION），
  有新版弹系统提示框（仅提示 + 引导下载，不做自动替换）
- 依赖：pip install pywebview（打包见 Pray.spec 与 README）

注：本模块不 import pywebview（避免无窗口环境报错），仅在真正开窗时导入。
"""
import argparse
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

# 版本号**单一来源**：仓库根目录 VERSION 文件 → config.APP_VERSION。
# 不要在桌面端硬编码 —— 此前 desktop.py("1.1.0")、pyproject.toml("0.9.0")
# 与 Release tag 三处各写一个，随时会不一致。
from config import APP_VERSION  # noqa: E402
from server import Handler

# 默认窗口尺寸
WINDOW_SIZE = (1180, 800)

# 与 GitHub Releases 的 tag（如 v1.1.0）比较触发更新提示。
UPDATE_REPO = "pray0411/langgraph-doc-agent"


def _parse_version(v: str) -> tuple:
    """把 'v1.2.3' / '1.10' 解析为可比较数字元组；无法解析返回 (0,)。"""
    digits = []
    for seg in (v or "").lstrip("vV").replace("-", ".").split("."):
        try:
            digits.append(int(seg))
        except ValueError:
            break
    return tuple(digits) if digits else (0,)


def check_for_update(timeout: float = 6.0) -> dict | None:
    """查询 GitHub Releases 最新版；返回 {version, url} 或 None（无更新/无 release/失败）。

    任何异常都静默返回 None：更新检查失败绝不能阻碍主程序使用。
    """
    api = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
    try:
        req = urllib.request.Request(
            api,
            headers={
                "User-Agent": f"Pray-Desktop/{APP_VERSION}",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        latest = (data.get("tag_name") or "").lstrip("vV")
        if not latest:
            return None
        if _parse_version(latest) > _parse_version(APP_VERSION):
            return {
                "version": latest,
                "url": data.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases",
            }
        return None
    except Exception:  # noqa: BLE001 - 网络/限流/无 release 均静默
        return None


def start_server(port: int = 0):
    """后台启动 Web 服务（port=0 自动选空闲端口），返回 (server, url)。"""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    real_port = srv.server_address[1]

    def _serve():
        try:
            srv.serve_forever()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_serve, daemon=True).start()

    # 后台定期清理闲置交互终端会话（与 server.run 一致的防泄漏）
    def _sweep_loop():
        import runterm

        while True:
            time.sleep(120)
            try:
                runterm.sweep_stale()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_sweep_loop, daemon=True).start()
    return srv, f"http://127.0.0.1:{real_port}/"


def open_desktop_window(port: int = 0, update_check: bool = True) -> None:
    """起服务并打开桌面窗口；关窗后关闭服务。"""
    import webview  # 延迟导入：无桌面环境（CI/服务器）不依赖

    srv, url = start_server(port)
    print(f"Pray 桌面版已启动: {url}（关闭窗口即退出）")
    # 这里不接返回值：pywebview 的窗口对象只在 webview.start() 期间有效，
    # 赋值给局部变量不会被使用（ruff F841）。需要窗口句柄时应显式保留并说明用途。
    webview.create_window(
        "Pray · 通用 AI Agent",
        url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=(900, 620),
    )
    if update_check:
        threading.Thread(target=_update_check_worker, daemon=True).start()
    try:
        webview.start()
    finally:
        srv.shutdown()
        srv.server_close()
        print("已退出。")


def _update_check_worker():
    """后台检查更新：发现新版弹系统提示框，确认则打开下载页（线程无关，用 Win32 消息框）。"""
    info = check_for_update()
    if not info:
        return
    try:
        import ctypes
        import webbrowser

        # MB_YESNO | MB_ICONINFORMATION | MB_DEFBUTTON2(默认"否")
        msg = (
            f"发现新版本 Pray v{info['version']}（当前 v{APP_VERSION}）\n\n"
            "是否前往 GitHub Releases 下载？\n（本程序不会自动替换自身）"
        )
        ans = ctypes.windll.user32.MessageBoxW(
            0, msg, "Pray 更新", 0x00000004 | 0x00000040 | 0x00000100
        )
        if ans == 6:  # IDYES
            webbrowser.open(info["url"])
    except Exception:  # noqa: BLE001
        pass


def self_check(port: int = 0) -> int:
    """无窗口自检：起服务 → 请求首页确认 200 → 打印配置摘要 → 关闭。返回退出码。

    打印 provider 与"API Key 是否已配置"（不回显密钥本身）以及数据目录，
    便于用户排查"exe 有没有读到我放在旁边的 .env"。
    """
    srv, url = start_server(port)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            assert resp.status == 200, f"首页返回 {resp.status}"
            body = resp.read().decode("utf-8")
        assert "Pray" in body or "chatArea" in body, "首页内容异常"
        print(f"[self-check] OK: {url} 返回 200，页面 {len(body)} 字节")
        try:
            from config import APP_VERSION, BASE_DIR, LLM_PROVIDER, get_provider_config

            cfg = get_provider_config(LLM_PROVIDER)
            key_state = "已配置" if (cfg.get("api_key") or "").strip() else "缺失"
            print(f"[self-check] 版本 {APP_VERSION} | provider={LLM_PROVIDER} | API Key={key_state}")
            print(f"[self-check] 数据目录 {BASE_DIR}")
        except Exception as exc:  # noqa: BLE001 - 自检摘要失败不影响通过判定
            print(f"[self-check] 配置摘要不可用: {exc}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[self-check] FAILED: {exc}")
        return 1
    finally:
        srv.shutdown()
        srv.server_close()


def main():
    parser = argparse.ArgumentParser(description="Pray 桌面端（pywebview 内嵌本地 Web UI）")
    parser.add_argument("--port", type=int, default=0, help="服务端口（默认 0 = 自动选空闲端口）")
    parser.add_argument("--check", action="store_true", help="无窗口自检后退出（测试用）")
    parser.add_argument("--no-update-check", action="store_true", help="跳过启动时的版本检查")
    args = parser.parse_args()

    if args.check:
        raise SystemExit(self_check(args.port))
    open_desktop_window(args.port, update_check=not args.no_update_check)


if __name__ == "__main__":
    main()
