"""Pray 安装程序（单文件 PraySetup.exe）。

为什么自己写而不是用 Inno Setup（外部评审/环境约束）：
    Inno Setup 的下载在本机网络下不可用（GitHub 504、官方源返回空文件），
    而"下载一个安装包、双击就装好"这件事不能因为缺一个打包工具而停住。
    这里用 PyInstaller 把【应用 zip + 安装逻辑】打成一个 exe：
    - 双赢的取舍：体积约 85 MB（内嵌 67 MB 应用包），首次启动需解压几秒；
    - 安装逻辑复用被验证过的 installer/install.ps1，
      避免"GUI 版和命令行版各写一套、慢慢漂移"。

用法（终端用户）：
    双击 PraySetup.exe → 点"安装" → 完成 → 可勾选"安装后启动"。

用法（自动化/CI 验证）：
    PraySetup.exe --silent [--dir <目标目录>] [--no-shortcuts]
    解压内嵌 zip 到临时目录后调用 install.ps1，打印结果并以退出码返回。
"""
import argparse
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

APP_NAME = "Pray"
APP_ZIP = "Pray-1.1.0-win64.zip"   # 构建时内嵌（见 installer/build_installer.py）
# 默认安装位置（提示文案用；真正的默认值在 install.ps1 里）
DEFAULT_TARGET_HINT = "%LOCALAPPDATA%\\Programs\\Pray"


def resource_dir() -> Path:
    """PyInstaller 解包目录 / 源码运行时的脚本目录。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def find_app_zip() -> Path:
    """定位内嵌的应用 zip（兼容版本号变化）。"""
    base = resource_dir()
    exact = base / APP_ZIP
    if exact.exists():
        return exact
    candidates = sorted(base.glob("Pray-*-win64.zip"))
    if not candidates:
        raise FileNotFoundError(f"安装包内缺少应用包（期望 {APP_ZIP}）")
    return candidates[-1]


def extract_payload(dest: Path) -> Path:
    """把内嵌的应用 zip 解压到 dest，返回含 install.ps1 的目录。"""
    with zipfile.ZipFile(find_app_zip()) as zf:
        zf.extractall(dest)
    if not (dest / "install.ps1").exists():
        raise FileNotFoundError("应用包内缺少 install.ps1")
    return dest


def run_install_ps1(src_dir: Path, install_dir: Path | None, shortcuts: bool) -> subprocess.CompletedProcess:
    """调用 install.ps1 完成实际安装（复用命令行版同一套逻辑）。"""
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(src_dir / "install.ps1"), "-NoLaunch",
    ]
    if install_dir:
        cmd += ["-InstallDir", str(install_dir)]
    if not shortcuts:
        cmd.append("-NoShortcuts")
    return subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800,
    )


def silent_install(args) -> int:
    """无界面安装（CI/自动化验证用）。"""
    tmp = Path(tempfile.mkdtemp(prefix="pray-setup-"))
    try:
        print(f"[setup] 解压应用包到 {tmp}")
        src = extract_payload(tmp)
        target = Path(args.dir) if args.dir else None
        hint = str(target) if target else DEFAULT_TARGET_HINT
        print(f"[setup] 调用安装脚本（目标: {hint}）")
        proc = run_install_ps1(src, target, shortcuts=not args.no_shortcuts)
        out = (proc.stdout or "").strip()
        if out:
            print(out)
        if proc.returncode != 0:
            print(f"[setup] 安装失败（exit {proc.returncode}）", file=sys.stderr)
            err = (proc.stderr or "").strip()
            if err:
                print(err, file=sys.stderr)
            return proc.returncode or 1
        print("[setup] 安装完成")
        return 0
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def _msgbox(text: str, title: str, flags: int) -> int:
    """Windows 原生消息框（不依赖 tkinter：便携 Python 往往不带 tkinter）。"""
    import ctypes

    return ctypes.windll.user32.MessageBoxW(0, text, title, flags)


def interactive_install() -> int:
    """双击运行时的交互安装：原生确认框 + 控制台进度。

    为什么不用 tkinter 做漂亮的向导界面：目标 Python 运行时不带 tkinter，
    为了一个安装界面去要求用户预装 GUI 组件不划算。原生 MessageBoxW 同样能
    完成"确认 → 安装 → 完成后询问是否启动"的交互，且零依赖。
    """
    MB_YESNO, MB_ICONQUESTION, MB_ICONINFORMATION = 0x4, 0x20, 0x40
    IDYES = 6

    target_hint = DEFAULT_TARGET_HINT
    if _msgbox(
        f"即将安装 {APP_NAME} 到：\n  {target_hint}\n\n"
        "· 无需管理员权限\n"
        "· 会创建开始菜单与桌面快捷方式\n"
        "· 可在「设置 → 应用」中卸载\n\n是否继续安装？",
        f"{APP_NAME} 安装程序", MB_YESNO | MB_ICONQUESTION,
    ) != IDYES:
        print("[setup] 已取消安装。")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pray-setup-"))
    try:
        print("[setup] 正在解压安装文件…")
        src = extract_payload(tmp)
        print("[setup] 正在安装（复制文件 / 创建快捷方式 / 注册卸载项）…")
        proc = run_install_ps1(src, None, shortcuts=True)
        for line in (proc.stdout or "").splitlines():
            if line.strip():
                print("  " + line.rstrip())
        if proc.returncode != 0:
            print("[setup] 安装失败。", file=sys.stderr)
            err = (proc.stderr or "").strip()
            if err:
                print(err, file=sys.stderr)
            _msgbox(
                "安装失败，请查看命令行窗口中的错误信息。",
                f"{APP_NAME} 安装程序", MB_ICONINFORMATION,
            )
            return proc.returncode or 1
        print("[setup] 安装完成。")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    if _msgbox(
        f"{APP_NAME} 已安装完成。\n\n开始菜单与桌面已创建快捷方式。\n是否立即启动？",
        f"{APP_NAME} 安装程序", MB_YESNO | MB_ICONQUESTION,
    ) == IDYES:
        exe = Path.home() / "AppData" / "Local" / "Programs" / APP_NAME / f"{APP_NAME}.exe"
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
        else:
            print(f"[setup] 未找到 {exe}，请从开始菜单启动。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 安装程序")
    parser.add_argument("--silent", action="store_true", help="无界面安装（自动化/CI）")
    parser.add_argument("--dir", help="自定义安装目录（默认 %%LOCALAPPDATA%%\\Programs\\Pray）")
    parser.add_argument("--no-shortcuts", action="store_true", help="不创建快捷方式")
    args = parser.parse_args()

    if args.silent:
        return silent_install(args)
    return interactive_install()


if __name__ == "__main__":
    sys.exit(main())
