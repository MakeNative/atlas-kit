from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SCHALTER = "ATLAS_KIT_BROWSER"
WARTEN_HOECHSTENS_S = 30


def _programm() -> list[str] | None:
    attrappe = os.environ.get(SCHALTER)
    if attrappe:
        return [attrappe]
    name = "open" if sys.platform == "darwin" else "xdg-open"
    gefunden = shutil.which(name)
    return [gefunden] if gefunden else None


def oeffne(pfad: Path) -> bool:
    if sys.platform.startswith("win") and not os.environ.get(SCHALTER):
        try:
            os.startfile(str(pfad))
        except OSError:
            return False
        return True
    programm = _programm()
    if programm is None:
        return False
    try:
        lauf = subprocess.run([*programm, str(pfad)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=WARTEN_HOECHSTENS_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return lauf.returncode == 0
