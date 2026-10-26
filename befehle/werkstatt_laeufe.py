from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import ergebnis
import finden
import lauf_ort
import plattform
import start_plattform
import werkstatt_ablage
import werkstatt_lauf_sicht as sicht
import werkstatt_pfade

LAUF_ID = re.compile(r"^l\d{8}-\d{6}-[0-9a-f]{6}$")
SCHEMA = "atlas-lauf/0.1"
LAUFE_ORDNER = "laeufe"
LAUF_JSON = "lauf.json"
BAUPLAN_JSON = "bauplan.json"
EINSTELLUNGEN_JSON = "einstellungen.json"
EREIGNISSE = "ereignisse.jsonl"
ABGEBROCHEN = "abgebrochen"
ABGESCHLOSSEN = "abgeschlossen"
VERLOREN = "verloren-"
HOOK_SKRIPT = "lauf_hook.py"
HOOK_EREIGNISSE = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop",
                   "UserPromptSubmit", "Stop", "SessionStart", "SessionEnd", "PermissionRequest", "Notification")
MIT_MATCHER = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
HOOK_TIMEOUT_S = 5
OFFEN_HOECHSTENS = 3
ABGESCHLOSSEN_HOECHSTENS = 50
ARGUMENTE_HOECHSTENS = 2000
MELDEN_ABSTAND_S = 0.5
LAUF_JSON_HOECHSTENS = 256 * 1024
STEUERZEICHEN = re.compile(r"[\x00-\x1f\x7f]")
ORDNERWAHL_TITEL = "Arbeitsordner für den Lauf wählen"

UNBEKANNTE_SKILL = "Diesen Ablauf gibt es im Kit nicht."
ARGUMENTE_UNGUELTIG = "Die Argumente sind ungültig: eine Zeile, höchstens 2000 Zeichen."
ARGUMENTE_ANFUEHRUNG = 'Unter Windows dürfen die Argumente kein " enthalten. Schreib sie ohne Anführungszeichen.'
FELD_ABLAUF, FELD_ARGUMENTE, FELD_ARBEITSORDNER = "ablauf", "argumente", "ordner"
ORDNER_NOETIG = "Wähl einen Arbeitsordner für den Lauf."
ORDNER_IM_PROJEKT = "Ein Projekt läuft immer in seinem Projektordner; einen Arbeitsordner gibt es dafür nicht."
WERKSTATT_ORDNER = "In den Ordner der Werkstatt kann kein Lauf."
DREI_LAUFEN = "Es laufen schon 3 Läufe. Schließ einen ab, bevor du den nächsten startest."
KEIN_CLAUDE = ("Claude Code ist auf diesem Rechner nicht zu finden. Installier es von claude.com/claude-code und starte "
               "den Lauf danach noch einmal.")
TERMINAL_GRUND = "Das System hat das Fenster nicht gestartet."
LAUF_FEHLT = "Diesen Lauf gibt es nicht."
KENNUNG_FALSCH = "Das ist keine Kennung eines Laufs."
SCHON_ZU = "Der Lauf ist schon abgeschlossen."
LAUFEND_LOESCHEN = "Ein laufender Lauf lässt sich nicht löschen. Brich ihn zuerst ab."
ORDNERWAHL_OFFEN = "Die Ordnerauswahl ist schon offen."
KEINE_ORDNERWAHL = "Auf diesem System gibt es keine Ordnerauswahl. Tipp den Pfad des Arbeitsordners ein."
ORDNER_VERWEIS = "Der Ordner der Läufe liegt hinter einem Symlink; die Werkstatt legt dort nichts ab und löscht nichts."


def ordner_fehlt(pfad: str) -> str:
    return f"Den Ordner {pfad} gibt es nicht."


def terminal_zu(grund: str) -> str:
    return f"Das Terminal-Fenster ließ sich nicht öffnen: {grund}"


class LaufFehler(Exception):
    def __init__(self, status: int, fehler: str, feld: str | None = None):
        super().__init__(fehler)
        self.status = status
        self.daten = {"fehler": fehler}
        if feld is not None:
            self.daten["feld"] = feld


