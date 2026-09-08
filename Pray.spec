# -*- mode: python ; coding: utf-8 -*-
"""Pray 桌面端打包配置（PyInstaller）。

用法:
    pip install pyinstaller
    pyinstaller Pray.spec

体积取舍：
- sentence-transformers 依赖 torch（解压后 >2GB）。本 spec 默认**排除** torch 全家
  与 sentence-transformers：桌面版检索自动回退纯 BM25（retriever.get_encoder 捕获
  导入失败即降级），打包体积 ~150-250MB，启动更快。
- 若需要语义检索，删掉 spec 里 excludes 中的 torch/transformers 相关项再打包
  （体积会到 1GB+，且首次启动要联网下 embedding 模型）。

产物：dist/Pray/Pray.exe（双击即用：内嵌 WebView 加载本地 UI，关窗退出）。
"""
import os

block_cipher = None

# 排除重型/桌面端不需要的依赖（语义模型 → 回退 BM25；MCP/CLI 保持源码可跑）
excludes = [
    "torch", "torchvision", "torchaudio", "torchtext",
    "transformers", "sentence_transformers",
    "tkinter", "matplotlib", "pandas", "numpy",
]

a = Analysis(
    ["desktop.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=[("static", "static")],   # 前端页面打进包（server 从这里读 INDEX_HTML）
    hiddenimports=[
        "server", "graph", "tools", "retriever", "memory", "runterm",
        "approvals", "prompts", "config",
        # langgraph/langchain 动态导入较多，常见缺漏一并补上
        "langgraph.checkpoint.sqlite",
        "langgraph.prebuilt",
        "langchain.agents",
        "langchain_openai",
        "jieba",
        "dotenv",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Pray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # 无控制台窗口（GUI 应用）
    disable_windowed_traceback=False,
    icon=os.path.join(SPECPATH, "assets", "icon.ico"),  # 应用图标（scripts/make_icon.py 生成）
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Pray",
)
