from __future__ import annotations

import html
import json

PLATZHALTER_TITEL = "__ATLAS_TITEL__"
PLATZHALTER_DATEN = "__ATLAS_DATEN__"


class VorlageFehler(Exception):
    pass


def _als_skriptdaten(atlas: dict) -> str:
    text = json.dumps(atlas, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _stelle(vorlage: str, platzhalter: str) -> int:
    if vorlage.count(platzhalter) != 1:
        raise VorlageFehler(f"Platzhalter {platzhalter} steht in der Vorlage nicht genau einmal")
    return vorlage.index(platzhalter)


def fuelle(vorlage: str, atlas: dict) -> str:
    ersetzungen = sorted([
        (_stelle(vorlage, PLATZHALTER_TITEL), PLATZHALTER_TITEL, html.escape(atlas["kit"]["id"])),
        (_stelle(vorlage, PLATZHALTER_DATEN), PLATZHALTER_DATEN, _als_skriptdaten(atlas)),
    ], reverse=True)
    for stelle, platzhalter, wert in ersetzungen:
        vorlage = vorlage[:stelle] + wert + vorlage[stelle + len(platzhalter):]
    return vorlage
