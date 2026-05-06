# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for CHAPPIE.

Build with:  python -m PyInstaller --noconfirm chappie.spec

Output:      dist/CHAPPIE/CHAPPIE.exe   (Windows)
             dist/CHAPPIE/CHAPPIE       (Mac/Linux)

Notes:
- Uses one-folder mode (faster startup, smaller delta updates than --onefile).
- Bundles the backend Python sources + the frontend HTML.
- Does NOT bundle Playwright's Chromium binaries; the app installs those on
  first browser-task launch via `playwright install chromium`. Bundling them
  would balloon the build to ~400 MB and break code-signing.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

ROOT = Path(".").resolve()
BACKEND = ROOT / "backend"

hidden = []
for pkg in ("agents", "core", "core.skills", "models", "utils"):
    try:
        hidden += collect_submodules(pkg, filter=lambda name: True)
    except Exception:
        pass

a = Analysis(
    ["desktop_app.py"],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=[
        (str(BACKEND / "config.json"), "backend"),
        (str(ROOT / "frontend"), "frontend"),
    ],
    hiddenimports=hidden + [
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "tests", "unittest"],
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
    name="CHAPPIE",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # windowed app: no terminal window pops up
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                # drop a icon.ico in the project root and uncomment:
    # icon="icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CHAPPIE",
)
