from __future__ import annotations

import json
import os
from pathlib import Path

import ergebnis
import zuletzt

DATEI = "laeufe-ort.json"
HOECHSTENS = 50


def _kennung(ordner: Path) -> list[int] | None:
    try:
        info = os.stat(ordner)
    except (OSError, ValueError):
        return None
    return [info.st_dev, info.st_ino]


def _eintraege(daten: Path) -> list[dict]:
    try:
        inhalt = json.loads((daten / DATEI).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    liste = inhalt.get("kits") if isinstance(inhalt, dict) else None
    return [e for e in liste if isinstance(e, dict) and isinstance(e.get("kit"), list) and len(e["kit"]) == 2
            and isinstance(e.get("ordner"), str) and os.path.isabs(e["ordner"])] if isinstance(liste, list) else []


def lies(daten: Path, kit: Path) -> Path | None:
    kennung = _kennung(kit)
    if kennung is None:
        return None
    for eintrag in _eintraege(daten):
        if eintrag["kit"] == kennung:
            ordner = Path(eintrag["ordner"])
            return ordner if ordner.is_dir() else None
    return None


def merke(daten: Path, kit: Path, ordner: Path) -> None:
    kennung = _kennung(kit)
    if kennung is None:
        return
    try:
        daten.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    with zuletzt.Sperre(daten) as sperre:
        if not sperre.gehalten:
            return
        rest = [e for e in _eintraege(daten) if e["kit"] != kennung]
        liste = [{"kit": kennung, "ordner": str(ordner)}, *rest][:HOECHSTENS]
        inhalt = json.dumps({"kits": liste}, ensure_ascii=False, indent=2) + "\n"
        try:
            ergebnis.schreibe_atomar(daten / DATEI, inhalt.encode("utf-8"), modus=0o600)
        except (OSError, ergebnis.NichtSchreibbar):
            pass
