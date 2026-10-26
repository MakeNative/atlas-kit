from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import ergebnis
import finden
import plattform
import texte
import werkstatt_ablage as ablage_modul
import werkstatt_pfade as pfade
import zuletzt
from werkstatt_pfade import ABLEITUNG, PfadFehler

DATEI = "werkstatt-alt.json"
HOECHSTENS = 200
ZUSTAND_ORDNER = "werkstatt"


class UebernahmeFehler(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


@dataclass
class Fund:
    ort: Path
    alt: Path
    saetze: int
    zuletzt: dict
    kit: Path
    ordner_namen: tuple[str, str]
    schluessel: bytes

    def fuer_status(self, home: Path) -> dict:
        return {"ort": finden.tilde(self.ort, home), "saetze": self.saetze, "zuletzt": self.zuletzt}


def _kennung(pfad: Path) -> list[int] | None:
    try:
        info = os.stat(pfad)
    except (OSError, ValueError):
        return None
    return [info.st_dev, info.st_ino]


def _gleich(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _eintraege(daten: Path) -> list[dict]:
    try:
        inhalt = json.loads((daten / DATEI).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    liste = inhalt.get("orte") if isinstance(inhalt, dict) else None
    return [e for e in liste if isinstance(e, dict) and isinstance(e.get("kit"), list)
            and isinstance(e.get("ort"), str)] if isinstance(liste, list) else []


def _passt(eintrag: dict, kit: list[int], ort: Path) -> bool:
    if eintrag["kit"] != kit:
        return False
    kennung = _kennung(ort)
    if kennung is not None and eintrag.get("ort_kennung") == kennung:
        return True
    return os.path.normcase(os.path.abspath(eintrag["ort"])) == os.path.normcase(os.path.abspath(ort))


def abgelehnt(daten: Path, kit: Path, ort: Path) -> bool:
    kennung = _kennung(kit)
    return kennung is not None and any(_passt(e, kennung, ort) for e in _eintraege(daten))


def merke(daten: Path, kit: Path, ort: Path) -> None:
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
        rest = [e for e in _eintraege(daten) if not _passt(e, kennung, ort)]
        liste = [*rest, {"kit": kennung, "ort": str(ort), "ort_kennung": _kennung(ort)}][-HOECHSTENS:]
        inhalt = json.dumps({"orte": liste}, ensure_ascii=False, indent=2) + "\n"
        try:
            ergebnis.schreibe_atomar(daten / DATEI, inhalt.encode("utf-8"), modus=0o600)
        except (OSError, ergebnis.NichtSchreibbar):
            pass


def _von_laufender_werkstatt(daten: Path, alt: Path) -> bool:
    ordner = daten / ZUSTAND_ORDNER
    for datei in ordner.glob("*.json") if ordner.is_dir() else []:
        try:
            zustand = json.loads(datei.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(zustand, dict) or not isinstance(zustand.get("pid"), int) \
                or not isinstance(zustand.get("ergebnis"), str):
            continue
        if _gleich(Path(zustand["ergebnis"]), alt) and plattform.lebt(zustand["pid"]):
            return True
    return False


def _regulaer(pfad: Path) -> bool:
    try:
        info = os.lstat(pfad)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _ablage(alt: Path, kit: Path, ordner_namen: tuple[str, str], schluessel: bytes) -> ablage_modul.Ablage:
    skills, agenten = ordner_namen
    return ablage_modul.Ablage(alt, kit, None, skills, agenten, schluessel)


def _zeit(fund: Fund) -> datetime:
    text = fund.zuletzt.get("zeit")
    try:
        zeit = datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        zeit = None
    if zeit is None:
        return datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return zeit if zeit.tzinfo is not None else zeit.astimezone()


def _pruefe_ort(kit: Path, ergebnis_ordner: Path, ort: Path, daten: Path, schluessel: bytes,
                ordner_namen: tuple[str, str]) -> Fund | None:
    alt = ort / ergebnis.ORDNER_NAME
    werkstatt_ordner = alt / ablage_modul.ORDNER
    if _gleich(alt, ergebnis_ordner):
        return None
    if any(plattform.ist_verweis(p) for p in (alt, werkstatt_ordner)) or not werkstatt_ordner.is_dir() \
            or not _regulaer(werkstatt_ordner / ablage_modul.VERLAUF):
        return None
    if abgelehnt(daten, kit, ort) or _von_laufender_werkstatt(daten, alt):
        return None
    ablage = _ablage(alt, kit, ordner_namen, schluessel)
    saetze = ablage.verlauf()["saetze"]
    if ablage.unbeglaubigt or not saetze:
        return None
    letzter = saetze[-1]
    return Fund(ort, alt, len(saetze), {"titel": letzter.get("titel"), "zeit": letzter.get("zeit")}, kit,
                ordner_namen, schluessel)


def finde(kit: Path, ergebnis_ordner: Path, orte: list[Path], daten: Path, schluessel: bytes,
          ordner_namen: tuple[str, str]) -> Fund | None:
    funde: list[Fund] = []
    for ort in orte:
        if any(_gleich(ort, f.ort) for f in funde):
            continue
        fund = _pruefe_ort(kit, ergebnis_ordner, ort, daten, schluessel, ordner_namen)
        if fund is not None:
            funde.append(fund)
    return max(funde, key=_zeit) if funde else None


def _zu_kopieren(fund: Fund) -> list[tuple[list[str], bytes]]:
    ablage = _ablage(fund.alt, fund.kit, fund.ordner_namen, fund.schluessel)
    verlauf = ablage.verlauf()
    if ablage.unbeglaubigt or not verlauf["saetze"]:
        raise UebernahmeFehler(ablage_modul.UNBEGLAUBIGT)
    try:
        dateien: list[tuple[list[str], bytes]] = [([ablage_modul.VERLAUF],
                                                   (ablage.ordner / ablage_modul.VERLAUF).read_bytes())]
        for satz in verlauf["saetze"]:
            kennung = satz["sicherung"]
            ordner, info = ablage.info(kennung)
            basis = [ablage_modul.SICHERUNG, kennung]
            dateien.append(([*basis, ablage_modul.INFO], (ordner / ablage_modul.INFO).read_bytes()))
            for schluessel in info["dateien"]:
                inhalt = ablage.vorher_aus(ordner, info, schluessel)
                if inhalt is None:
                    continue
                rel = ablage_modul.ABLEITUNG_DATEI if schluessel == ABLEITUNG else schluessel
                dateien.append(([*basis, ablage_modul.DATEIEN, *rel.split("/")], inhalt))
    except (OSError, ablage_modul.AblageFehler, PfadFehler) as fehler:
        raise UebernahmeFehler(texte.alter_verlauf_kaputt(str(fehler)))
    return dateien


def uebernimm(fund: Fund, ergebnis_ordner: Path) -> None:
    dateien = _zu_kopieren(fund)
    ziel_da = (ergebnis_ordner / ablage_modul.ORDNER).exists()
    angelegt: list[Path] = []
    try:
        ziel = ablage_modul.werkstatt_ordner(ergebnis_ordner)
        for teile, inhalt in dateien[1:]:
            sicherung = ziel / teile[0] / teile[1]
            if sicherung not in angelegt:
                if sicherung.exists():
                    raise UebernahmeFehler(texte.alter_verlauf_kaputt(f"Die Sicherung {teile[1]} gibt es schon."))
                angelegt.append(sicherung)
            ordner = pfade.sicherer_ordner(ziel, *teile[:-1])
            ergebnis.schreibe_atomar(ordner / teile[-1], inhalt, modus=0o600)
        ergebnis.schreibe_atomar(ziel / ablage_modul.VERLAUF, dateien[0][1], modus=0o600)
    except UebernahmeFehler:
        _raeume_weg(ergebnis_ordner, angelegt, ziel_da)
        raise
    except (OSError, PfadFehler, ergebnis.NichtSchreibbar) as fehler:
        _raeume_weg(ergebnis_ordner, angelegt, ziel_da)
        raise UebernahmeFehler(texte.alter_verlauf_kaputt(str(fehler)))


def _raeume_weg(ergebnis_ordner: Path, angelegt: list[Path], ziel_da: bool) -> None:
    for ordner in angelegt:
        if not plattform.ist_verweis(ordner):
            shutil.rmtree(ordner, ignore_errors=True)
    ordner = ergebnis_ordner / ablage_modul.ORDNER
    if not ziel_da and not plattform.ist_verweis(ordner):
        shutil.rmtree(ordner, ignore_errors=True)
