from __future__ import annotations

import hmac
import json
import os
import re
import stat
import sys
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

import ergebnis as ergebnis_modul
import plattform
import werkstatt_ablage as ablage
import werkstatt_pfade as pfade

SCHEMA = "atlas-werkstatt-pipelines/0.1"
DATEI = "pipelines.json"
EINTRAEGE_HOECHSTENS = 500
LESEN_HOECHSTENS = 4 * 1024 * 1024
EINSTELLUNGEN_HOECHSTENS = 1024 * 1024
SKILL = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
LAUF_ID = re.compile(r"^l\d{8}-\d{6}-[0-9a-f]{6}$")
ZU_VIELE = "Zu viele Einstellungen gespeichert."
FELDER = ("logs", "auswertung", "takt", "ausgewertet_bis", "ausgeloest_bis", "zuletzt_gestellt")
FRUEHERE_FELDER = ("logs", "auswertung", "ausgewertet_bis")
TAKT_ARTEN = ("jeder_lauf", "alle_n", "abstand", "klick")
TAKT_GRENZEN = {"alle_n": ("n", 2, 50), "abstand": ("stunden", 1, 720)}
TAKT_FEHLER = ("Der Takt der Auswertung ist ungültig: alle 2 bis 50 Läufe oder höchstens einmal je 1 bis 720 "
               "Stunden.")
ANZEIGE = f"{ablage.ORDNER}/{DATEI}"


