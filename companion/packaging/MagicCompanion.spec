# PyInstaller spec: onedir, windowed. Build with build.ps1 (or `pyinstaller MagicCompanion.spec` from this folder).
import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
                                                 VarStruct, VSVersionInfo)

HERE = Path(SPECPATH)
COMPANION = HERE.parent
REPO = COMPANION.parent
version = re.search(r'__version__\s*=\s*"([^"]+)"', (COMPANION / "version.py").read_text()).group(1)
nums = (tuple(int(p) for p in version.split(".")) + (0, 0, 0, 0))[:4]

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "halftruths"),
            StringStruct("FileDescription", "Magic Companion"),
            StringStruct("FileVersion", version),
            StringStruct("InternalName", "MagicCompanion"),
            StringStruct("OriginalFilename", "MagicCompanion.exe"),
            StringStruct("ProductName", "Magic Companion"),
            StringStruct("ProductVersion", version),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

datas = [(str(COMPANION / "web"), "web"), (str(HERE / "magic.ico"), ".")]
inf = REPO / "build" / "AmtPtpDevice_AMD64.inf"  # only used to read the trackpad hardware ids
if inf.is_file():
    datas.append((str(inf), "build"))

a = Analysis(
    [str(COMPANION / "server.py")],
    pathex=[str(COMPANION)],
    datas=datas,
    hiddenimports=["tools.hidprobe"],
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="MagicCompanion",
    console=False,
    icon=str(HERE / "magic.ico"),
    version=version_info,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="MagicCompanion", upx=False)