def einstellungen(python: Path, hook: Path, lauf_ordner: Path) -> dict:
    befehl = " ".join([start_plattform.sh_quote(plattform.hook_pfad(python)), "-X", "utf8", "-I",
                       start_plattform.sh_quote(plattform.hook_pfad(hook)),
                       start_plattform.sh_quote(plattform.hook_pfad(lauf_ordner))])
    eintrag = {"type": "command", "command": befehl, "timeout": HOOK_TIMEOUT_S}
    return {"hooks": {name: [{**({"matcher": "*"} if name in MIT_MATCHER else {}), "hooks": [dict(eintrag)]}]
                      for name in HOOK_EREIGNISSE}}


def signatur_text(stand: tuple) -> str:
    return hashlib.sha256(json.dumps(list(stand), ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]


def als_json(daten: object) -> bytes:
    return (json.dumps(daten, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _jetzt() -> datetime:
    return datetime.now().astimezone()


def _ist_ordner_ohne_verweis(pfad: Path) -> bool:
    try:
        info = os.lstat(pfad)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not plattform.ist_verweis_info(info)


def _marke_zeit(datei: Path) -> datetime | None:
    try:
        info = os.lstat(datei)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        zeit = sicht.zeit_von(datei.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        zeit = None
    return zeit or datetime.fromtimestamp(info.st_mtime).astimezone()


def _zeilen(roh: bytes) -> tuple[list[dict], int, int]:
    ende = roh.rfind(b"\n")
    if ende < 0:
        return [], 0, 0
    ereignisse: list[dict] = []
    ungueltig = 0
    for zeile_roh in roh[:ende].split(b"\n"):
        zeile_roh = zeile_roh.rstrip(b"\r")
        if not zeile_roh.strip():
            continue
        try:
            zeile = json.loads(zeile_roh.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            zeile = None
        if sicht.gueltig(zeile):
            ereignisse.append(zeile)
        else:
            ungueltig += 1
    return ereignisse, ungueltig, ende + 1


def _lies_ab(datei: Path, versatz: int) -> bytes | None:
    try:
        if plattform.ist_verweis(datei):
            return None
        with open(datei, "rb") as f:
            if os.fstat(f.fileno()).st_size < versatz:
                return None
            f.seek(versatz)
            return f.read()
    except OSError:
        return b""


@dataclass(frozen=True)
class Abgeschlossen:
    id: str
    ordner: Path
    skill: str
    arbeitsordner: str | None
    gestartet: str
    ende: str | None
    zustand: str
    agentenlaeufe: int
    signatur: str | None
    ereignis_bytes: int
    marken: sicht.Marken
    lauf: dict

    def gestartet_zeit(self) -> datetime:
        return sicht.zeit_von(self.gestartet) or datetime.fromtimestamp(0).astimezone()


@dataclass
class Gelesen:
    lid: str
    ordner: Path
    lauf: dict
    ereignisse: list[dict] = field(default_factory=list)
    versatz: int = 0
    ungueltig: int = 0
    stand: tuple | None = None
    marken: sicht.Marken = field(default_factory=sicht.Marken)
    verloren: int = 0
    frist: str | None = None
    voll: bool = False
    ausgelagert: bool = False
    zusammen: sicht.Zusammenfassung | None = None

    @property
    def offen(self) -> bool:
        return self.zusammen is None or self.zusammen.stand.zustand in sicht.OFFEN

    def gestartet(self) -> datetime:
        return sicht.zeit_von(self.lauf.get("gestartet")) or datetime.fromtimestamp(0).astimezone()


class Laeufe:
    def __init__(self, werkstatt, ergebnis_ordner: Path, daten: Path, paket: Path,
                 plattform_: start_plattform.Plattform, sende: Callable[[str, dict], None], still_s: float,
                 ruhe_s: float, *, logs_an: Callable[[str], bool] | None = None,
                 bei_ende: Callable[[Abgeschlossen], None] | None = None) -> None:
        self.werkstatt = werkstatt
        self.ergebnis = Path(ergebnis_ordner)
        self.werkstatt_ordner = self.ergebnis / "werkstatt"
        self.ordner = self.werkstatt_ordner / LAUFE_ORDNER
        self.daten = Path(daten)
        self.paket = Path(paket)
        self.plattform = plattform_
        self.sende = sende
        self.still_s = still_s
        self.ruhe_s = ruhe_s
        self.sperre = threading.RLock()
        self.ordnerwahl = threading.Lock()
        self.gelesen: dict[str, Gelesen] = {}
        self.kaputt_gemeldet: set[str] = set()
        self.gemeldet: dict[str, float] = {}
        self.ausstehend: set[str] = set()
        self.logs_an = logs_an or (lambda _skill: True)
        self.bei_ende = bei_ende
        self.abgeschlossen: dict[str, Abgeschlossen] = {}
        self.enden: list[Abgeschlossen] = []
        with self.sperre:
            self._lies_alle()

    # ------------------------------------------------------------ Lesen (LA-D4)

    def _ordner_sicher(self) -> bool:
        return all(not plattform.ist_verweis(p) for p in (self.ergebnis, self.werkstatt_ordner, self.ordner)) \
            and _ist_ordner_ohne_verweis(self.ordner)

    def _lies_alle(self) -> set[str]:
        geaendert: set[str] = set()
        vorhanden: set[str] = set()
        if self._ordner_sicher():
            try:
                eintraege = list(os.scandir(self.ordner))
            except OSError:
                eintraege = []
            for eintrag in eintraege:
                if not LAUF_ID.fullmatch(eintrag.name) or plattform.ist_verweis_eintrag(eintrag) \
                        or not eintrag.is_dir(follow_symlinks=False):
                    continue
                gelesen = self.gelesen.get(eintrag.name) or self._lade(eintrag.name)
                if gelesen is None:
                    continue
                vorhanden.add(eintrag.name)
                if eintrag.name not in self.gelesen:
                    self.gelesen[eintrag.name] = gelesen
                    geaendert.add(eintrag.name)
                if self._aktualisiere(gelesen):
                    geaendert.add(eintrag.name)
        for lid in [lid for lid in self.gelesen if lid not in vorhanden]:
            del self.gelesen[lid]
            geaendert.add(lid)
        for lid in [lid for lid in self.abgeschlossen if lid not in vorhanden]:
            if not os.path.lexists(self.ordner / lid):
                del self.abgeschlossen[lid]
        return geaendert

    def _lade(self, lid: str) -> Gelesen | None:
        ordner = self.ordner / lid
        datei = ordner / LAUF_JSON
        try:
            info = os.lstat(datei)
            if not stat.S_ISREG(info.st_mode) or info.st_size > LAUF_JSON_HOECHSTENS:
                raise ValueError("keine reguläre Datei")
            lauf = json.loads(datei.read_text(encoding="utf-8"))
            if not isinstance(lauf, dict) or lauf.get("id") != lid or not isinstance(lauf.get("skill"), str) \
                    or sicht.zeit_von(lauf.get("gestartet")) is None:
                raise ValueError("Form passt nicht")
        except (OSError, UnicodeDecodeError, ValueError) as fehler:
            if lid not in self.kaputt_gemeldet:
                self.kaputt_gemeldet.add(lid)
                sys.stderr.write(f"Lauf {lid}: lauf.json fehlt oder ist ungültig ({fehler}); der Lauf erscheint "
                                 f"nicht.\n")
                sys.stderr.flush()
            return None
        self.kaputt_gemeldet.discard(lid)
        return Gelesen(lid, ordner, lauf)

    def _stand_von(self, g: Gelesen) -> tuple:
        teile = []
        for name in (EREIGNISSE, ABGEBROCHEN, ABGESCHLOSSEN):
            try:
                info = os.lstat(g.ordner / name)
                teile.append((info.st_mtime_ns, info.st_size))
            except OSError:
                teile.append(None)
        try:
            verloren = sum(1 for n in os.listdir(g.ordner) if n.startswith(VERLOREN))
        except OSError:
            verloren = 0
        return (*teile, verloren)

    def _aktualisiere(self, g: Gelesen) -> bool:
        stand = self._stand_von(g)
        if stand == g.stand:
            return False
        war_offen = g.zusammen is not None and g.offen
        g.stand = stand
        g.verloren = stand[-1]
        g.marken = sicht.Marken(_marke_zeit(g.ordner / ABGEBROCHEN), _marke_zeit(g.ordner / ABGESCHLOSSEN))
        self._lies_zeilen(g)
        g.zusammen = sicht.fasse_zusammen(g.ereignisse, g.marken)
        if not g.offen:
            g.ereignisse = []
            g.ausgelagert = True
            if g.zusammen.zuletzt is not None:
                self.abgeschlossen[g.lid] = self._schnappschuss(g)
                if war_offen:
                    self.enden.append(self.abgeschlossen[g.lid])
        return True

    def _schnappschuss(self, g: Gelesen) -> Abgeschlossen:
        stand = g.zusammen.stand
        ordner = g.lauf.get("arbeitsordner")
        return Abgeschlossen(g.lid, g.ordner, str(g.lauf.get("skill")), ordner if isinstance(ordner, str) else None,
                             str(g.lauf.get("gestartet")), sicht.zeit_text(stand.ende) if stand.ende else None,
                             stand.zustand, g.zusammen.agenten, g.lauf.get("signatur"), g.versatz, g.marken,
                             dict(g.lauf))

    def _rufe_enden(self) -> None:
        with self.sperre:
            enden, self.enden = self.enden, []
        if self.bei_ende is None:
            return
        for lauf in enden:
            try:
                self.bei_ende(lauf)
            except Exception as fehler:  # noqa: BLE001
                sys.stderr.write(f"Lauf {lauf.id}: Auswertung nach dem Lauf nicht gestellt ({fehler}).\n")
                sys.stderr.flush()

    def abgeschlossene(self, skill: str) -> list[Abgeschlossen]:
        with self.sperre:
            self._frisch()
            liste = [self.abgeschlossen[lid] for lid, g in self.gelesen.items()
                     if lid in self.abgeschlossen and not g.offen and g.lauf.get("skill") == skill]
        self._rufe_enden()
        return sorted(liste, key=lambda a: (a.gestartet_zeit(), a.id), reverse=True)

    def kandidaten(self, skill: str) -> list[Abgeschlossen]:
        with self.sperre:
            self._frisch()
            liste = [a for a in self.abgeschlossen.values() if a.skill == skill]
        self._rufe_enden()
        return sorted(liste, key=lambda a: (a.gestartet_zeit(), a.id), reverse=True)

    def offen_im_kit(self) -> bool:
        with self.sperre:
            self._frisch()
            offen = any(g.offen for g in self.gelesen.values())
        self._rufe_enden()
        return offen

    def _lies_zeilen(self, g: Gelesen) -> None:
        if g.ausgelagert:
            g.ereignisse, g.versatz, g.ungueltig, g.voll, g.ausgelagert = [], 0, 0, False, False
        neu = _lies_ab(g.ordner / EREIGNISSE, g.versatz)
        if neu is None:
            g.ereignisse, g.versatz, g.ungueltig, g.voll = [], 0, 0, False
            neu = _lies_ab(g.ordner / EREIGNISSE, 0) or b""
        ereignisse, ungueltig, gelesen = _zeilen(neu)
        g.versatz += gelesen
        g.ungueltig += ungueltig
        g.ereignisse.extend(ereignisse)
        g.voll = g.voll or any(e["art"] == "voll" for e in ereignisse)

    def _alle_ereignisse(self, g: Gelesen) -> list[dict]:
        if not g.ausgelagert:
            return list(g.ereignisse)
        ereignisse, _, _ = _zeilen((_lies_ab(g.ordner / EREIGNISSE, 0) or b"")[:g.versatz])
        return ereignisse

    # ------------------------------------------------------------ Melden (LA-A4)

    def _melde(self, lid: str) -> None:
        jetzt = time.monotonic()
        if jetzt - self.gemeldet.get(lid, -MELDEN_ABSTAND_S) >= MELDEN_ABSTAND_S:
            self.gemeldet[lid] = jetzt
            self.ausstehend.discard(lid)
            self.sende("lauf", {"id": lid})
        else:
            self.ausstehend.add(lid)

    def _melde_ausstehende(self) -> None:
        for lid in list(self.ausstehend):
            if time.monotonic() - self.gemeldet.get(lid, -MELDEN_ABSTAND_S) >= MELDEN_ABSTAND_S:
                self._melde(lid)

    def _frisch(self) -> None:
        for lid in self._lies_alle():
            self._melde(lid)

    def takt(self) -> None:
        with self.sperre:
            self._frisch()
            jetzt = _jetzt()
            for g in self.gelesen.values():
                if not g.offen:
                    continue
                frist = sicht.still(g.lauf, g.zusammen.zuletzt, g.zusammen.stand, jetzt, self.still_s, self.ruhe_s)
                art = frist["art"] if frist else None
                if art != g.frist:
                    g.frist = art
                    self._melde(g.lid)
            self._melde_ausstehende()
        self._rufe_enden()

    # ------------------------------------------------------------ Antworten (LA-A1, LA-A2)

    def _kurz(self, g: Gelesen, jetzt: datetime) -> dict:
        return sicht.kurzform(g.lauf, g.zusammen, jetzt, self.still_s, self.ruhe_s)

    @staticmethod
    def _offen(g: Gelesen) -> bool:
        return g.offen

    def _zuletzt(self) -> str | None:
        if self.werkstatt.art != "plugin":
            return None
        ordner = lauf_ort.lies(self.daten, self.werkstatt.kit)
        return finden.tilde(ordner, self.werkstatt.home) if ordner is not None else None

    def liste(self) -> dict:
        with self.sperre:
            self._frisch()
            jetzt = _jetzt()
            geordnet = sorted(self.gelesen.values(), key=lambda g: (g.gestartet(), g.lid), reverse=True)
            laeufe = [self._kurz(g, jetzt) for g in geordnet]
        self._rufe_enden()
        return {"laeufe": laeufe, "claude": self.plattform.claude() is not None, "plattform": self.plattform.name,
                "ordnerwahl": self.plattform.name in (start_plattform.MAC, start_plattform.WINDOWS),
                "zuletzt": self._zuletzt()}

    def _gelesen(self, lid: object) -> Gelesen:
        if not isinstance(lid, str) or not LAUF_ID.fullmatch(lid):
            raise LaufFehler(404, LAUF_FEHLT)
        g = self.gelesen.get(lid)
        if g is None:
            raise LaufFehler(404, LAUF_FEHLT)
        return g

    def lauf(self, lid: str) -> dict:
        if not LAUF_ID.fullmatch(lid):
            raise LaufFehler(400, KENNUNG_FALSCH)
        with self.sperre:
            self._frisch()
            g = self._gelesen(lid)
            try:
                bauplan = json.loads((g.ordner / BAUPLAN_JSON).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                bauplan = {}
            kit = g.lauf.get("kit") if isinstance(g.lauf.get("kit"), dict) else {}
            lauf, marken, ausgelagert = g.lauf, g.marken, g.ausgelagert
            ereignisse = self._alle_ereignisse(g) if not ausgelagert else None
            ungueltig, verloren, voll = g.ungueltig, g.verloren, g.voll
            signatur = g.lauf.get("signatur")
        self._rufe_enden()
        if ereignisse is None:
            ereignisse = self._alle_ereignisse(g)
        antwort = sicht.sicht(lauf, bauplan, ereignisse, marken, _jetzt(), str(kit.get("id", "")), self.still_s,
                              self.ruhe_s)
        jetzt_signatur = signatur_text(self.werkstatt.signatur())
        antwort["lauf"].update({"kit": kit, "befehl": g.lauf.get("befehl"), "signatur": signatur,
                                "kit_geaendert": signatur != jetzt_signatur})
        return {"lauf": antwort["lauf"], "bauplan": bauplan, "schritte": antwort["schritte"],
                "zeitleiste": antwort["zeitleiste"], "kosten": antwort["kosten"], "ungueltig": ungueltig,
                "verloren": verloren, "voll": voll}

    # ------------------------------------------------------------ Starten (LA-S4 bis LA-S6)

    def _skills(self, bauplan: dict) -> set[str]:
        return {k.get("name") for k in bauplan.get("knoten", [])
                if isinstance(k, dict) and k.get("typ") == "skill" and isinstance(k.get("name"), str)}

    def _pfad(self, text: str) -> Path:
        if text == "~" or text.startswith(("~/", "~\\")):
            return Path(self.werkstatt.home) / text[2:] if len(text) > 1 else Path(self.werkstatt.home)
        return Path(text)

    def _im_werkstatt_ordner(self, ordner: Path) -> bool:
        echt = Path(os.path.realpath(ordner))
        for oben in (echt, *echt.parents):
            if oben.name.casefold() == "werkstatt" and oben.parent.name.casefold() == ergebnis.ORDNER_NAME:
                return True
            try:
                if self.werkstatt_ordner.exists() and os.path.samefile(oben, self.werkstatt_ordner):
                    return True
            except OSError:
                continue
        return False

    def _im_kit(self, ordner: Path) -> bool:
        echt = Path(os.path.realpath(ordner))
        for oben in (echt, *echt.parents):
            try:
                if os.path.samefile(oben, self.werkstatt.kit):
                    return True
            except OSError:
                continue
        return False

    def _arbeitsordner(self, roh: object) -> Path:
        if self.werkstatt.art != "plugin":
            if roh is not None:
                raise LaufFehler(400, ORDNER_IM_PROJEKT, FELD_ARBEITSORDNER)
            return Path(self.werkstatt.kit)
        if not isinstance(roh, str) or not roh.strip():
            raise LaufFehler(400, ORDNER_NOETIG, FELD_ARBEITSORDNER)
        ordner = self._pfad(roh.strip())
        if not ordner.is_absolute() or not ordner.is_dir():
            raise LaufFehler(400, ordner_fehlt(roh.strip()), FELD_ARBEITSORDNER)
        if self._im_werkstatt_ordner(ordner):
            raise LaufFehler(400, WERKSTATT_ORDNER, FELD_ARBEITSORDNER)
        return Path(os.path.abspath(ordner))

    def pruefe_ordner(self, anfrage: dict) -> dict:
        try:
            ordner = self._arbeitsordner(anfrage.get("ordner"))
        except LaufFehler as fehler:
            return {"ok": False, "fehler": fehler.daten["fehler"], "im_kit": False, "pfad": None, "anzeige": None}
        return {"ok": True, "fehler": None, "im_kit": self._im_kit(ordner), "pfad": str(ordner),
                "anzeige": finden.tilde(ordner, self.werkstatt.home)}

    def _argumente(self, roh: object) -> str:
        if roh is None:
            return ""
        if not isinstance(roh, str) or len(roh) > ARGUMENTE_HOECHSTENS or STEUERZEICHEN.search(roh):
            raise LaufFehler(400, ARGUMENTE_UNGUELTIG, FELD_ARGUMENTE)
        if self.plattform.name == start_plattform.WINDOWS and '"' in roh:
            raise LaufFehler(400, ARGUMENTE_ANFUEHRUNG, FELD_ARGUMENTE)
        return roh.strip()

    def _neue_id(self) -> str:
        jetzt = datetime.now()
        return f"l{jetzt:%Y%m%d}-{jetzt:%H%M%S}-{secrets.token_hex(3)}"

    def _lege_ordner_an(self) -> Path:
        try:
            werkstatt = werkstatt_ablage.werkstatt_ordner(self.ergebnis)
            laeufe = werkstatt_pfade.sicherer_ordner(werkstatt, LAUFE_ORDNER)
        except werkstatt_pfade.PfadFehler:
            raise LaufFehler(409, ORDNER_VERWEIS)
        if not self._ordner_sicher():
            raise LaufFehler(409, ORDNER_VERWEIS)
        while True:
            lid = self._neue_id()
            try:
                os.mkdir(laeufe / lid, 0o700)
            except FileExistsError:
                continue
            os.chmod(laeufe / lid, 0o700)
            return laeufe / lid

    def starte(self, anfrage: dict) -> dict:
        try:
            return self._starte(anfrage)
        except LaufFehler as fehler:
            fehler.daten.setdefault("feld", None)
            raise

    def _starte(self, anfrage: dict) -> dict:
        skill = anfrage.get("skill")
        bauplan = self.werkstatt.bauplan()
        if not isinstance(skill, str) or skill not in self._skills(bauplan):
            raise LaufFehler(400, UNBEKANNTE_SKILL, FELD_ABLAUF)
        argumente = self._argumente(anfrage.get("argumente"))
        arbeitsordner = self._arbeitsordner(anfrage.get("ordner"))
        plugin = self.werkstatt.art == "plugin"
        if not self.logs_an(skill):
            return self._starte_ohne_logs(skill, argumente, arbeitsordner, plugin)
        with self.sperre:
            self._frisch()
            if sum(1 for g in self.gelesen.values() if self._offen(g)) >= OFFEN_HOECHSTENS:
                raise LaufFehler(409, DREI_LAUFEN)
            claude = self.plattform.claude()
            if claude is None:
                raise LaufFehler(409, KEIN_CLAUDE)
            ordner = self._lege_ordner_an()
            try:
                lid, befehl, claude_argumente = self._schreibe_lauf(ordner, skill, argumente, arbeitsordner, plugin,
                                                                    bauplan)
            except BaseException:
                shutil.rmtree(ordner, ignore_errors=True)
                raise
        if plugin:
            lauf_ort.merke(self.daten, self.werkstatt.kit, arbeitsordner)
        antwort = {"ok": True, "id": lid}
        if self.plattform.name in (start_plattform.MAC, start_plattform.WINDOWS):
            try:
                offen = self.plattform.terminal(arbeitsordner, claude, claude_argumente, self.daten,
                                                start_plattform.ORT_ARBEIT)
            except (OSError, ergebnis.NichtSchreibbar, ValueError):
                offen = False
            if not offen:
                shutil.rmtree(ordner, ignore_errors=True)
                with self.sperre:
                    self.gelesen.pop(lid, None)
                raise LaufFehler(409, terminal_zu(TERMINAL_GRUND))
        else:
            antwort["befehl"] = start_plattform.befehlszeile(arbeitsordner, claude, claude_argumente)
        with self.sperre:
            self._frisch()
            self._bewahre_auf(lid)
            self._melde(lid)
        return antwort

    def _befehl(self, skill: str, argumente: str, plugin: bool) -> str:
        return (f"/{self.werkstatt.kit_id}:{skill}" if plugin else f"/{skill}") + (f" {argumente}" if argumente else "")

    def _starte_ohne_logs(self, skill: str, argumente: str, arbeitsordner: Path, plugin: bool) -> dict:
        claude = self.plattform.claude()
        if claude is None:
            raise LaufFehler(409, KEIN_CLAUDE)
        plugin_dir = plugin and self.werkstatt.schreibbar
        claude_argumente = [*(["--plugin-dir", str(self.werkstatt.kit)] if plugin_dir else []),
                            self._befehl(skill, argumente, plugin)]
        antwort = {"ok": True, "ohne_logs": True}
        if self.plattform.name in (start_plattform.MAC, start_plattform.WINDOWS):
            try:
                offen = self.plattform.terminal(arbeitsordner, claude, claude_argumente, self.daten,
                                                start_plattform.ORT_ARBEIT)
            except (OSError, ergebnis.NichtSchreibbar, ValueError):
                offen = False
            if not offen:
                raise LaufFehler(409, terminal_zu(TERMINAL_GRUND))
        else:
            antwort["befehl"] = start_plattform.befehlszeile(arbeitsordner, claude, claude_argumente)
        return antwort

    def _schreibe_lauf(self, ordner: Path, skill: str, argumente: str, arbeitsordner: Path, plugin: bool,
                       bauplan: dict) -> tuple[str, str, list[str]]:
        lid = ordner.name
        w = self.werkstatt
        befehl = self._befehl(skill, argumente, plugin)
        plugin_dir = plugin and w.schreibbar
        einstellungen_datei = ordner / EINSTELLUNGEN_JSON
        claude_argumente = [*(["--plugin-dir", str(w.kit)] if plugin_dir else []), "--settings",
                            str(einstellungen_datei), befehl]
        kit = bauplan.get("kit") if isinstance(bauplan.get("kit"), dict) else {}
        lauf = {"schema": SCHEMA, "id": lid,
                "kit": {"id": w.kit_id, "art": w.art, "ordner": w.quelle, "commit": kit.get("commit")},
                "skill": skill, "befehl": befehl, "argumente": argumente,
                "arbeitsordner": finden.tilde(arbeitsordner, w.home), "plattform": self.plattform.name,
                "gestartet": sicht.zeit_text(_jetzt()), "signatur": signatur_text(w.signatur()),
                "plugin_dir": plugin_dir}
        python = Path(sys.executable)
        hook = self.paket / "befehle" / HOOK_SKRIPT
        ergebnis.schreibe_atomar(ordner / BAUPLAN_JSON, als_json(bauplan), modus=0o600)
        ergebnis.schreibe_atomar(einstellungen_datei, als_json(einstellungen(python, hook, ordner)), modus=0o600)
        ergebnis.schreibe_atomar(ordner / LAUF_JSON, als_json(lauf), modus=0o600)
        return lid, befehl, claude_argumente

    # ------------------------------------------------------------ Aufbewahrung und Löschen (LA-D2, LA-D3)

    def _entferne(self, g: Gelesen) -> None:
        if not self._ordner_sicher() or plattform.ist_verweis(g.ordner) or not _ist_ordner_ohne_verweis(g.ordner):
            raise LaufFehler(409, ORDNER_VERWEIS)
        shutil.rmtree(g.ordner)
        self.gelesen.pop(g.lid, None)

    def _bewahre_auf(self, neu: str) -> None:
        abgeschlossen = sorted((g for g in self.gelesen.values() if g.lid != neu and not self._offen(g)),
                               key=lambda g: (g.gestartet(), g.lid))
        for g in abgeschlossen[:max(0, len(abgeschlossen) - ABGESCHLOSSEN_HOECHSTENS)]:
            try:
                self._entferne(g)
            except (LaufFehler, OSError):
                continue
            self._melde(g.lid)

    def loesche(self, lid: object) -> dict:
        with self.sperre:
            self._frisch()
            g = self._gelesen(lid)
            if self._offen(g):
                raise LaufFehler(409, LAUFEND_LOESCHEN)
            self._entferne(g)
            self._melde(g.lid)
        return {"ok": True}

    # ------------------------------------------------------------ Abbrechen und Abschließen (LA-S8, LA-S9)

    def _marke(self, lid: object, name: str) -> dict:
        with self.sperre:
            self._frisch()
            g = self._gelesen(lid)
            if not self._offen(g):
                raise LaufFehler(409, SCHON_ZU)
            try:
                fd = os.open(g.ordner / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                             | plattform.BINAER, 0o600)
            except FileExistsError:
                raise LaufFehler(409, SCHON_ZU)
            with os.fdopen(fd, "wb") as datei:
                datei.write((sicht.zeit_text(_jetzt()) + "\n").encode("utf-8"))
            self._aktualisiere(g)
            self._melde(g.lid)
        self._rufe_enden()
        return {"ok": True}

    def abbrechen(self, lid: object) -> dict:
        return self._marke(lid, ABGEBROCHEN)

    def abschliessen(self, lid: object) -> dict:
        return self._marke(lid, ABGESCHLOSSEN)

    # ------------------------------------------------------------ Ordnerauswahl (LA-S3, LA-A3)

    def waehle_ordner(self) -> dict:
        if self.plattform.name not in (start_plattform.MAC, start_plattform.WINDOWS):
            raise LaufFehler(409, KEINE_ORDNERWAHL)
        if not self.ordnerwahl.acquire(blocking=False):
            raise LaufFehler(409, ORDNERWAHL_OFFEN)
        try:
            start = lauf_ort.lies(self.daten, self.werkstatt.kit) or Path(self.werkstatt.home)
            try:
                pfad = self.plattform.waehle_ordner(ORDNERWAHL_TITEL, start)
            except start_plattform.NichtMoeglich:
                raise LaufFehler(409, KEINE_ORDNERWAHL)
        finally:
            self.ordnerwahl.release()
        if not pfad:
            return {"abgebrochen": True}
        return {"pfad": pfad, "anzeige": finden.tilde(Path(pfad), self.werkstatt.home), "im_kit": self._im_kit(Path(pfad))}
