"""Sicherung, Verlauf und git der Werkstatt (specs/werkstatt.yaml WS-V, WS-X5).

Alles liegt im Ergebnisordner unter werkstatt/: je angewandtem Satz eine Sicherung mit den Dateien von
vorher, dazu verlauf.json. Was aus verlauf.json und sicherung.json gelesen wird, gilt als Eingabe: Schlüssel
und Kennungen werden geprüft, Ungültiges wird verworfen. Jedes Schreiben und Löschen prüft unmittelbar davor
den Pfad (werkstatt_pfade.pruefe_ziel). git läuft nur über subprocess mit Argumentliste und '--' vor Pfaden.
Nur Standardbibliothek.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import ergebnis
import plattform
import werkstatt_gedaechtnis as gd
import werkstatt_pfade as pfade
from werkstatt_pfade import ABLEITUNG, PfadFehler

VERLAUF = "verlauf.json"
SICHERUNG = "sicherung"
INFO = "sicherung.json"
DATEIEN = "dateien"
ABLEITUNG_DATEI = "ableitung.json"
ORDNER = "werkstatt"
GITIGNORE = ".gitignore"


def werkstatt_ordner(ergebnis_ordner: Path) -> Path:
    """Legt <ergebnis>/werkstatt/ an (sicherer Weg, kein Symlink) und darin einmal .gitignore mit '*': Sicherungen,
    Verlauf, Aufträge und Arbeitskopien gehören nicht ins Repo. Eine vorhandene .gitignore bleibt, wie sie ist."""
    ordner = pfade.sicherer_ordner(ergebnis_ordner, ORDNER)
    try:
        fd = os.open(ordner / GITIGNORE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | plattform.BINAER
                     | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except FileExistsError:
        return ordner
    with os.fdopen(fd, "wb") as datei:
        datei.write(b"*\n")
    return ordner


SICHERUNG_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6}(-\d{1,4})?$")
SHA = re.compile(r"^[0-9a-f]{64}$")
# Der Verlauf behält die letzten so vielen Sätze; ältere fallen weg, sobald sie gespeichert oder rückgängig gemacht sind
# oder ihr ungespeichert-Eintrag aus einem früheren Serverlauf stammt (der lässt sich nicht mehr speichern).
SAETZE_HOECHSTENS = 50
GIT_UMGEBUNG = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


class AblageFehler(Exception):
    pass


class GedaechtnisSchreibFehler(PfadFehler):
    pass


def _jetzt() -> datetime:
    return datetime.now().astimezone()


UNBEGLAUBIGT = ("Der Verlauf der Werkstatt ist nicht beglaubigt und wird nicht benutzt. Rückgängig geht nur für "
                "Änderungen, die eine Werkstatt auf diesem Rechner selbst gemacht hat.")


GEHEIMNIS_LAENGE = 32


def lade_geheimnis(datei: Path) -> bytes:
    """Schlüssel für die Beglaubigung von Verlauf und Sicherung (Ort: werkstatt.GEHEIMNIS im Datenordner des Plugins,
    außerhalb des Kits, 0600).

    Neu angelegt wird er erst ganz in eine Temp-Datei geschrieben und dann mit os.link an seinen Platz gebracht
    (atomar, scheitert bei einer vorhandenen Datei); so entsteht nie eine halbe Datei. Verliert der Aufruf einen
    Wettlauf, liest er die vorhandene. Ist eine vorhandene Datei zu kurz (kaputt), wird sie ersetzt; ein so kurzes
    Geheimnis hat nie etwas beglaubigt, vorhandener Verlauf gilt danach als nicht beglaubigt.
    """
    if not datei.parent.exists():
        datei.parent.mkdir(parents=True, mode=0o700)
    if datei.is_symlink():
        raise PfadFehler(f"{datei} ist ein Symlink; die Werkstatt liest dort kein Geheimnis.")
    nummer, temp = tempfile.mkstemp(prefix=".geheimnis.", suffix=".tmp", dir=str(datei.parent))
    try:
        inhalt = secrets.token_bytes(GEHEIMNIS_LAENGE)
        with os.fdopen(nummer, "wb") as f:
            f.write(inhalt)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        try:
            os.link(temp, datei)
            return inhalt
        except FileExistsError:
            pass
        for _ in range(10):
            vorhanden = datei.read_bytes()
            if len(vorhanden) >= GEHEIMNIS_LAENGE:
                return vorhanden
            time.sleep(0.02)
        plattform.ersetze(temp, datei)
        return inhalt
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def signiere(geheimnis: bytes, daten: object) -> str:
    text = json.dumps(daten, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(geheimnis, text, hashlib.sha256).hexdigest()


class Ablage:
    def __init__(self, ergebnis_ordner: Path, kit: Path, ableitung_pfad: Path | None, skills: str, agenten: str,
                 geheimnis: bytes, sitzung: str | None = None, ableitung_lesen=None):
        self.ergebnis = ergebnis_ordner
        self.ordner = ergebnis_ordner / ORDNER
        self.kit = kit
        self.ableitung_pfad = ableitung_pfad
        self.skills = skills
        self.agenten = agenten
        self.geheimnis = geheimnis
        self.sitzung = sitzung  # Kennung dieses Serverlaufs (Werkstatt.sitzung), für die Kürzung des Verlaufs
        self.ableitung_lesen = ableitung_lesen
        self.unbeglaubigt = False
        self._verlauf: tuple | None = None  # (Stand von verlauf.json, geprüfter Verlauf, unbeglaubigt)
        self.register: gd.Register | None = None
        self.verboten: gd.Verboten | None = None
        self.ziele: dict[str, gd.Ziel] = {}
        self.gedaechtnis_geschrieben: list[str] = []

    # ------------------------------------------------------------ Beglaubigung

    def gueltiger_schluessel(self, schluessel: object) -> bool:
        return schluessel == ABLEITUNG or gd.ist_schluessel(schluessel) \
            or pfade.freigegeben(schluessel, self.skills, self.agenten)

    def ziel(self, schluessel: str) -> tuple[gd.Ziel, str]:
        gid, datei = gd.teile_von(schluessel)
        ziel = self.ziele.get(gid)
        if ziel is None and self.register is not None and self.register.kennt(gid):
            ziel = self.ziele[gid] = self.register.hole(gid)
        if ziel is None or self.verboten is None:
            raise PfadFehler(gd.ORDNER_UNBEKANNT)
        return ziel, datei

    def _gueltige_dateien(self, dateien: object) -> list[str] | None:
        if not isinstance(dateien, list) or not all(self.gueltiger_schluessel(s) for s in dateien):
            return None
        return list(dateien)

    def _lies_beglaubigt(self, datei: Path) -> object:
        """Inhalt einer beglaubigten JSON-Datei; fehlt sie: None; unbeglaubigt: None und Vermerk."""
        if not datei.exists() and not datei.is_symlink():
            return None
        try:
            huelle = json.loads(datei.read_text(encoding="utf-8")) if not datei.is_symlink() else None
        except (OSError, ValueError):
            huelle = None
        try:
            signatur = huelle.get("signatur") if isinstance(huelle, dict) else None
            if isinstance(signatur, str) and SHA.fullmatch(signatur) and "daten" in huelle \
                    and hmac.compare_digest(signatur.encode("ascii"),
                                            signiere(self.geheimnis, huelle["daten"]).encode("ascii")):
                return huelle["daten"]
        except Exception:  # noqa: BLE001 – jede kaputte Hülle heißt nur: nicht beglaubigt
            pass
        self.unbeglaubigt = True
        return None

    def _beglaubigt(self, daten: object) -> bytes:
        huelle = {"daten": daten, "signatur": signiere(self.geheimnis, daten)}
        return (json.dumps(huelle, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    # ------------------------------------------------------------ Pfade

    def pfad(self, schluessel: str) -> Path:
        """Geprüfter Zielpfad (WS-X5); wirft PfadFehler."""
        if schluessel == ABLEITUNG:
            if self.ableitung_pfad is None:
                raise PfadFehler("Keine Ableitung für dieses Kit.")
            if self.ableitung_pfad.is_symlink() or self.ableitung_pfad.parent.is_symlink():
                raise PfadFehler(f"{self.ableitung_pfad} ist ein Symlink; die Werkstatt schreibt dort nicht.")
            return self.ableitung_pfad
        pfade.pruefe_freigabe(schluessel, self.skills, self.agenten)
        return pfade.pruefe_ziel(self.kit, schluessel)

    def lies(self, schluessel: str) -> bytes | None:
        if gd.ist_schluessel(schluessel):
            try:
                ziel, datei = self.ziel(schluessel)
                return gd.lies(ziel, datei, self.verboten)
            except (PfadFehler, gd.GedaechtnisFehler):
                return None
        try:
            if schluessel == ABLEITUNG and self.ableitung_lesen is not None:
                pfad = self.ableitung_lesen()
                if pfad.is_symlink() or pfad.parent.is_symlink():
                    return None
                return pfad.read_bytes() if pfad.is_file() else None
            pfad = self.pfad(schluessel)
            return pfad.read_bytes() if pfad.is_file() else None
        except (OSError, PfadFehler):
            return None

    # ------------------------------------------------------------ Verlauf

    def _verlauf_stand(self) -> tuple | None:
        try:
            info = os.lstat(self.ordner / VERLAUF)
        except OSError:
            return None
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)

    def verlauf(self) -> dict:
        """Der geprüfte Verlauf; gelesen und beglaubigt wird nur, wenn sich verlauf.json geändert hat (Gerät, inode,
        mtime, Größe), sonst kommt eine Kopie aus dem Speicher."""
        stand = self._verlauf_stand()
        gemerkt = self._verlauf
        if stand is not None and gemerkt is not None and gemerkt[0] == stand:
            self.unbeglaubigt = gemerkt[2]
            return copy.deepcopy(gemerkt[1])
        verlauf = self._lies_verlauf()
        self._verlauf = (stand, copy.deepcopy(verlauf), self.unbeglaubigt) if stand is not None else None
        return verlauf

    def _lies_verlauf(self) -> dict:
        self.unbeglaubigt = False
        daten = self._lies_beglaubigt(self.ordner / VERLAUF)
        if not isinstance(daten, dict):
            daten = {}
        saetze = []
        for s in daten.get("saetze") if isinstance(daten.get("saetze"), list) else []:
            if isinstance(s, dict) and isinstance(s.get("sicherung"), str) and SICHERUNG_ID.fullmatch(s["sicherung"]) \
                    and self._gueltige_dateien(s.get("dateien")) is not None and isinstance(s.get("titel"), str):
                saetze.append(s)
        ungespeichert = []
        for u in daten.get("ungespeichert") if isinstance(daten.get("ungespeichert"), list) else []:
            if isinstance(u, dict) and self._gueltige_dateien(u.get("dateien")) is not None \
                    and isinstance(u.get("titel"), str):
                ungespeichert.append(u)
        eigene_roh = daten.get("eigene") if isinstance(daten.get("eigene"), dict) else {}
        eigene = {k: v for k, v in eigene_roh.items()
                  if self.gueltiger_schluessel(k) and (v is None or isinstance(v, str) and SHA.fullmatch(v))}
        return {"saetze": saetze, "ungespeichert": ungespeichert, "eigene": eigene}

    def speichere_verlauf(self, daten: dict) -> None:
        """Schreibt den Verlauf, gekürzt nach SAETZE_HOECHSTENS; die Sicherungen weggefallener Sätze werden erst nach
        dem Schreiben entfernt."""
        daten, weg = self._gekuerzt(daten, self.sitzung)
        ordner = werkstatt_ordner(self.ergebnis)
        self._verlauf = None
        ergebnis.schreibe_atomar(ordner / VERLAUF, self._beglaubigt(daten), modus=0o600)
        for kennung in weg:
            try:
                self.entferne_sicherung(self._sicherungs_ordner(kennung))
            except AblageFehler:
                pass

    @staticmethod
    def _gekuerzt(daten: dict, sitzung: str | None) -> tuple[dict, list[str]]:
        """Die letzten SAETZE_HOECHSTENS Sätze bleiben, ältere nur, solange sie in dieser Sitzung ungespeichert und
        nicht rückgängig gemacht sind. Ungespeichert-Einträge früherer Serverläufe halten nichts fest (speichern kann
        sie keiner mehr) und fallen mit ihrem Satz weg; (Verlauf, Kennungen der Sicherungen weggefallener Sätze)."""
        saetze = daten.get("saetze", [])
        if len(saetze) <= SAETZE_HOECHSTENS:
            return daten, []
        ungespeichert = daten.get("ungespeichert", [])
        eigene = {u.get("id") for u in ungespeichert if u.get("sitzung") == sitzung}
        aeltere = saetze[:-SAETZE_HOECHSTENS]
        bleiben = [s for s in aeltere if not s.get("rueckgaengig") and s.get("id") in eigene]
        weggefallen = [s for s in aeltere if s not in bleiben]
        ohne = {s.get("id") for s in weggefallen}
        uebrig = [u for u in ungespeichert
                  if u.get("sitzung") == sitzung or str(u.get("id")).removesuffix("-zurueck") not in ohne]
        return ({**daten, "saetze": [*bleiben, *saetze[-SAETZE_HOECHSTENS:]], "ungespeichert": uebrig},
                [s["sicherung"] for s in weggefallen])

    # ------------------------------------------------------------ Sicherung

    def _sicherungs_ordner(self, kennung: object) -> Path:
        if not isinstance(kennung, str) or not SICHERUNG_ID.fullmatch(kennung):
            raise AblageFehler("Die Sicherung hat eine ungültige Kennung.")
        basis = self.ordner / SICHERUNG
        ordner = basis / kennung
        for teil in (self.ergebnis, self.ordner, basis, ordner):
            if teil.is_symlink():
                raise AblageFehler("Die Sicherung liegt hinter einem Symlink.")
        if not ordner.is_dir() or not pfade.liegt_unter(Path(os.path.realpath(ordner)), Path(os.path.realpath(basis))):
            raise AblageFehler("Die Sicherung fehlt.")
        return ordner

    def info(self, kennung: object) -> tuple[Path, dict]:
        """Ordner und geprüfte sicherung.json einer Sicherung; wirft AblageFehler."""
        ordner = self._sicherungs_ordner(kennung)
        info = self._lies_beglaubigt(ordner / INFO)
        if info is None:
            raise AblageFehler(UNBEGLAUBIGT)
        dateien = info.get("dateien") if isinstance(info, dict) else None
        if not isinstance(dateien, dict) or not dateien:
            raise AblageFehler("Die Sicherung ist unbrauchbar.")
        for schluessel, werte in dateien.items():
            if not self.gueltiger_schluessel(schluessel) or not isinstance(werte, dict):
                raise AblageFehler("Die Sicherung ist unbrauchbar.")
            for feld in ("vorher", "nachher"):
                wert = werte.get(feld)
                if wert is not None and not (isinstance(wert, str) and SHA.fullmatch(wert)):
                    raise AblageFehler("Die Sicherung ist unbrauchbar.")
            if gd.ist_schluessel(schluessel):
                self._ziel_aus_sicherung(schluessel, werte)
        return ordner, info

    def _ziel_aus_sicherung(self, schluessel: str, werte: dict) -> None:
        gid, datei = gd.teile_von(schluessel)
        modus = werte.get("modus")
        if self.register is None or werte.get("ziel") != "gedaechtnis" or werte.get("datei") != datei \
                or not (modus is None or isinstance(modus, int) and not isinstance(modus, bool) and 0 <= modus <= 0o7777):
            raise AblageFehler("Die Sicherung ist unbrauchbar.")
        try:
            self.ziele[gid] = self.register.aus_sicherung(werte, gid)
        except gd.GedaechtnisFehler:
            raise AblageFehler("Die Sicherung ist unbrauchbar.")

    def sichere(self, titel: str, herkunft: str, eintraege: list, aenderungen: dict) -> Path:
        basis = pfade.sicherer_ordner(werkstatt_ordner(self.ergebnis), SICHERUNG)
        stempel = _jetzt().strftime("%Y-%m-%d_%H-%M-%S-%f")
        kennung, nummer = stempel, 1
        while (basis / kennung).exists():
            nummer += 1
            kennung = f"{stempel}-{nummer}"
        ziel = pfade.sicherer_ordner(basis, kennung)
        try:
            dateien = {}
            for schluessel, (alt, neu) in aenderungen.items():
                if alt is not None:
                    rel = ABLEITUNG_DATEI if schluessel == ABLEITUNG else schluessel
                    teile = [DATEIEN, *rel.split("/")]
                    ordner = pfade.sicherer_ordner(ziel, *teile[:-1])
                    ergebnis.schreibe_atomar(ordner / teile[-1], alt, modus=0o600)
                dateien[schluessel] = {"vorher": pfade.sha(alt), "nachher": pfade.sha(neu)}
                if gd.ist_schluessel(schluessel):
                    dateien[schluessel].update(self._gedaechtnis_felder(schluessel))
            info = {"titel": titel, "herkunft": herkunft, "zeit": _jetzt().isoformat(timespec="seconds"),
                    "eintraege": eintraege, "dateien": dateien}
            ergebnis.schreibe_atomar(ziel / INFO, self._beglaubigt(info), modus=0o600)
        except BaseException:
            self.entferne_sicherung(ziel)  # keine halbe Sicherung ohne sicherung.json liegen lassen
            raise
        return ziel

    def _gedaechtnis_felder(self, schluessel: str) -> dict:
        ziel, datei = self.ziel(schluessel)
        try:
            modus = gd.modus(ziel, datei, self.verboten)
        except gd.GedaechtnisFehler:
            modus = None
        return {"ziel": "gedaechtnis", "bereich": ziel.bereich, "agent": ziel.agent, "ordner": str(ziel.ordner),
                "ordner_id": list(ziel.ordner_id), "anker_id": list(ziel.anker_id), "datei": datei, "modus": modus}

    def vorher_aus(self, sicherung: Path, info: dict, schluessel: str) -> bytes | None:
        if info["dateien"][schluessel]["vorher"] is None:
            return None
        rel = ABLEITUNG_DATEI if schluessel == ABLEITUNG else schluessel
        try:
            pfad = pfade.pruefe_ziel(sicherung / DATEIEN, rel)
            inhalt = pfad.read_bytes()
        except (OSError, PfadFehler):
            raise AblageFehler("Eine Datei der Sicherung fehlt.")
        if pfade.sha(inhalt) != info["dateien"][schluessel]["vorher"]:
            raise AblageFehler("Eine Datei der Sicherung wurde verändert.")
        return inhalt

    # ------------------------------------------------------------ Schreiben

    def schreibe(self, inhalte: dict[str, bytes | None], modi: dict[str, int | None] | None = None,
                 wiederherstellen: bool = False, erwartet: dict[str, bytes | None] | None = None) -> None:
        """Prüft je Datei unmittelbar vor dem Schreiben bzw. Löschen den Pfad (WS-X5)."""
        self.gedaechtnis_geschrieben = []
        for schluessel, inhalt in inhalte.items():
            if gd.ist_schluessel(schluessel):
                self._schreibe_gedaechtnis(schluessel, inhalt, (modi or {}).get(schluessel), wiederherstellen,
                                           (erwartet or {}).get(schluessel, gd.OHNE_PRUEFUNG))
                self.gedaechtnis_geschrieben.append(schluessel)
                continue
            pfad = self.pfad(schluessel)
            if inhalt is None:
                try:
                    info = os.lstat(pfad)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise PfadFehler(f"{schluessel} ist keine Datei; die Werkstatt löscht sie nicht.")
                pfad.unlink()
                self._raeume_leere_ordner(pfad.parent)
            else:
                pfad.parent.mkdir(parents=True, exist_ok=True)
                pfad = self.pfad(schluessel)
                ergebnis.schreibe_atomar(pfad, inhalt)

    def _schreibe_gedaechtnis(self, schluessel: str, inhalt: bytes | None, modus: int | None,
                              wiederherstellen: bool, erwartet: object) -> None:
        ziel, datei = self.ziel(schluessel)
        try:
            gd.schreibe(ziel, datei, inhalt, self.verboten, modus, wiederherstellen=wiederherstellen,
                        erwartet=erwartet)
        except gd.GedaechtnisFehler as fehler:
            raise GedaechtnisSchreibFehler(fehler.text)
        except OSError:
            raise GedaechtnisSchreibFehler(gd.schreibt_nicht(Path(ziel.ordner), self.verboten,
                                                             "die Datei lässt sich nicht schreiben"))

    def _raeume_leere_ordner(self, ordner: Path) -> None:
        """Entfernt leer gewordene Ordner unterhalb des Skill- bzw. Agenten-Ordners (z. B. skills/<name>/), nie
        diese Ordner selbst, .claude oder die Kit-Wurzel."""
        grenzen = [self.kit / self.skills, self.kit / self.agenten]
        while not ordner.is_symlink() and any(pfade.liegt_unter(ordner, g) and not pfade.gleich(ordner, g)
                                              for g in grenzen):
            try:
                ordner.rmdir()
            except OSError:
                return
            ordner = ordner.parent

    @staticmethod
    def entferne_sicherung(sicherung: Path) -> None:
        if not sicherung.is_symlink():
            shutil.rmtree(sicherung, ignore_errors=True)


# ---------------------------------------------------------------- git

class GitFehler(AblageFehler):
    """git hat nicht geantwortet oder einen Fehler gemeldet; der Text ist seine letzte Fehlerzeile."""


def _git(ordner: Path, *argumente: str, pruefen: bool = False,
         eingabe: str | None = None) -> subprocess.CompletedProcess:
    umgebung = {**os.environ, **GIT_UMGEBUNG}
    try:
        lauf = subprocess.run(["git", "-C", str(ordner), *argumente], input=eingabe,
                              stdin=subprocess.DEVNULL if eingabe is None else None, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=umgebung, timeout=60)
    except (OSError, subprocess.SubprocessError) as fehler:
        if pruefen:
            raise AblageFehler(f"git lässt sich nicht starten ({fehler}).")
        return subprocess.CompletedProcess(argumente, 1, "", str(fehler))
    if pruefen and lauf.returncode != 0:
        zeilen = [z for z in lauf.stderr.splitlines() if z.strip()]
        raise AblageFehler(zeilen[-1] if zeilen else f"git {argumente[0]} Exit {lauf.returncode}")
    return lauf


def git_wurzel(kit: Path) -> Path | None:
    lauf = _git(kit, "rev-parse", "--show-toplevel")
    if lauf.returncode != 0 or not lauf.stdout.strip():
        return None
    return Path(lauf.stdout.strip())


def _name_auf_der_platte(ordner: Path, name: str) -> str:
    """Der Name im Ordner, wie er dort steht: Auf einem Dateisystem ohne Unterschied von Groß- und Kleinschreibung
    (macOS) findet auch eine andere Schreibweise die Datei, git aber nennt sie so, wie sie angelegt ist."""
    try:
        namen = os.listdir(ordner)
    except OSError:
        return name
    if name in namen:
        return name
    def form(n: str) -> str:
        return unicodedata.normalize("NFC", n).casefold()
    gleich = [n for n in namen if form(n) == form(name)]
    return gleich[0] if len(gleich) == 1 else name


def _im_repo(wurzel: Path, pfad: Path) -> str:
    """Pfad relativ zur Wurzel, wie git ihn nennt: die Ordner aufgelöst (realpath), die Wurzel darin über ihre
    Identität gefunden und jeder Teil darunter in der Schreibweise auf der Platte, nicht in der des Aufrufs (Start mit
    ~/projekte/… statt ~/Projekte/…). Die Datei selbst darf fehlen."""
    echt = Path(os.path.realpath(pfad.parent)) / pfad.name
    try:
        ziel = os.stat(wurzel)
    except OSError:
        ziel = None
    for oben in (echt.parent, *echt.parent.parents) if ziel is not None else ():
        try:
            if not os.path.samestat(os.stat(oben), ziel):
                continue
        except OSError:
            continue
        ordner, teile = Path(wurzel), []
        for teil in echt.relative_to(oben).parts:
            teil = _name_auf_der_platte(ordner, teil)
            teile.append(teil)
            ordner = ordner / teil
        return "/".join(teile)
    return Path(os.path.relpath(echt, os.path.realpath(wurzel))).as_posix()


def _git_fehler(lauf: subprocess.CompletedProcess) -> GitFehler:
    zeilen = [z.strip() for z in (lauf.stderr or "").splitlines() if z.strip()]
    return GitFehler(zeilen[-1] if zeilen else f"Exit {lauf.returncode}")


def ungespeichert_im_git(wurzel: Path, pfade_: list[Path]) -> set[Path]:
    """Welche der Pfade haben Änderungen im Arbeitsbaum oder Index (auch neu oder gelöscht)? Ein git-Aufruf für
    alle. Antwortet git nicht, GitFehler: „nichts geändert“ wäre geraten."""
    if not pfade_:
        return set()
    namen = {p: _im_repo(wurzel, p) for p in pfade_}
    lauf = _git(wurzel, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *namen.values())
    if lauf.returncode != 0:
        raise _git_fehler(lauf)
    genannt: set[str] = set()
    teile = lauf.stdout.split("\0")
    i = 0
    while i < len(teile):
        if len(teile[i]) > 3:
            genannt.add(teile[i][3:])
            if teile[i][0] in "RC" and i + 1 < len(teile):  # umbenannt/kopiert: danach steht der alte Name
                i += 1
                genannt.add(teile[i])
        i += 1
    return {p for p in pfade_ if namen[p] in genannt}


def versioniert(wurzel: Path, pfad: Path) -> bool:
    """Ist die Datei im Repo versioniert (git ls-files)?"""
    return _git(wurzel, "ls-files", "--error-unmatch", "--", _im_repo(wurzel, pfad)).returncode == 0


def ignoriert(wurzel: Path, pfade_: list[Path]) -> set[Path]:
    """Welche der Pfade ignoriert git? Ein git-Aufruf für alle; die Pfade gehen über stdin (-z verlangt --stdin).
    Exit 1 heißt: keiner ignoriert; alles andere außer 0 ist ein GitFehler."""
    if not pfade_:
        return set()
    namen = {p: _im_repo(wurzel, p) for p in pfade_}
    lauf = _git(wurzel, "check-ignore", "--stdin", "-z", eingabe="".join(f"{n}\0" for n in namen.values()))
    if lauf.returncode == 1:
        return set()
    if lauf.returncode != 0:
        raise _git_fehler(lauf)
    genannt = set(lauf.stdout.split("\0"))
    return {p for p in pfade_ if namen[p] in genannt}


def committe(wurzel: Path, pfade_: list[Path], nachricht: str) -> str:
    """add und commit genau dieser Pfade. Scheitert eins davon (z. B. ein pre-commit-Hook), steht der Index dieser
    Pfade danach wieder wie vorher: was vorher nicht darin war, fällt heraus, der Rest bekommt seinen alten Eintrag."""
    namen = [_im_repo(wurzel, p) for p in pfade_]
    vorher = _git(wurzel, "ls-files", "-s", "-z", "--", *namen, pruefen=True).stdout
    try:
        _git(wurzel, "add", "-A", "--", *namen, pruefen=True)
        _git(wurzel, "commit", "-q", "--only", "-m", nachricht, "--", *namen, pruefen=True)
    except AblageFehler:
        _index_wie_vorher(wurzel, namen, vorher)
        raise
    return _git(wurzel, "rev-parse", "--short", "HEAD", pruefen=True).stdout.strip()


def _index_wie_vorher(wurzel: Path, namen: list[str], vorher: str) -> None:
    eintraege = [e for e in vorher.split("\0") if e]
    bekannt = {e.split("\t", 1)[1] for e in eintraege if "\t" in e}
    neu = [n for n in namen if n not in bekannt]
    if neu:
        _git(wurzel, "update-index", "--force-remove", "--", *neu)
    if eintraege:
        _git(wurzel, "update-index", "-z", "--index-info", eingabe="".join(f"{e}\0" for e in eintraege))
