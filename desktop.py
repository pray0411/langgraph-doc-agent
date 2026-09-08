"""Pray 桌面端：内嵌系统 WebView 加载本地 Web UI（pywebview）。

用法:
    python -X utf8 desktop.py            # 启动桌面窗口（端口自动选空闲）
    python -X utf8 desktop.py --port 9000
    python -X utf8 desktop.py --check    # 无窗口自检：起服务→请求首页→退出（CI/测试用）

实现：
- 后台线程运行与 main.py web 相同的 ThreadingHTTPServer（复用 server.Handler，
  含鉴权/CSRF/SSE 全能力），前端零改动（同源访问，Origin 校验天然通过）
- pywebview 用系统 WebView（Windows = Edge WebView2，Win10/11 自带）加载页面，
  无 Chromium/Electron 体积；关窗即关服务退出
- 依赖：pip install pywebview（打包见 Pray.spec 与 README）

注：本模块不 import pywebview（避免无窗口环境报错），仅在真正开窗时导入。
"""
import argparse
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

from server import Handler

# 默认窗口尺寸
WINDOW_SIZE = (1180, 800)


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


def open_desktop_window(port: int = 0) -> None:
    """起服务并打开桌面窗口；关窗后关闭服务。"""
    import webview  # 延迟导入：无桌面环境（CI/服务器）不依赖

    srv, url = start_server(port)
    print(f"Pray 桌面版已启动: {url}（关闭窗口即退出）")
    window = webview.create_window(
        "Pray · 通用 AI Agent",
        url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=(900, 620),
    )
    try:
        webview.start()
    finally:
        srv.shutdown()
        srv.server_close()
        print("已退出。")


def self_check(port: int = 0) -> int:
    """无窗口自检：起服务 → 请求首页确认 200 → 关闭。返回退出码。"""
    srv, url = start_server(port)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            assert resp.status == 200, f"首页返回 {resp.status}"
            body = resp.read().decode("utf-8")
        assert "Pray" in body or "chatArea" in body, "首页内容异常"
        print(f"[self-check] OK: {url} 返回 200，页面 {len(body)} 字节")
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
    args = parser.parse_args()

    if args.check:
        raise SystemExit(self_check(args.port))
    open_desktop_window(args.port)


if __name__ == "__main__":
    main()
