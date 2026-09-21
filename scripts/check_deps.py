"""校验各 requirements 文件能否被 pip 解析（依赖可安装性门禁）。

为什么需要它：项目曾在 README 里教人 `pip install -r requirements-mcp.txt`，而该文件
因 `mcp==1.22.0` 与 `fastmcp==4.0.3` 的依赖链冲突**根本装不上** —— `pip` 直接
`ResolutionImpossible`。本机因为早已装好相关包，`import` 一切正常，这个事实被掩盖了
很久。**「照文档装依赖」必须是一条被验证的不变量，而不是靠运气。**

用法：
    python -X utf8 scripts/check_deps.py            # 校验全部 requirements 文件
    python -X utf8 scripts/check_deps.py --offline   # 只用本地缓存解析（无网络环境）

退出码：0 = 全部可解析；1 = 有文件解析失败（可直接作为 CI 门禁）。

CI 的 `deps-installable` job 调用本脚本，因此本地与 CI 是**同一份实现**，
不会出现"两边逻辑各写一遍、慢慢漂移"的情况。
"""
import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FILES = [
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-mcp.txt",
    "requirements-desktop.txt",
]


def check_one(path: Path, offline: bool = False) -> tuple[bool, str]:
    """用 `pip install --dry-run` 做纯依赖解析（不实际安装）。"""
    cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--disable-pip-version-check"]
    if offline:
        cmd.append("--no-index")
    cmd += ["-r", str(path)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        # Windows 上 pip 的输出可能是 GBK：显式指定编码并容错，
        # 否则"解析失败"这条分支会因为解码异常而崩在打印之前
        encoding="utf-8", errors="replace",
        timeout=600,
    )
    if proc.returncode == 0:
        return True, ""
    tail = (proc.stderr or proc.stdout or "").strip()[-1200:]
    return False, tail


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 requirements 文件可解析性")
    parser.add_argument(
        "--offline", action="store_true",
        help="只用本地缓存解析（无网络时用；无法发现需要下载的新依赖冲突）",
    )
    args = parser.parse_args()

    failures: list[str] = []
    for name in FILES:
        path = REPO_ROOT / name
        if not path.exists():
            print(f"[skip] {name} 不存在")
            continue
        try:
            ok, detail = check_one(path, offline=args.offline)
        except subprocess.TimeoutExpired:
            ok, detail = False, "解析超时（600s）"
        print(f"[{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)
            print(detail)

    if failures:
        print(f"\n解析失败：{', '.join(failures)}")
        print("提示：这类失败通常意味着版本约束互斥（两个包对同一依赖要求不兼容），")
        print("      修法是调整其中一个的版本 pin，而不是放宽到无约束。")
        return 1

    print("\n全部 requirements 文件可解析。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
