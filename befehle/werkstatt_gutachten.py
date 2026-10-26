"""Gutachten atlas-gutachten/0.1 und Einbauen gewählter Vorschläge (specs/werkstatt.yaml WS-K15, WS-K16).

Der gutachter liefert Vorschläge statt eines Änderungssatzes. Hier steht, was davon nur Form und Datei ist: der Rahmen
des Gutachtens, die Felder eines Vorschlags, der Beleg (wörtlich in der Arbeitskopie, so wie der Server sie angelegt hat,
oder eine benannte Lücke) und beim
Einbauen die Zuordnung der Einträge zu den gewählten Vorschlägen. Die Skizze eines Vorschlags prüft werkstatt_ki.py
wie einen eingereichten Satz. Nur Standardbibliothek.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import ableiten
import werkstatt_pfade as pfade

SCHEMA = "atlas-gutachten/0.1"
ARTEN = ("agent_ergaenzen", "schleife", "mensch_freigabe", "rechte_einschraenken", "schritt_zusammenlegen",
         "sonstiges")
VORSCHLAG_ID = re.compile(r"^g[1-9][0-9]?$")
VORSCHLAEGE_HOECHSTENS = 10
SKIZZE_HOECHSTENS = 20
TITEL_HOECHSTENS = 80
BEGRUENDUNG_HOECHSTENS = 400
ZITAT_HOECHSTENS = 300
ZITAT_MINDESTENS = 5
LUECKE_HOECHSTENS = 300
ZEILE_HOECHSTENS = 100000
ZEILEN_DANEBEN = 3
DATEI_HOECHSTENS = 1024 * 1024
# Das geprüfte Gutachten hängt am Auftrag; die Liste der Aufträge trägt nur Zahlen, das Gutachten kommt einzeln.
GUTACHTEN_HOECHSTENS = 64 * 1024


class GutachtenFehler(Exception):
    """Der Rahmen passt nicht: das ganze Gutachten ist abgelehnt."""

    def __init__(self, meldungen: list[str]):
        super().__init__("; ".join(meldungen))
        self.meldungen = meldungen


def _zeile_ok(wert: object, hoechstens: int) -> bool:
    return (isinstance(wert, str) and bool(wert.strip()) and len(wert) <= hoechstens
            and not any(ableiten.ist_steuerzeichen(z) for z in wert))


def _leerraum_weg(text: str) -> str:
    return " ".join(text.split())


def rahmen(gutachten: object) -> list:
    """Prüft den Rahmen und gibt die Liste der Vorschläge zurück (ungeprüft); wirft GutachtenFehler."""
    if not isinstance(gutachten, dict) or set(gutachten) != {"schema", "vorschlaege"}:
        raise GutachtenFehler(["gutachten: erwartet ein Objekt mit genau schema und vorschlaege."])
    if gutachten["schema"] != SCHEMA:
        raise GutachtenFehler([f"gutachten.schema: erwartet {SCHEMA}."])
    if len(json.dumps(gutachten, ensure_ascii=False).encode("utf-8")) > GUTACHTEN_HOECHSTENS:
        raise GutachtenFehler(["gutachten: höchstens 64 KiB; weniger oder kürzere Vorschläge."])
    vorschlaege = gutachten["vorschlaege"]
    if not isinstance(vorschlaege, list) or len(vorschlaege) > VORSCHLAEGE_HOECHSTENS:
        raise GutachtenFehler([f"gutachten.vorschlaege: erwartet eine Liste mit höchstens "
                               f"{VORSCHLAEGE_HOECHSTENS} Vorschlägen."])
    return vorschlaege


def stand_der_kopie(kopie: Path) -> dict[str, str]:
    """sha256 je regulärer Datei der Arbeitskopie (Pfad relativ, mit '/'), gleich nach dem Anlegen."""
    stand: dict[str, str] = {}
    for ordner, unterordner, dateien in os.walk(kopie):
        unterordner[:] = [u for u in unterordner if not os.path.islink(os.path.join(ordner, u))]
        for name in dateien:
            pfad = Path(ordner) / name
            try:
                inhalt = pfade.lies_regulaer(pfad, DATEI_HOECHSTENS)
            except pfade.LeseFehler:
                continue
            stand[pfad.relative_to(kopie).as_posix()] = pfade.sha(inhalt)
    return stand


def lies_datei(kopie: Path, rel: object, stand: dict[str, str] | None = None) -> list[str]:
    """Zeilen einer Datei der Arbeitskopie; nur eine reguläre Datei darin, ohne Symlink, höchstens 1 MiB.

    Mit stand (stand_der_kopie beim Anlegen) nur, wenn die Datei noch genau so ist: Der Agent liest die Kopie nur, die
    Claude-Code-Sitzung aber könnte in sie schreiben, und ein dort eingefügtes Zitat wäre kein Beleg aus dem Kit."""
    rel = pfade.pruefe_rel(rel)
    wurzel = Path(os.path.realpath(kopie))
    pfad = wurzel / rel
    if os.path.realpath(pfad.parent) != str(pfad.parent):
        raise pfade.PfadFehler(f"{rel} gibt es in der Kopie des Kits nicht.")
    try:
        inhalt = pfade.lies_regulaer(pfad, DATEI_HOECHSTENS)
    except pfade.LeseFehler as fehler:
        if fehler.grund == pfade.LeseFehler.NICHT_REGULAER:
            raise pfade.PfadFehler(f"{rel} ist keine reguläre Datei.")
        if fehler.grund == pfade.LeseFehler.ZU_GROSS:
            raise pfade.PfadFehler(f"{rel} ist größer als 1 MiB.")
        raise pfade.PfadFehler(f"{rel} gibt es in der Kopie des Kits nicht.")
    if stand is not None and stand.get(rel) != pfade.sha(inhalt):
        raise pfade.PfadFehler(f"{rel} ist in der Kopie des Kits nicht mehr so, wie die Werkstatt sie angelegt hat.")
    return inhalt.decode("utf-8", "replace").splitlines()


def pruefe_beleg(beleg: object, kopie: Path, stand: dict[str, str] | None = None) -> tuple[list[str], dict]:
    """(Meldungen, Beleg mit der Zeile, in der das Zitat wirklich steht)."""
    if isinstance(beleg, dict) and "luecke" in beleg:
        if set(beleg) - {"luecke", "datei"}:
            return ["beleg: eine Lücke hat nur luecke und datei."], {}
        if not _zeile_ok(beleg["luecke"], LUECKE_HOECHSTENS):
            return [f"beleg.luecke: eine Zeile mit 1 bis {LUECKE_HOECHSTENS} Zeichen."], {}
        if "datei" in beleg:
            try:
                lies_datei(kopie, beleg["datei"], stand)
            except pfade.PfadFehler as fehler:
                return [f"beleg.datei: {fehler}"], {}
        return [], dict(beleg)
    if not isinstance(beleg, dict) or set(beleg) != {"datei", "zeile", "zitat"}:
        return ["beleg: erwartet datei, zeile und zitat oder eine benannte luecke."], {}
    zeile, zitat = beleg["zeile"], beleg["zitat"]
    if isinstance(zeile, bool) or not isinstance(zeile, int) or not 1 <= zeile <= ZEILE_HOECHSTENS:
        return ["beleg.zeile: eine Zeilennummer ab 1."], {}
    if not _zeile_ok(zitat, ZITAT_HOECHSTENS) or len("".join(zitat.split())) < ZITAT_MINDESTENS:
        return [f"beleg.zitat: eine Zeile mit {ZITAT_MINDESTENS} bis {ZITAT_HOECHSTENS} Zeichen."], {}
    try:
        zeilen = lies_datei(kopie, beleg["datei"], stand)
    except pfade.PfadFehler as fehler:
        return [f"beleg.datei: {fehler}"], {}
    gesucht = _leerraum_weg(zitat)
    for abstand in range(ZEILEN_DANEBEN + 1):
        for nummer in sorted({zeile - abstand, zeile + abstand}):
            if 1 <= nummer <= len(zeilen) and gesucht in _leerraum_weg(zeilen[nummer - 1]):
                return [], {"datei": beleg["datei"], "zeile": nummer, "zitat": zitat}
    return [f"beleg.zitat steht nicht in {beleg['datei']} Zeile {zeile}."], {}


def pruefe_vorschlag(vorschlag: object, kopie: Path, gesehen: set[str],
                     stand: dict[str, str] | None = None) -> tuple[list[str], dict]:
    """Felder und Beleg eines Vorschlags (ohne Skizze); (Meldungen, Vorschlag mit geprüftem Beleg)."""
    if not isinstance(vorschlag, dict):
        return ["erwartet ein Objekt."], {}
    felder = {"id", "art", "titel", "begruendung", "beleg", "skizze"}
    if set(vorschlag) != felder:
        return [f"erwartet genau die Felder {', '.join(sorted(felder))}."], {}
    vid = vorschlag["id"]
    if not isinstance(vid, str) or not VORSCHLAG_ID.fullmatch(vid):
        return ["id: erwartet g1 bis g99."], {}
    if vid in gesehen:
        return [f"id {vid} kommt doppelt vor."], {}
    if vorschlag["art"] not in ARTEN:
        return [f"art: erlaubt sind {' | '.join(ARTEN)}."], {}
    if not _zeile_ok(vorschlag["titel"], TITEL_HOECHSTENS):
        return [f"titel: eine Zeile mit 1 bis {TITEL_HOECHSTENS} Zeichen."], {}
    if not _zeile_ok(vorschlag["begruendung"], BEGRUENDUNG_HOECHSTENS):
        return [f"begruendung: eine Zeile mit 1 bis {BEGRUENDUNG_HOECHSTENS} Zeichen."], {}
    skizze = vorschlag["skizze"]
    if not isinstance(skizze, list) or not 1 <= len(skizze) <= SKIZZE_HOECHSTENS:
        return [f"skizze: erwartet 1 bis {SKIZZE_HOECHSTENS} Einträge eines Änderungssatzes."], {}
    meldungen, beleg = pruefe_beleg(vorschlag["beleg"], kopie, stand)
    if meldungen:
        return meldungen, {}
    return [], {**vorschlag, "beleg": beleg}


def skizze_als_satz(vorschlag: dict) -> dict:
    return {"schema": "atlas-aenderung/0.1", "titel": vorschlag["titel"], "herkunft": "ki",
            "eintraege": vorschlag["skizze"]}


# ---------------------------------------------------------------- Einbauen

def pruefe_auswahl(gutachten: object, auswahl: object) -> list[dict]:
    """Die gewählten Vorschläge in der Reihenfolge des Gutachtens; ValueError mit einem Satz, wenn die Auswahl nicht
    passt."""
    if not isinstance(gutachten, dict):
        raise ValueError("Dieser Auftrag hat kein Gutachten.")
    if (not isinstance(auswahl, list) or not 1 <= len(auswahl) <= VORSCHLAEGE_HOECHSTENS
            or not all(isinstance(a, str) for a in auswahl) or len(set(auswahl)) != len(auswahl)):
        raise ValueError(f"auswahl ist eine Liste mit 1 bis {VORSCHLAEGE_HOECHSTENS} verschiedenen Vorschlags-ids.")
    vorhanden = {v["id"] for v in gutachten["vorschlaege"]}
    fremd = [a for a in auswahl if a not in vorhanden]
    if fremd:
        raise ValueError(f"Den Vorschlag {fremd[0][:20]} gibt es in diesem Gutachten nicht.")
    return [v for v in gutachten["vorschlaege"] if v["id"] in auswahl]


def auftragstext(vorschlaege: list[dict], zusatz: str | None) -> str:
    teile = "; ".join(f"{v['id']} „{v['titel']}“" for v in vorschlaege)
    text = f"Baue genau die ausgewählten Vorschläge aus dem Gutachten ein: {teile}."
    return f"{text} Dazu: {zusatz}" if zusatz else text


def zuordnung(satz: dict, auswahl: list[str]) -> list[str]:
    """WS-K16: jede Eintrag-id ist <gid> oder <gid>-<rest> eines gewählten Vorschlags; jeder hat mindestens einen."""
    meldungen = []
    getroffen: set[str] = set()
    for e in satz.get("eintraege", []):
        eid = str(e.get("id", ""))
        vid = next((v for v in auswahl if eid == v or eid.startswith(v + "-")), None)
        if vid is None:
            meldungen.append(f"Eintrag {eid} gehört zu keinem gewählten Vorschlag (id beginnt mit "
                             f"{' oder '.join(auswahl)}).")
        else:
            getroffen.add(vid)
    meldungen += [f"Vorschlag {v} fehlt im Satz." for v in auswahl if v not in getroffen]
    return meldungen
