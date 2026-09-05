# PyInstaller one-folder build for the portable Windows distribution.
# Keep the application as a console executable so startup errors are visible.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


ROOT = Path(SPECPATH).resolve().parents[1]
datas = collect_data_files("pilferedparrot", include_py_files=True)
datas.extend([
    (str(ROOT / "config.example.json"), "."),
    (str(ROOT / "packaging" / "windows" / "README-WINDOWS.txt"), "."),
])

a = Analysis(
    [str(ROOT / "packaging" / "windows" / "entrypoint.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    name="PilferedParrot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    exclude_binaries=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PilferedParrot",
)
