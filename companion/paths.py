"""Where the app reads bundled assets and keeps its state.

From source everything stays next to the code (as before). Frozen, state lives in %LOCALAPPDATA%\MagicCompanion.
MAGIC_COMPANION_DATA overrides the data dir (testing).
"""
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)  # bundled web/, magic.ico
BUILD_DIR = APP_DIR / "build" if FROZEN else APP_DIR.parent / "build"  # driver INF, for hardware ids

_override = os.environ.get("MAGIC_COMPANION_DATA")
if _override:
    DATA_DIR = Path(_override)
elif FROZEN:
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "MagicCompanion"
else:
    DATA_DIR = Path(__file__).resolve().parent
DATA_DIR.mkdir(parents=True, exist_ok=True)

EXE_PATH = Path(sys.executable) if FROZEN else None
