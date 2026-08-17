import os
from pathlib import Path

ROOT = Path(SPECPATH).parents[1]
METADATA = Path(SPECPATH) / "build" / "metadata"
METADATA.mkdir(parents=True, exist_ok=True)
REVISION = METADATA / "revision.txt"
REVISION.write_text(os.environ.get("CTW_BUILD_REVISION", "local-development") + "\n", encoding="utf-8")

a = Analysis(
    [str(ROOT / "native" / "macos" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "config"), "config"),
        (str(ROOT / "web" / "templates"), "web/templates"),
        (str(ROOT / "web" / "static"), "web/static"),
        (str(REVISION), "metadata"),
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, name="Chinese Tech Wire", console=False, exclude_binaries=True)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="Chinese Tech Wire")
app = BUNDLE(coll, name="Chinese Tech Wire.app", bundle_identifier="com.clank.chinesetechwire.fieldtest")
