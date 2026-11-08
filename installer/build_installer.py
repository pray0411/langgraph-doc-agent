"""构建 Pray 的 Windows 分发产物：应用 zip + 单文件安装器 PraySetup.exe。

一条命令产出"发给别人就能装"的两个文件：

    dist/Pray-<VERSION>-win64.zip   解压后双击 install.bat（或 install.ps1）
    dist/PraySetup.exe              单文件安装器，双击即装（内嵌上面的 zip）

为什么要有这个脚本：发布流程里最容易出错的是"人肉复制"——
漏了 VERSION、把 .env（含 API Key）一起打进去、zip 里的 install.ps1
和仓库里的版本不一致。这些错误没有命令行会报错，只有用户装上才发现。
所以把打包固化成一步，本地和 CI 走同一份代码。

前置条件：先构建应用本体（dist/Pray/Pray.exe）：

    python -m PyInstaller --clean --noconfirm Pray.spec

用法：

    python installer/build_installer.py            # 构建 zip + PraySetup.exe
    python installer/build_installer.py --zip-only # 只出 zip（CI 快速校验）
    python installer/build_installer.py --keep-stage   # 保留 dist/release_stage 便于排查

退出码：0 成功，非 0 失败（CI 直接据此判定）。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_DIR = DIST / "Pray"
STAGE = DIST / "release_stage"
INSTALLER_DIR = ROOT / "installer"

# 绝不能进分发包的东西。与 installer/install.ps1 的 skip 列表保持一致：
#   - .env       含用户 API Key，打进去等于把密钥发给所有人
#   - data/generated/logs/index  用户私有运行时数据
#   - __pycache__/*.pyc 构建残留
SKIP_NAMES = {".env", "data", "generated", "logs", "index", "__pycache__"}
SKIP_SUFFIXES = {".pyc"}

# 分发包顶层的额外文件（与 exe 一起组成可安装的包）。
EXTRA_TOP_LEVEL = [ROOT / ".env.example", INSTALLER_DIR / "使用说明.txt"]


def read_version() -> str:
    version_file = ROOT / "VERSION"
    if not version_file.exists():
        raise SystemExit(f"[build] 缺少 VERSION 文件：{version_file}")
    version = version_file.read_text(encoding="utf-8").strip()
    if not version:
        raise SystemExit("[build] VERSION 文件为空")
    return version


def stage_release(version: str) -> Path:
    """把 dist/Pray + 安装脚本整理成 dist/release_stage。"""
    if not (APP_DIR / "Pray.exe").exists():
        raise SystemExit(
            f"[build] 未找到 {APP_DIR / 'Pray.exe'}，请先运行：\n"
            "        python -m PyInstaller --clean --noconfirm Pray.spec"
        )
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    copied = 0
    for item in sorted(APP_DIR.iterdir()):
        if item.name in SKIP_NAMES or item.suffix in SKIP_SUFFIXES:
            print(f"[build] 跳过（用户数据/缓存）: {item.name}")
            continue
        dest = STAGE / item.name
        if item.is_dir():
            shutil.copytree(item, dest, ignore=_ignore_user_data)
        else:
            shutil.copy2(item, dest)
        copied += 1

    # 安装脚本与说明来自 installer/：保证分发包里的脚本就是仓库里被验证过的那份
    for name in ["install.ps1", "install.bat"]:
        src = INSTALLER_DIR / name
        if not src.exists():
            raise SystemExit(f"[build] 缺少安装文件：{src}")
        shutil.copy2(src, STAGE / name)
        copied += 1
    for src in EXTRA_TOP_LEVEL:
        if not src.exists():
            raise SystemExit(f"[build] 缺少分发包文件：{src}")
        shutil.copy2(src, STAGE / src.name)
        copied += 1

    # 版本号必须在包内（install.ps1 用它写注册表 DisplayVersion）
    venv_version = STAGE / "VERSION"
    venv_version.write_text(version + "\n", encoding="utf-8")

    # 兜底：无论上游怎么改，分发包里都不允许出现 .env 或用户数据目录
    leaked = [p for p in STAGE.rglob("*") if p.name in SKIP_NAMES]
    if leaked:
        raise SystemExit(f"[build] 分发包里出现了不该有的路径：{leaked[:5]}")
    env_leak = STAGE / ".env"
    if env_leak.exists():
        raise SystemExit("[build] 分发包里出现了 .env（含 API Key），已中止")

    print(f"[build] 已整理 {copied} 项到 {STAGE}")
    return STAGE


def _ignore_user_data(directory: str, names: list[str]) -> set[str]:
    """copytree 的 ignore：目录内部同样要过滤用户数据。"""
    return {n for n in names if n in SKIP_NAMES or Path(n).suffix in SKIP_SUFFIXES}


def verify_zip(path: Path) -> None:
    """打包结果自检：不含密钥/用户数据，且必须含运行所需文件。

    和打 zip 绑在一起而不是只在 CI 里做——本地构建同样需要它；
    失败信息写清楚"哪些文件不该在 / 缺了什么"，避免出现"CI 红了看不出原因"。
    """
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    bad = [
        n for n in names
        if n == ".env" or n.startswith((".env/", "data/", "generated/", "logs/", "index/"))
    ]
    if bad:
        raise SystemExit(f"[build] 分发包含禁止内容: {bad[:5]}（共 {len(bad)} 项）")
    required = {"install.ps1", "install.bat", "Pray.exe", "VERSION"}
    missing = sorted(required - set(names))
    if missing:
        raise SystemExit(f"[build] 分发包缺少必需文件: {missing}")
    print(f"[build] 自检通过: {len(names)} 个条目，无 .env / 用户数据，必需文件齐全")


def build_zip(version: str) -> Path:
    target = DIST / f"Pray-{version}-win64.zip"
    if target.exists():
        target.unlink()
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(STAGE.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(STAGE).as_posix())
                count += 1
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"[build] 应用包: {target.name}  ({count} 个文件, {size_mb:.1f} MB)")
    verify_zip(target)
    return target


def build_setup_exe(app_zip: Path) -> Path:
    """用 PyInstaller 把 [应用 zip + 安装逻辑] 打成单个 PraySetup.exe。"""
    target = DIST / "PraySetup.exe"
    if target.exists():
        target.unlink()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--console", "--name", "PraySetup",
        "--add-data", f"{app_zip};.",
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build" / "setup"),
        "--specpath", str(ROOT / "build" / "setup"),
        "--noconfirm", "--clean",
        str(INSTALLER_DIR / "setup_app.py"),
    ]
    print("[build] 构建 PraySetup.exe ...")
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0 or not target.exists():
        raise SystemExit(f"[build] PraySetup.exe 构建失败（exit {proc.returncode}）")
    print(f"[build] 安装器: {target.name}  ({target.stat().st_size / 1024 / 1024:.1f} MB)")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Pray Windows 分发产物")
    parser.add_argument("--zip-only", action="store_true", help="只构建应用 zip，不构建 PraySetup.exe")
    parser.add_argument("--keep-stage", action="store_true", help="保留 dist/release_stage")
    args = parser.parse_args()

    version = read_version()
    print(f"[build] 版本: {version}")
    stage_release(version)
    app_zip = build_zip(version)
    if not args.zip_only:
        build_setup_exe(app_zip)
    if not args.keep_stage:
        shutil.rmtree(STAGE, ignore_errors=True)
        print("[build] 已清理临时 staging 目录")
    print("[build] 完成。")
    print(f"  {DIST / f'Pray-{version}-win64.zip'}")
    if not args.zip_only:
        print(f"  {DIST / 'PraySetup.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
