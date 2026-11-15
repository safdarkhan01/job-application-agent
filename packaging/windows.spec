from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, copy_metadata


root = Path(SPECPATH).parent
datas, binaries, hiddenimports = collect_all("pypdf")
datas += copy_metadata("pypdf")
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if not python_license.is_file():
    raise SystemExit("Build on Windows with a standard CPython installation containing LICENSE.txt.")
datas.append((str(python_license), "licenses/python"))

analysis = Analysis(
    [str(root / "run_desktop.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="JobApplicationAgent",
    console=False,
    upx=False,
)
