from __future__ import annotations

import os
import re
from pathlib import Path

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


def schreibe_atomar(ziel: Path, inhalt: bytes) -> None:
    temp = ziel.parent / f".{ziel.name}.{os.getpid()}.tmp"
    try:
        with open(temp, "wb") as datei:
            datei.write(inhalt)
        os.replace(temp, ziel)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise NichtSchreibbar(ziel.parent)