class EinstellungFehler(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
        self.text = text


def gueltiger_takt(takt: object) -> dict | None:
    if not isinstance(takt, dict) or takt.get("art") not in TAKT_ARTEN:
        return None
    grenze = TAKT_GRENZEN.get(takt["art"])
    if grenze is None:
        return {"art": takt["art"]} if set(takt) == {"art"} else None
    feld, kleinste, groesste = grenze
    wert = takt.get(feld)
    if set(takt) != {"art", feld} or isinstance(wert, bool) or not isinstance(wert, int) \
            or not kleinste <= wert <= groesste:
        return None
    return {"art": takt["art"], feld: wert}


def zeit_von(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        zeit = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return zeit if zeit.tzinfo is not None else None


@dataclass(frozen=True)
class Pipeline:
    logs: bool = True
    auswertung: bool = False
    takt: dict = field(default_factory=lambda: {"art": "jeder_lauf"})
    ausgewertet_bis: str | None = None
    ausgeloest_bis: str | None = None
    zuletzt_gestellt: str | None = None
    ausgeloest_fehlt: bool = False

    def als_dict(self) -> dict:
        daten = {"logs": self.logs, "auswertung": self.auswertung, "takt": dict(self.takt),
                 "ausgewertet_bis": self.ausgewertet_bis, "ausgeloest_bis": self.ausgeloest_bis,
                 "zuletzt_gestellt": self.zuletzt_gestellt}
        if self.ausgeloest_fehlt:
            del daten["ausgeloest_bis"]
        return daten


def _lauf_id_oder_null(wert: object) -> bool:
    return wert is None or isinstance(wert, str) and bool(LAUF_ID.fullmatch(wert))


def _gueltig(eintrag: object) -> Pipeline | None:
    if not isinstance(eintrag, dict) or not set(FRUEHERE_FELDER) <= set(eintrag) or set(eintrag) - set(FELDER):
        return None
    if not isinstance(eintrag["logs"], bool) or not isinstance(eintrag["auswertung"], bool):
        return None
    takt = gueltiger_takt(eintrag["takt"]) if "takt" in eintrag else {"art": "jeder_lauf"}
    zuletzt = eintrag.get("zuletzt_gestellt")
    if takt is None or not _lauf_id_oder_null(eintrag["ausgewertet_bis"]) \
            or not _lauf_id_oder_null(eintrag.get("ausgeloest_bis")) \
            or not (zuletzt is None or zeit_von(zuletzt) is not None):
        return None
    return Pipeline(eintrag["logs"], eintrag["auswertung"], takt, eintrag["ausgewertet_bis"],
                    eintrag.get("ausgeloest_bis"), zuletzt, "ausgeloest_bis" not in eintrag)


class Einstellungen:
    def __init__(self, ergebnis: Path, geheimnis: bytes, sperre: threading.RLock,
                 protokoll: Callable[[str], None] | None = None) -> None:
        self.ergebnis = Path(ergebnis)
        self.ordner = self.ergebnis / ablage.ORDNER
        self.datei = self.ordner / DATEI
        self.geheimnis = geheimnis
        self.sperre = sperre
        self.protokoll = protokoll or self._stderr
        self._gemerkt: tuple | None = None
        self._letzter_grund: str | None = None

    @staticmethod
    def _stderr(zeile: str) -> None:
        sys.stderr.write(zeile + "\n")
        sys.stderr.flush()

    def _melde(self, grund: str | None) -> None:
        if grund is not None and grund != self._letzter_grund:
            self.protokoll(f"Einstellungen {DATEI}: {grund}; es gelten die Vorgaben (Logs an, Auswertung aus).")
        self._letzter_grund = grund

    def _verweis_auf_dem_weg(self) -> bool:
        return any(plattform.ist_verweis(p) for p in (self.ergebnis, self.ordner, self.datei))

    def _stand(self) -> tuple | None:
        try:
            info = os.lstat(self.datei)
        except OSError:
            return None
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode)

    def _lies(self) -> tuple[dict[str, Pipeline], str | None]:
        if self._verweis_auf_dem_weg():
            return {}, "liegt hinter einem Symlink und wird nicht gelesen"
        try:
            roh = pfade.lies_regulaer(self.datei, LESEN_HOECHSTENS)
        except pfade.LeseFehler as fehler:
            if fehler.grund == pfade.LeseFehler.FEHLT:
                return {}, "fehlt"
            return {}, "lässt sich nicht lesen"
        try:
            huelle = json.loads(roh.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return {}, "ist kaputt"
        if not isinstance(huelle, dict):
            return {}, "ist kaputt"
        signatur = huelle.get("signatur")
        daten = {k: v for k, v in huelle.items() if k != "signatur"}
        try:
            echt = isinstance(signatur, str) and hmac.compare_digest(
                signatur.encode("ascii"), ablage.signiere(self.geheimnis, daten).encode("ascii"))
        except (UnicodeEncodeError, TypeError, ValueError):
            echt = False
        if not echt:
            return {}, "ist nicht beglaubigt"
        if set(daten) != {"schema", "skills"} or daten.get("schema") != SCHEMA or not isinstance(daten.get("skills"), dict):
            return {}, "hat eine unbekannte Form"
        skills: dict[str, Pipeline] = {}
        ungueltig = []
        for name, eintrag in daten["skills"].items():
            wert = _gueltig(eintrag) if isinstance(name, str) and SKILL.fullmatch(name) else None
            if wert is None:
                ungueltig.append(str(name)[:64])
            else:
                skills[name] = wert
        return skills, (f"ungültige Einträge für {', '.join(ungueltig)}" if ungueltig else None)

    def alle(self) -> dict[str, Pipeline]:
        with self.sperre:
            stand = self._stand()
            if stand is not None and self._gemerkt is not None and self._gemerkt[0] == stand:
                return dict(self._gemerkt[1])
            skills, grund = self._lies()
            self._melde(grund)
            self._gemerkt = (stand, dict(skills)) if stand is not None else None
            return skills

    def schnappschuss(self) -> dict[str, Pipeline]:
        gemerkt = self._gemerkt
        if gemerkt is not None and gemerkt[0] == self._stand():
            return dict(gemerkt[1])
        return self._lies()[0]

    def fuer(self, skill: str) -> Pipeline:
        return self.alle().get(skill, Pipeline())

    def setze(self, skill: str, im_bauplan: set[str], **werte) -> None:
        if not SKILL.fullmatch(skill) or set(werte) - set(FELDER):
            raise EinstellungFehler(400, "Diese Einstellung gibt es nicht.")
        with self.sperre:
            if self._verweis_auf_dem_weg():
                raise EinstellungFehler(409, f"{ANZEIGE} liegt hinter einem Symlink; die Werkstatt schreibt dort "
                                             f"nicht.")
            skills = self.alle()
            if "ausgeloest_bis" in werte:
                werte["ausgeloest_fehlt"] = False
            skills[skill] = replace(skills.get(skill, Pipeline()), **werte)
            if len(skills) > EINTRAEGE_HOECHSTENS:
                for name in sorted(n for n in skills if n not in im_bauplan and n != skill):
                    del skills[name]
                    if len(skills) <= EINTRAEGE_HOECHSTENS:
                        break
            if len(skills) > EINTRAEGE_HOECHSTENS:
                raise EinstellungFehler(409, ZU_VIELE)
            daten = {"schema": SCHEMA, "skills": {n: skills[n].als_dict() for n in sorted(skills)}}
            inhalt = (json.dumps({**daten, "signatur": ablage.signiere(self.geheimnis, daten)},
                                 ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(inhalt) > EINSTELLUNGEN_HOECHSTENS:
                raise EinstellungFehler(409, ZU_VIELE)
            try:
                ordner = ablage.werkstatt_ordner(self.ergebnis)
            except (OSError, pfade.PfadFehler) as fehler:
                raise EinstellungFehler(409, f"{ablage.ORDNER} im Ergebnisordner lässt sich nicht anlegen ({fehler})")
            try:
                info = os.lstat(ordner / DATEI)
            except FileNotFoundError:
                info = None
            except OSError as fehler:
                raise EinstellungFehler(409, f"{ANZEIGE} lässt sich nicht prüfen ({fehler})")
            if info is not None and (plattform.ist_verweis_info(info) or not stat.S_ISREG(info.st_mode)):
                raise EinstellungFehler(409, f"{ANZEIGE} im Ergebnisordner ist keine reguläre Datei")
            try:
                ergebnis_modul.schreibe_atomar(ordner / DATEI, inhalt, modus=0o600)
            except (OSError, ergebnis_modul.NichtSchreibbar):
                raise EinstellungFehler(409, f"{ANZEIGE} im Ergebnisordner lässt sich nicht schreiben")
            self._gemerkt = None
            self._letzter_grund = None


def claude_ordner(home: Path, umgebung: Mapping[str, str]) -> Path:
    gesetzt = umgebung.get("CLAUDE_CONFIG_DIR")
    return Path(gesetzt) if gesetzt else Path(home) / ".claude"


def auto_memory_aus(umgebung: Mapping[str, str], claude_ordner_: Path) -> bool:
    wert = (umgebung.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") or "").strip()
    if wert and wert.casefold() not in ("0", "false"):
        return True
    datei = Path(claude_ordner_) / "settings.json"
    try:
        info = os.lstat(datei)
        if plattform.ist_verweis_info(info) or not stat.S_ISREG(info.st_mode) or info.st_size > EINSTELLUNGEN_HOECHSTENS:
            return False
        daten = json.loads(pfade.lies_regulaer(datei, EINSTELLUNGEN_HOECHSTENS).decode("utf-8"))
    except (OSError, pfade.LeseFehler, UnicodeDecodeError, ValueError, RecursionError):
        return False
    return isinstance(daten, dict) and daten.get("autoMemoryEnabled") is False
