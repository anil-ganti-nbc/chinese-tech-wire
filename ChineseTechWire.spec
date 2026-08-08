# PyInstaller spec for the CTW dashboard launcher (V0.5.6.1).
#
# Bundles ONLY code assets (web/templates, web/static) — never data/,
# config/, or .env. The built .exe must be kept in the real CTW project
# root so it operates on the operator's live data/ctw.db and config.
#
# Build:
#   .venv\Scripts\pyinstaller.exe ChineseTechWire.spec --noconfirm

import sys
from pathlib import Path

block_cipher = None
ROOT = Path.cwd()

a = Analysis(
    ['launcher_main.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / 'web' / 'templates'), 'web/templates'),
        (str(ROOT / 'web' / 'static'), 'web/static'),
    ],
    hiddenimports=[
        'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ChineseTechWire',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # kept visible for diagnosability, see HANDOFF notes
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
