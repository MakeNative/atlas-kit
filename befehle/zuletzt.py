"""Liste der zuletzt in der Werkstatt geöffneten Kits (specs/werkstatt.yaml WS-NK8, WS-NK9).

Bibliothek für /atlas:werkstatt und das Start-Fenster (Ticket 22). Die Datei <datenordner>/werkstatt-zuletzt.json
enthält nur Pfade ({"ordner": [...]}, neueste zuerst, höchstens 10). Gleich sind zwei Pfade nach
os.path.normcase(normpath(...)): unter Windows ohne Unterschied von Groß-/Kleinschreibung und Schrägstrich bzw.
Backslash. Beim Lesen fallen Ordner weg, die es nicht mehr gibt oder die kein Kit bzw. Projekt mehr sind; eine kaputte
Datei gilt als leer. Nur Standardbibliothek.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import ergebnis
import finden

DATEI = "werkstatt-zuletzt.json"
SPERRE = "werkstatt-zuletzt.sperre"
HOECHSTENS = 10
SPERRE_WARTEN_S = 3.0


def _schluessel(pfad: str, modul=os.path) -> str:
    return modul.normcase(modul.normpath(pfad))


def merke_in(liste: list[str], ordner: str, pfad=os.path, hoechstens: int = HOECHSTENS) -> list[str]:
    """Neue Liste mit ordner vorn, ohne einen gleichen Eintrag weiter hinten, höchstens hoechstens Einträge."""
    neu = _schluessel(ordner, pfad)
    rest = [e for e in liste if _schluessel(e, pfad) != neu]
    return [ordner, *rest][:hoechstens]


def lies(datenordner: Path) -> list[str]:
    try:
        daten = json.loads((datenordner / DATEI).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    ordner = daten.get("ordner") if isinstance(daten, dict) else None
    if not isinstance(ordner, list):
        return []
    return [o for o in ordner if isinstance(o, str) and o.strip() and os.path.isabs(o)]


def _gleich(a: str, b: str) -> bool:
    """Derselbe Ordner (st_dev, st_ino), auch in anderer Schreibweise (macOS, Windows); sonst nach _schluessel."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return _schluessel(a) == _schluessel(b)


class _Sperre:
    """Sperre des Systems auf der bleibenden Datei werkstatt-zuletzt.sperre (fcntl.flock, unter Windows
    msvcrt.locking), damit gleichzeitige Starts einander keine Einträge überschreiben (r21-5). Das System gibt sie frei,
    wenn ihr Prozess endet, auch nach einem Absturz; es gibt keine Verwaist-Frist und niemand löscht eine fremde Sperre
    (r21-12). Harte Gesamtfrist SPERRE_WARTEN_S: Lässt sie sich so nicht holen (gehalten, Datei nicht anlegbar, an
    ihrer Stelle ein Ordner), ist gehalten False, und merke merkt diesmal nichts."""

    def __init__(self, datenordner: Path, name: str = SPERRE, warten: float = SPERRE_WARTEN_S):
        self.pfad = datenordner / name
        self.warten = warten
        self.gehalten = False
        self._fd: int | None = None

    def __enter__(self):
        ende = time.monotonic() + self.warten
        while True:
            if self._versuche():
                self.gehalten = True
                return self
            if time.monotonic() >= ende:
                self._schliesse()
                return self
            time.sleep(0.02)

    def _versuche(self) -> bool:
        try:
            if self._fd is None:
                self._fd = os.open(self.pfad, os.O_RDWR | os.O_CREAT, 0o600)
            if os.name == "nt":  # pragma: no cover - Windows
                import msvcrt
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _schliesse(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)  # gibt auch die Sperre frei
            except OSError:
                pass
            self._fd = None

    def __exit__(self, *_):
        if self.gehalten and os.name == "nt":  # pragma: no cover - Windows
            try:
                import msvcrt
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self._schliesse()
        self.gehalten = False


Sperre = _Sperre  # auch für den Start der Start-App (Ticket 22, start.sperre)


def merke(datenordner: Path, ordner: Path) -> None:
    """Setzt ordner (aufgelöst) an den Anfang der Liste; ein Eintrag für denselben Ordner in anderer Schreibweise fällt
    weg. Fehler beim Schreiben sind kein Grund, den Start scheitern zu lassen; die Liste ist eine Bequemlichkeit."""
    neu = os.path.realpath(ordner)
    try:
        datenordner.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    with _Sperre(datenordner) as sperre:
        if not sperre.gehalten:
            return  # r21-12: ohne Sperre diesmal nicht merken; die Werkstatt hängt nie daran
        alt = [e for e in lies(datenordner) if not _gleich(e, neu)]
        liste = merke_in(alt, neu)
        inhalt = json.dumps({"ordner": liste}, ensure_ascii=False, indent=2) + "\n"
        try:
            ergebnis.schreibe_atomar(datenordner / DATEI, inhalt.encode("utf-8"), modus=0o600)
        except (OSError, ergebnis.NichtSchreibbar):
            pass


def kits(datenordner: Path, home: Path, befehl: str = "/atlas:werkstatt") -> list[finden.Kit]:
    """Die Kits der Liste, die es noch gibt, in ihrer Reihenfolge, jeder Ordner einmal (st_dev, st_ino), höchstens
    HOECHSTENS. Ein Eintrag, der sich nicht lesen lässt, fällt weg, statt den Befehl scheitern zu lassen (r21-4)."""
    gefunden: list[finden.Kit] = []
    gesehen: set[tuple[int, int]] = set()
    for eintrag in lies(datenordner):
        try:
            info = os.stat(eintrag)
            kennung = (info.st_dev, info.st_ino)
            if kennung in gesehen or not Path(eintrag).is_dir():
                continue
            kit = finden.pruefe_ordner(Path(eintrag), home, befehl)
        except (finden.KeinKit, OSError, ValueError):
            continue
        gesehen.add(kennung)
        gefunden.append(kit)
        if len(gefunden) == HOECHSTENS:
            break
    return gefunden


def namens(liste: list[finden.Kit], name: str) -> list[finden.Kit]:
    """Kits der Liste, deren kit.id oder Ordnername name ist."""
    return [k for k in liste if k.kit_id == name or k.ordner.name == name]
