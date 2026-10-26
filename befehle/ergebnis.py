from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import plattform

ORDNER_NAME = "atlas-kit"
NICHT_ERLAUBT = re.compile(r"[^A-Za-z0-9._-]")


class NichtSchreibbar(Exception):
    def __init__(self, ordner: Path):
        super().__init__(str(ordner))
        self.ordner = ordner


def dateiname(kit_id: str) -> str:
    return NICHT_ERLAUBT.sub("-", kit_id)


def ergebnisordner(arbeitsordner: Path, kit: Path | None) -> Path:
    if kit is not None and _liegt_in(arbeitsordner, kit):
        return kit / ORDNER_NAME
    return arbeitsordner / ORDNER_NAME


def _liegt_in(pfad: Path, ordner: Path) -> bool:
    for p, o in ((Path(os.path.abspath(pfad)), Path(os.path.abspath(ordner))),
                 (Path(os.path.realpath(pfad)), Path(os.path.realpath(ordner)))):
        if p == o or o in p.parents:
            return True
    return False


def lege_an(ordner: Path) -> bool:
    if ordner.is_dir():
        return False
    try:
        ordner.mkdir()
    except OSError:
        raise NichtSchreibbar(ordner.parent)
    return True


def _standard_rechte() -> int:
    maske = os.umask(0)
    os.umask(maske)
    return 0o666 & ~maske


STANDARD_RECHTE = _standard_rechte()


def schreibe_atomar(ziel: Path, inhalt: bytes, modus: int | None = None) -> None:
    """Schreibt erst eine Temp-Datei mit zufälligem Namen im Zielordner (O_EXCL, folgt keinem Symlink),
    dann os.replace. Rechte: modus, sonst die der vorhandenen Datei, sonst die Vorgabe der umask."""
    try:
        nummer, temp = tempfile.mkstemp(prefix=f".{ziel.name}.", suffix=".tmp", dir=str(ziel.parent))
    except OSError:
        raise NichtSchreibbar(ziel.parent)
    try:
        with os.fdopen(nummer, "wb") as datei:
            datei.write(inhalt)
        if modus is None:
            try:
                modus = os.lstat(ziel).st_mode & 0o777 if not os.path.islink(ziel) else STANDARD_RECHTE
            except OSError:
                modus = STANDARD_RECHTE
        os.chmod(temp, modus)
        plattform.ersetze(temp, ziel)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise NichtSchreibbar(ziel.parent)
