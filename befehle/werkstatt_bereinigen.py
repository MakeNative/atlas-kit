from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import aenderungssatz
import werkstatt_gedaechtnis as gd
from virtuelles_kit import diff

SCHEMA = "atlas-aenderung/0.1"
BEREICH_WORT = {"project": "Projekt", "local": "Projekt, nur auf diesem Rechner", "user": "Alle Projekte"}
ZEILENTEXT_IN_BESCHREIBUNG = 120
EINTRAEGE_HOECHSTENS = 30
MEMORY_MD = "MEMORY.md"
KEINER_GEHT = "Keiner der gewählten Memory-Einträge lässt sich so entfernen."


def nicht_mehr_so(agent: str) -> str:
    return f"Der Eintrag steht nicht mehr so im Memory von {agent}."


def datei_geaendert(datei: str) -> str:
    return f"Die Datei {datei} hat sich seit der Auswertung geändert."


seit_vorschau = gd.seit_vorschau


def verweist_noch(agent: str, datei: str, zeile: int) -> str:
    return f"MEMORY.md von {agent} verweist noch auf {datei} (Zeile {zeile})."


LAUF_WARNUNG = ("Gerade läuft ein Lauf dieses Kits; seine Agenten können ihr Memory gleichzeitig ändern. Die Werkstatt "
                "prüft jede Zeile unmittelbar vor dem Schreiben.")


def titel(anzahl: int) -> str:
    return f"Memory bereinigen: {anzahl} {'Eintrag' if anzahl == 1 else 'Einträge'}"


def meldung_bereinigt(anzahl: int) -> str:
    return f"Memory bereinigt: {anzahl} {'Eintrag' if anzahl == 1 else 'Einträge'} entfernt."


def _gekuerzt(text: str) -> str:
    return text if len(text) <= ZEILENTEXT_IN_BESCHREIBUNG else text[:ZEILENTEXT_IN_BESCHREIBUNG] + "…"


def beschreibung(eintrag: dict) -> str:
    wo = f"{eintrag['agent']} ({BEREICH_WORT.get(eintrag['bereich'], eintrag['bereich'])})"
    if eintrag["aktion"] == "zeile_entfernen":
        return f"Aus dem Memory von {wo}: „{_gekuerzt(eintrag['zeilentext'])}“ entfernen"
    return f"Datei {eintrag['datei']} aus dem Memory von {wo} entfernen"


def ist_gedaechtnis_satz(satz: dict) -> bool:
    return aenderungssatz.ist_gedaechtnis_satz(satz)


def _lies(ziel: gd.Ziel, datei: str, verboten: gd.Verboten) -> tuple[bytes | None, str | None]:
    try:
        return gd.lies(ziel, datei, verboten), None
    except gd.GedaechtnisFehler as fehler:
        return None, fehler.text


def baue_satz(eintraege: list[dict], auswahl: object, register: gd.Register,
              verboten: gd.Verboten) -> tuple[dict, list[str]]:
    gewaehlt = [e for e in eintraege if e["id"] in auswahl]
    satz_eintraege = []
    meldungen = []
    for e in gewaehlt:
        ziel = register.hole(e["gid"])
        inhalt, fehler = _lies(ziel, e["datei"], verboten)
        if e["aktion"] == "zeile_entfernen":
            if fehler is not None or inhalt is None or gd.treffer(inhalt, e["zeilentext"]) != 1:
                meldungen.append(nicht_mehr_so(e["agent"]))
            operation = {"memory_zeile_entfernen": {"gedaechtnis_id": e["gid"], "datei": e["datei"],
                                                    "zeilentext": e["zeilentext"]}}
        else:
            if fehler is not None or inhalt is None or hashlib.sha256(inhalt).hexdigest() != e["sha256"]:
                meldungen.append(datei_geaendert(e["datei"]))
            operation = {"memory_datei_entfernen": {"gedaechtnis_id": e["gid"], "datei": e["datei"],
                                                    "sha256": e["sha256"]}}
        satz_eintraege.append({"id": e["id"], "beschreibung": beschreibung(e), **operation})
    satz = {"schema": SCHEMA, "titel": titel(len(satz_eintraege)), "herkunft": "ki", "eintraege": satz_eintraege}
    return satz, meldungen


@dataclass
class Berechnung:
    eintraege: list[dict] = field(default_factory=list)
    fehler: list[dict] = field(default_factory=list)
    aenderungen: dict[str, tuple[bytes | None, bytes | None]] = field(default_factory=dict)
    dateien: list[dict] = field(default_factory=list)
    warnungen: list[dict] = field(default_factory=list)
    ziele: dict[str, gd.Ziel] = field(default_factory=dict)
    anzeigen: dict[str, str] = field(default_factory=dict)
    seit_vorschau: list[str] = field(default_factory=list)
    gelesen: dict[str, bytes | None] = field(default_factory=dict)
    entfernt: int = 0


