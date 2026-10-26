"""Eine Seite im Browser öffnen, nicht im Programm, das für .html-Dateien eingestellt ist (oft ein Editor).

Ermittelt wird der Standardbrowser für http(s): auf macOS die Bundle-ID des http-Handlers aus den LaunchServices
(ohne Eintrag Safari), geöffnet mit `open -b`; auf Linux `xdg-settings get default-web-browser` und `gtk-launch`;
unter Windows die ProgId aus UrlAssociations\\http\\UserChoice(Latest) und deren shell\\open\\command in der Registry.
oeffne() sagt, wie sicher das war: SICHER (im Browser), UNSICHER (mit der Dateizuordnung, z. B. xdg-open oder
os.startfile) oder None (gar nicht). ATLAS_KIT_BROWSER (Tests) gilt als Browser.
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCHALTER = "ATLAS_KIT_BROWSER"
WARTEN_HOECHSTENS_S = 30
SICHER, UNSICHER = "browser", "zuordnung"
SAFARI = "com.apple.Safari"
LAUNCHSERVICES = (Path.home() / "Library" / "Preferences" / "com.apple.LaunchServices"
                  / "com.apple.launchservices.secure.plist")
WAHL = "Software\\Microsoft\\Windows\\Shell\\Associations\\UrlAssociations\\http\\UserChoice"
WAHL_SCHLUESSEL = (WAHL + "Latest", WAHL)  # Windows 11 24H2 schreibt UserChoiceLatest
PLATZHALTER = ("%1", "%l", "%L")
WINDOWS_START_S = 3.0
_TEIL = re.compile(r'"([^"]*)"|(\S+)')


def _http_handler(plist: Path = LAUNCHSERVICES) -> str | None:
    """Bundle-ID des Programms, das macOS für http-Links nimmt; None ohne Eintrag oder lesbare Datei."""
    try:
        with open(plist, "rb") as datei:
            daten = plistlib.load(datei)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    for eintrag in daten.get("LSHandlers", []) if isinstance(daten, dict) else []:
        if isinstance(eintrag, dict) and eintrag.get("LSHandlerURLScheme") == "http":
            kennung = eintrag.get("LSHandlerRoleAll")
            return kennung if isinstance(kennung, str) and kennung else None
    return None


def _lauf(befehl: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(befehl, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=WARTEN_HOECHSTENS_S)
    except (OSError, subprocess.SubprocessError):
        return None


def _gelungen(befehl: list[str]) -> bool:
    lauf = _lauf(befehl)
    return lauf is not None and lauf.returncode == 0


def _linux_browser() -> str | None:
    """Desktop-Datei des Standardbrowsers (z. B. firefox.desktop) oder None."""
    programm = shutil.which("xdg-settings")
    if programm is None:
        return None
    lauf = _lauf([programm, "get", "default-web-browser"])
    name = lauf.stdout.strip() if lauf is not None and lauf.returncode == 0 else ""
    return name if name.endswith(".desktop") and "/" not in name else None


def _winreg():
    import winreg  # noqa: PLC0415 (nur unter Windows)
    return winreg


def _registry_wert(reg, wurzel, pfad: str, name: str) -> str | None:
    try:
        schluessel = reg.OpenKey(wurzel, pfad)
    except OSError:
        return None
    try:
        wert = reg.QueryValueEx(schluessel, name)[0]
    except OSError:
        return None
    finally:
        reg.CloseKey(schluessel)
    return wert if isinstance(wert, str) and wert.strip() else None


def _windows_browser(pfad: Path) -> list[str] | None:
    """Befehl des Standardbrowsers für http aus der Registry, mit pfad statt %1; None ohne lesbaren Eintrag."""
    try:
        reg = _winreg()
    except ImportError:
        return None
    progid = next((p for p in (_registry_wert(reg, reg.HKEY_CURRENT_USER, s, "ProgId") for s in WAHL_SCHLUESSEL) if p),
                  None)
    befehl = _registry_wert(reg, reg.HKEY_CLASSES_ROOT, f"{progid}\\shell\\open\\command", "") if progid else None
    teile = [m.group(1) if m.group(1) is not None else m.group(2) for m in _TEIL.finditer(befehl or "")]
    if not teile or not teile[0].lower().endswith(".exe"):
        return None
    ersetzt = [str(pfad) if t in PLATZHALTER else t for t in teile]
    return ersetzt if ersetzt != teile else [*teile, str(pfad)]


def _starte_windows_browser(befehl: list[str]) -> bool:
    """Ein Browser, der noch nicht lief, bleibt im Vordergrund: nicht auf sein Ende warten, nur auf einen Fehlstart."""
    try:
        prozess = subprocess.Popen(befehl, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        return False
    try:
        return prozess.wait(timeout=WINDOWS_START_S) == 0
    except subprocess.TimeoutExpired:
        return True


def oeffne(pfad: Path) -> str | None:
    attrappe = os.environ.get(SCHALTER)
    if attrappe:
        return SICHER if _gelungen([attrappe, str(pfad)]) else None
    if sys.platform == "darwin":
        programm = shutil.which("open")
        if programm is None:
            return None
        return SICHER if _gelungen([programm, "-b", _http_handler() or SAFARI, str(pfad)]) else None
    if sys.platform.startswith("win"):
        befehl = _windows_browser(pfad)
        if befehl is not None and _starte_windows_browser(befehl):
            return SICHER
        try:
            os.startfile(str(pfad))
        except (OSError, AttributeError):
            return None
        return UNSICHER
    browser = _linux_browser()
    starter = shutil.which("gtk-launch")
    if browser and starter and _gelungen([starter, browser, str(pfad)]):
        return SICHER
    rueckfall = shutil.which("xdg-open")
    if rueckfall and _gelungen([rueckfall, str(pfad)]):
        return UNSICHER
    return None
