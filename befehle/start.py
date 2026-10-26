"""bin/atlas-kit app [desktop] und bin/atlas-kit start (specs/start-app.yaml SA-B2, SA-B3).

app legt das Programm Atlas Werkstatt an (macOS ~/Applications, Windows Startmenü), start startet den
Startseiten-Server abgelöst vom Aufruf mit demselben Python (nur Standardbibliothek) oder verwendet einen laufenden
wieder und lässt ihn das Fenster öffnen. Kein Shell-Aufruf.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import einrichtung
import finden
import plattform
import start_plattform
import zuletzt

ORDNER = "start"
SERVER = "start_server.py"
WARTEN_S = 20.0
FENSTER_WARTEN_S = 60.0
DESKTOP = "desktop"

ANGELEGT_MAC = ("Angelegt: {0}. Du findest Atlas Werkstatt im Launchpad und mit Spotlight; zieh es ins Dock, dann ist "
                "es mit einem Klick da.")
ANGELEGT_WINDOWS = "Angelegt: Atlas Werkstatt im Startmenü."
ANGELEGT_WINDOWS_DESKTOP = "Angelegt: Atlas Werkstatt im Startmenü und auf dem Desktop."
ANDERES_SYSTEM = "Atlas Werkstatt gibt es für macOS und Windows. Hier öffnest du Kits mit /atlas:werkstatt."
FREMD = ("In {0} liegt schon ein anderes Programm namens Atlas Werkstatt. Benenne es um oder lösch es, dann ruf "
         "/atlas:app noch einmal auf.")
NICHT_ANGELEGT = "Atlas Werkstatt ließ sich nicht anlegen: {0}"
GEOEFFNET = "Atlas Werkstatt ist geöffnet."
NICHT_GEOEFFNET = "Atlas Werkstatt ließ sich nicht öffnen: {0}"
NICHT_GESTARTET = "Atlas Werkstatt ließ sich nicht starten: {0}"
KEIN_FENSTER = "Kein Browser ging auf."
UNBEKANNT = "Aufruf: atlas-kit app [desktop]"


def datenordner(aufruf) -> Path:
    return einrichtung.datenordner(aufruf.paket, aufruf.daten) / ORDNER


def version(paket: Path) -> str:
    try:
        return json.loads((paket / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return "0"


def app(aufruf, sage, plattform: start_plattform.Plattform | None = None) -> int:
    """SA-B2"""
    if aufruf.argument not in ("", DESKTOP):
        sage(UNBEKANNT)
        return 1
    plattform = plattform or start_plattform.aktuelle(home=aufruf.home)
    if plattform.name == start_plattform.ANDERE:
        sage(ANDERES_SYSTEM)
        return 1
    daten = einrichtung.datenordner(aufruf.paket, aufruf.daten) if plattform.name == start_plattform.WINDOWS \
        else aufruf.daten
    try:
        angelegt = plattform.programm_anlegen(aufruf.paket, daten, version(aufruf.paket),
                                              aufruf.argument == DESKTOP,
                                              start_plattform.installiert_als(aufruf.paket, aufruf.home))
    except start_plattform.ProgrammFremd as fehler:
        sage(FREMD.format(finden.tilde(fehler.pfad, aufruf.home)))
        return 1
    except (OSError, start_plattform.NichtMoeglich) as fehler:
        sage(NICHT_ANGELEGT.format(str(fehler) or "unbekannter Grund"))
        return 1
    if plattform.name == start_plattform.MAC:
        sage(ANGELEGT_MAC.format(finden.tilde(angelegt[0], aufruf.home)))
    else:
        sage(ANGELEGT_WINDOWS_DESKTOP if aufruf.argument == DESKTOP else ANGELEGT_WINDOWS)
    return 0


def _anfrage(zustand: dict, methode: str, pfad: str, warten: float) -> dict | None:
    if not isinstance(zustand.get("oeffnen"), str) or not isinstance(zustand.get("port"), int):
        return None
    ursprung = f"http://127.0.0.1:{zustand['port']}"
    koepfe = {"X-Atlas-Start": zustand["oeffnen"]}
    inhalt = None
    if methode == "POST":
        koepfe.update({"Origin": ursprung, "Content-Type": "application/json"})
        inhalt = b"{}"
    anfrage = urllib.request.Request(f"{ursprung}{pfad}", data=inhalt, headers=koepfe, method=methode)
    oeffner = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with oeffner.open(anfrage, timeout=warten) as antwort:
            daten = json.loads(antwort.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return daten if isinstance(daten, dict) else None


def _lies_zustand(datei: Path) -> dict | None:
    try:
        daten = json.loads(datei.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return daten if isinstance(daten, dict) else None


class StartFehler(Exception):
    pass


def _starte_server(ordner: Path) -> dict:
    datei = ordner / "start.json"
    protokoll = ordner / "start.log"
    try:
        anfang = protokoll.stat().st_size
    except OSError:
        anfang = 0
    befehl = [sys.executable, "-X", "utf8", "-B", "-E", "-s", str(Path(__file__).resolve().parent / SERVER),
              "--daten", str(ordner)]
    # Windows: CREATE_NO_WINDOW statt DETACHED_PROCESS (mit beiden gilt nur DETACHED_PROCESS, dann öffnet jedes
    # cmd/PowerShell des Servers ein Fenster), außerhalb des Jobs des Aufrufers (plattform.starte_abgeloest, r23-3).
    with open(protokoll, "ab") as log:
        prozess = plattform.starte_abgeloest(befehl, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                             cwd=str(ordner), close_fds=True)
    ende = time.monotonic() + WARTEN_S
    while time.monotonic() < ende:
        zustand = _lies_zustand(datei)
        if zustand is not None and plattform.vom_start(zustand, prozess.pid) and \
                _anfrage(zustand, "GET", "/api/status", 10) is not None:
            return zustand
        if prozess.poll() is not None:
            break
        time.sleep(0.1)
    if prozess.poll() is None:
        prozess.terminate()
    # Nur Zeilen aus diesem Lauf (r22-4)
    with open(protokoll, "rb") as log:
        log.seek(anfang)
        neu = log.read().decode("utf-8", errors="replace")
    zeilen = [z for z in neu.splitlines() if z.strip()]
    raise StartFehler(zeilen[-1] if zeilen else "keine Rückmeldung")


SPERRE = "start.sperre"
SPERRE_VERSUCH_S = 0.5


def _laufender(ordner: Path) -> dict | None:
    zustand = _lies_zustand(ordner / "start.json")
    if zustand is not None and _anfrage(zustand, "GET", "/api/status", 10) is not None:
        return zustand
    return None


def _server(ordner: Path) -> dict:
    """Ein laufender Server oder ein neu gestarteter; zwei Starts zugleich (Doppelklick) teilen sich einen (r22-4).
    Gestartet wird nur unter der Sperre des Systems auf start.sperre (wie zuletzt.py): Sie wird frei, wenn ihr Prozess
    endet, und niemand löscht eine fremde."""
    if (ordner / SPERRE).is_dir() and not (ordner / SPERRE).is_symlink():
        raise StartFehler(f"an der Stelle von {ordner / SPERRE} liegt ein Ordner; lösch ihn und öffne Atlas Werkstatt "
                          "noch einmal")
    ende = time.monotonic() + WARTEN_S + 10
    while True:
        laufend = _laufender(ordner)
        if laufend is not None:
            return laufend
        with zuletzt.Sperre(ordner, SPERRE, warten=SPERRE_VERSUCH_S) as sperre:
            if sperre.gehalten:
                return _laufender(ordner) or _starte_server(ordner)
        if time.monotonic() > ende:
            raise StartFehler("ein anderer Start läuft noch")


def starte(aufruf, sage) -> int:
    """SA-B3"""
    ordner = datenordner(aufruf)
    ordner.mkdir(parents=True, mode=0o700, exist_ok=True)  # zwei Starts zugleich legen ihn evtl. beide an
    try:
        zustand = _server(ordner)
    except StartFehler as fehler:
        sage(NICHT_GESTARTET.format(fehler))
        return 1
    antwort = _anfrage(zustand, "POST", "/api/oeffnen", FENSTER_WARTEN_S)
    if antwort is not None and antwort.get("geoeffnet") is True:
        sage(GEOEFFNET)
        return 0
    sage(NICHT_GEOEFFNET.format(KEIN_FENSTER))
    return 1