def _verweis(datei: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9._/-]){re.escape(datei)}(?![A-Za-z0-9_-])")


def berechne(satz: dict, gewaehlt: set[str], register: gd.Register, verboten: gd.Verboten,
             lauf_offen: bool) -> Berechnung:
    b = Berechnung()
    vorher: dict[str, bytes | None] = {}
    jetzt: dict[str, bytes | None] = {}
    entfernte_zeilen: dict[str, list[str]] = {}
    entfernte_dateien: list[tuple[gd.Ziel, str]] = []
    for e in satz["eintraege"]:
        art, op = aenderungssatz.operation(e)
        info = {"id": e["id"], "beschreibung": e["beschreibung"], "operation": art, "gewaehlt": e["id"] in gewaehlt,
                "dateien": []}
        b.eintraege.append(info)
        if e["id"] not in gewaehlt:
            continue
        if not register.kennt(op["gedaechtnis_id"]):
            b.fehler.append({"eintrag": e["id"], "text": gd.ORDNER_UNBEKANNT})
            continue
        ziel = register.hole(op["gedaechtnis_id"])
        datei = op["datei"]
        schluessel = gd.schluessel(ziel.gid, datei)
        anzeige = gd.tilde(Path(ziel.ordner) / datei, verboten.home)
        b.ziele[ziel.gid] = ziel
        b.anzeigen[schluessel] = anzeige
        info["dateien"] = [anzeige]
        if schluessel not in jetzt:
            inhalt, fehler = _lies(ziel, datei, verboten)
            if fehler is not None:
                b.fehler.append({"eintrag": e["id"], "text": fehler})
                b.seit_vorschau.append(fehler)
                continue
            vorher[schluessel] = jetzt[schluessel] = b.gelesen[schluessel] = inhalt
        aktuell = jetzt[schluessel]
        if art == "memory_zeile_entfernen":
            if aktuell is None or gd.treffer(aktuell, op["zeilentext"]) != 1:
                b.fehler.append({"eintrag": e["id"], "text": nicht_mehr_so(ziel.agent)})
                b.seit_vorschau.append(seit_vorschau(datei, ziel.agent))
                continue
            jetzt[schluessel] = gd.ohne_zeile(aktuell, op["zeilentext"])
            entfernte_zeilen.setdefault(schluessel, []).append(op["zeilentext"])
        else:
            if aktuell is None or hashlib.sha256(aktuell).hexdigest() != op["sha256"]:
                b.fehler.append({"eintrag": e["id"], "text": datei_geaendert(datei)})
                b.seit_vorschau.append(seit_vorschau(datei, ziel.agent))
                continue
            jetzt[schluessel] = None
            entfernte_dateien.append((ziel, datei))
        b.entfernt += 1
    for schluessel in sorted(jetzt):
        if jetzt[schluessel] != vorher[schluessel]:
            b.aenderungen[schluessel] = (vorher[schluessel], jetzt[schluessel])
    for schluessel, (alt, neu) in b.aenderungen.items():
        gid, _ = gd.teile_von(schluessel)
        ziel = b.ziele[gid]
        pfad = b.anzeigen[schluessel]
        b.dateien.append({"pfad": pfad, "art": "geloescht" if neu is None else "geaendert", "diff": diff(pfad, alt, neu),
                          "im_kit": False, "gedaechtnis": {"agent": ziel.agent, "bereich": ziel.bereich}})
    for ziel, datei in entfernte_dateien:
        meldung = _verweis_in_memory_md(ziel, datei, verboten, entfernte_zeilen)
        if meldung is not None:
            b.warnungen.append({"pfad": gd.tilde(Path(ziel.ordner) / MEMORY_MD, verboten.home),
                                "art": "gedaechtnis_verweis", "text": meldung})
    if lauf_offen and b.aenderungen:
        b.warnungen.append({"pfad": None, "art": "gedaechtnis_lauf", "text": LAUF_WARNUNG})
    return b


def _verweis_in_memory_md(ziel: gd.Ziel, datei: str, verboten: gd.Verboten,
                          entfernte_zeilen: dict[str, list[str]]) -> str | None:
    if datei.casefold() == MEMORY_MD.casefold():
        return None
    inhalt, fehler = _lies(ziel, MEMORY_MD, verboten)
    if inhalt is None or fehler is not None:
        return None
    weg = list(entfernte_zeilen.get(gd.schluessel(ziel.gid, MEMORY_MD), []))
    muster = _verweis(datei)
    for nummer, zeile in enumerate(gd.zeilen_von(inhalt), 1):
        if zeile in weg:
            weg.remove(zeile)
            continue
        if f"({datei})" in zeile or muster.search(zeile):
            return verweist_noch(ziel.agent, datei, nummer)
    return None
