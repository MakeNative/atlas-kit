from __future__ import annotations

import errno
import json
from pathlib import Path

SCHEMA = "atlas/0.1"
LISTEN = ("knoten", "kanten", "befunde")


class BauplanFehler(Exception):
    pass


def _ablehnen(wert: str) -> None:
    raise ValueError(f"{wert} ist kein gültiger JSON-Wert")


def _json_text(wert: object) -> str:
    return json.dumps(wert, ensure_ascii=False, separators=(",", ":"))


def _ist_objekt(wert: object) -> bool:
    return isinstance(wert, dict)


def _pruefe_aufbau(roh: object) -> dict:
    if not _ist_objekt(roh):
        raise BauplanFehler("atlas.json ist unbrauchbar: oberste Ebene ist kein Objekt")
    if "schema" not in roh:
        raise BauplanFehler(f"atlas.json hat kein Feld schema, unterstützt wird {SCHEMA}")
    gefunden = roh["schema"]
    if gefunden != SCHEMA:
        gefunden_text = gefunden if isinstance(gefunden, str) else _json_text(gefunden)
        raise BauplanFehler(f"atlas.json hat schema {gefunden_text}, unterstützt wird {SCHEMA}")
    if not _ist_objekt(roh.get("kit")):
        raise BauplanFehler("atlas.json ist unbrauchbar: Feld kit fehlt oder ist kein Objekt")
    kit_id = roh["kit"].get("id")
    if not isinstance(kit_id, str) or not kit_id:
        raise BauplanFehler("atlas.json ist unbrauchbar: kit hat kein Feld id")
    for feld in LISTEN:
        if not isinstance(roh.get(feld), list):
            raise BauplanFehler(f"atlas.json ist unbrauchbar: Feld {feld} fehlt oder ist keine Liste")
    for nummer, knoten in enumerate(roh["knoten"], start=1):
        if not _ist_objekt(knoten):
            raise BauplanFehler(f"atlas.json ist unbrauchbar: knoten Nr. {nummer} ist kein Objekt")
        for feld in ("id", "typ"):
            wert = knoten.get(feld)
            if not isinstance(wert, str) or not wert:
                raise BauplanFehler(f"atlas.json ist unbrauchbar: knoten Nr. {nummer} hat kein Feld {feld}")
    return roh


def pruefe(pfad: Path, anzeige: str) -> dict:
    try:
        roh = pfad.read_bytes()
    except OSError as fehler:
        grund = errno.errorcode.get(fehler.errno or 0, str(fehler))
        raise BauplanFehler(f"{anzeige}: nicht lesbar ({grund})")
    try:
        inhalt = roh.decode("utf-8").removeprefix("\ufeff")
    except UnicodeDecodeError:
        raise BauplanFehler(f"{anzeige}: kein UTF-8")
    try:
        atlas = json.loads(inhalt, parse_constant=_ablehnen)
    except ValueError as fehler:
        raise BauplanFehler(f"{anzeige}: kein gültiges JSON ({fehler})")
    return _pruefe_aufbau(atlas)
