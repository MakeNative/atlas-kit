"""Ein neues, leeres Kit anlegen und die Doppelklick-Datei schreiben (specs/werkstatt.yaml WS-NK1 bis WS-NK3, WS-NK7).

Bibliothek für /atlas:neu und das Start-Fenster (Ticket 22): keine Chat-Texte, nur Ergebnisse und Ausnahmen; die Sätze
stehen in texte.py. Plattformneutral: git über eine Argumentliste, chmod nur unter POSIX, die Doppelklick-Datei je
Plattform (macOS .command, Windows .cmd). Nur Standardbibliothek.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
NAME_HOECHSTENS = 64
VERSION = "0.1.0"
PLUGIN_JSON = Path(".claude-plugin") / "plugin.json"
GIT_WARTEN_S = 60

KNOPF_MAC = "Werkstatt öffnen.command"
KNOPF_WINDOWS = "Werkstatt öffnen.cmd"

GIT_COMMIT, GIT_FEHLT, GIT_OHNE_COMMIT = "commit", "ohne_git", "ohne_commit"

_KNOPF_SH = """#!/bin/sh
# Öffnet die Werkstatt von atlas-kit für das Kit in diesem Ordner: Doppelklick im Finder.
cd -P "$(dirname "$0")" || exit 1
if ! command -v claude >/dev/null 2>&1; then
  for ordner in "$HOME/.local/bin" "$HOME/.claude/local" /opt/homebrew/bin /usr/local/bin; do
    if [ -x "$ordner/claude" ]; then
      PATH="$ordner:$PATH"
      break
    fi
  done
fi
if ! command -v claude >/dev/null 2>&1; then
  echo "Claude Code ist auf diesem Mac nicht zu finden. Installier es (https://claude.com/claude-code) und doppelklick die Datei danach noch einmal."
  printf 'Mit Enter schließt du das Fenster. '
  read -r _
  exit 1
fi
exec claude '/atlas:werkstatt'
"""

_KNOPF_CMD = """@echo off
rem Oeffnet die Werkstatt von atlas-kit fuer das Kit in diesem Ordner: Doppelklick im Explorer.
rem claude wird nur im PATH gesucht, nie im Kit-Ordner (eine fremde claude.cmd dort startet nicht), und zwar bevor
rem das Skript in den Kit-Ordner wechselt. Der erste Treffer in der Reihenfolge des PATH mit .exe, .cmd oder .bat gilt.
set "NoDefaultCurrentDirectoryInExePath=1"
set "CLAUDE_EXE="
for /f "delims=" %%i in ('where $PATH:claude 2^>nul') do if not defined CLAUDE_EXE for %%x in (.exe .cmd .bat) do if /i "%%~xi"=="%%x" set "CLAUDE_EXE=%%i"
if not defined CLAUDE_EXE if exist "%USERPROFILE%\\.local\\bin\\claude.exe" set "CLAUDE_EXE=%USERPROFILE%\\.local\\bin\\claude.exe"
set "NoDefaultCurrentDirectoryInExePath="
if not defined CLAUDE_EXE goto fehlt
pushd "%~dp0"
if errorlevel 1 goto ordner
"%CLAUDE_EXE%" "/atlas:werkstatt"
exit /b
:fehlt
echo Claude Code ist auf diesem Rechner nicht zu finden. Installier es (https://claude.com/claude-code) und doppelklick die Datei danach noch einmal.
pause
exit /b 1
:ordner
echo Der Ordner dieser Datei ist nicht erreichbar. Kopier das Kit auf ein Laufwerk dieses Rechners oder starte Claude Code dort selbst mit /atlas:werkstatt.
pause
exit /b 1
"""

# Frühere Fassungen der Doppelklick-Datei (SHA-256 des Inhalts): Sie stammen von atlas-kit und gelten als ältere
# eigene Fassung (r21-16). Nur genaue Inhalte, keine Kennzeile: Eine Kennzeile ließe sich in eine fremde Datei
# schreiben und würde sie als unsere ausgeben.
_FRUEHERE = {
    KNOPF_WINDOWS: {
        "2c30234e778c3a128dac5b26a2760577271e6fec0809a1e9168e88f9e509a314",  # 037642f
        "53e53ed3b23b645e251a96be0f88a6195a87956c9792735b32f301d074a86683",  # ca9cf07
    },
    KNOPF_MAC: set(),
}
KNOPF_AKTUELL, KNOPF_AELTER, KNOPF_FREMD = "aktuell", "aelter", "fremd"


class UngueltigerName(Exception):
    pass


class OrdnerFehlt(Exception):
    pass


class SchonDa(Exception):
    """Am Ziel liegt eine Datei oder ein nicht leerer Ordner; nichts wurde geschrieben."""


class NichtAnlegbar(Exception):
    def __init__(self, ordner: Path):
        super().__init__(str(ordner))
        self.ordner = ordner


@dataclass
class Angelegt:
    ordner: Path
    git: str  # GIT_COMMIT, GIT_FEHLT oder GIT_OHNE_COMMIT
    grund: str = ""  # bei GIT_OHNE_COMMIT: letzte Zeile von git
    knopf: Path | None = None


def name_gueltig(name: str) -> bool:
    return len(name) <= NAME_HOECHSTENS and NAME.match(name) is not None


def _ist_frei(ziel: Path) -> bool:
    if ziel.is_symlink() or (ziel.exists() and not ziel.is_dir()):
        return False
    if not ziel.exists():
        return True
    try:
        return not any(ziel.iterdir())
    except OSError:
        return False


def _letzte_zeile(text: str) -> str:
    zeilen = [z.strip() for z in text.splitlines() if z.strip()]
    return zeilen[-1] if zeilen else "ohne Meldung"


def _git(git: str, ordner: Path, *argumente: str) -> subprocess.CompletedProcess:
    return subprocess.run([git, *argumente], cwd=str(ordner), stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=GIT_WARTEN_S)


def _erster_commit(ordner: Path, name: str) -> tuple[str, str]:
    git = shutil.which("git")
    if git is None:
        return GIT_FEHLT, ""
    try:
        lauf = _git(git, ordner, "init", "-q")
        if lauf.returncode != 0:
            return GIT_FEHLT, ""
        for argumente in (("add", "--", PLUGIN_JSON.as_posix()), ("commit", "-q", "-m", f"Neues Kit {name}")):
            lauf = _git(git, ordner, *argumente)
            if lauf.returncode != 0:
                return GIT_OHNE_COMMIT, _letzte_zeile(lauf.stderr or lauf.stdout)
    except (OSError, subprocess.SubprocessError) as fehler:
        return GIT_OHNE_COMMIT, str(fehler) or type(fehler).__name__
    return GIT_COMMIT, ""


def lege_an(ordner: Path, name: str, plattform: str | None = None) -> Angelegt:
    """Legt <ordner>/<name> mit .claude-plugin/plugin.json an, macht ein git-Repo mit erstem Commit (wenn es git gibt)
    und schreibt die Doppelklick-Datei der Plattform (nicht im Commit). Wirft UngueltigerName, OrdnerFehlt, SchonDa
    oder NichtAnlegbar; dann ist nichts geschrieben."""
    if not name_gueltig(name):
        raise UngueltigerName(name)
    if not ordner.is_dir():
        raise OrdnerFehlt(str(ordner))
    ziel = ordner / name
    if not _ist_frei(ziel):
        raise SchonDa(str(ziel))
    # Nur aufräumen, was dieser Aufruf selbst angelegt hat (r21-8): Schritt für Schritt mit O_EXCL bzw. ohne exist_ok.
    angelegt: list[Path] = []
    try:
        if not ziel.exists():
            ziel.mkdir()
            angelegt.append(ziel)
        (ziel / PLUGIN_JSON.parent).mkdir()
        angelegt.append(ziel / PLUGIN_JSON.parent)
        inhalt = json.dumps({"name": name, "version": VERSION}, ensure_ascii=False, indent=2) + "\n"
        with open(ziel / PLUGIN_JSON, "xb") as ausgabe:
            angelegt.append(ziel / PLUGIN_JSON)
            ausgabe.write(inhalt.encode("utf-8"))
    except FileExistsError:
        _raeume_weg(angelegt)
        raise SchonDa(str(ziel))
    except OSError:
        _raeume_weg(angelegt)
        raise NichtAnlegbar(ordner)
    git, grund = _erster_commit(ziel, name)
    try:
        knopf = schreibe_knopf(ziel, plattform)
    except OSError:
        knopf = None
    return Angelegt(ziel, git, grund, knopf)


def _raeume_weg(angelegt: list[Path]) -> None:
    for pfad in reversed(angelegt):
        try:
            if pfad.is_dir() and not pfad.is_symlink():
                pfad.rmdir()
            else:
                pfad.unlink()
        except OSError:
            pass


def knopf(plattform: str | None = None) -> tuple[str, bytes] | None:
    """(Dateiname, Inhalt) der Doppelklick-Datei für die Plattform (sys.platform), None ohne Doppelklick-Datei."""
    plattform = sys.platform if plattform is None else plattform
    if plattform == "darwin":
        return KNOPF_MAC, _KNOPF_SH.encode("utf-8")
    if plattform.startswith("win"):
        return KNOPF_WINDOWS, _KNOPF_CMD.replace("\n", "\r\n").encode("ascii")
    return None


def knopf_name(plattform: str | None = None) -> str | None:
    datei = knopf(plattform)
    return datei[0] if datei else None


def knopf_stand(pfad: Path, plattform: str | None = None) -> str:
    """KNOPF_AKTUELL (genau der Inhalt dieser Version), KNOPF_AELTER (genau eine frühere Fassung von atlas-kit) oder
    KNOPF_FREMD (alles andere, auch ein Symlink oder nicht lesbar)."""
    datei = knopf(plattform)
    try:
        if datei is None or not pfad.is_file() or pfad.is_symlink():
            return KNOPF_FREMD
        inhalt = pfad.read_bytes()
    except OSError:
        return KNOPF_FREMD
    if inhalt == datei[1]:
        return KNOPF_AKTUELL
    if hashlib.sha256(inhalt).hexdigest() in _FRUEHERE.get(datei[0], set()):
        return KNOPF_AELTER
    return KNOPF_FREMD


def schreibe_knopf(kit: Path, plattform: str | None = None) -> Path | None:
    """Schreibt die Doppelklick-Datei in den Kit-Ordner; None auf Plattformen ohne. Eine vorhandene Datei bleibt
    (FileExistsError). Ausführbar nur unter POSIX."""
    datei = knopf(plattform)
    if datei is None:
        return None
    name, inhalt = datei
    pfad = kit / name
    with open(pfad, "xb") as ausgabe:
        ausgabe.write(inhalt)
    if os.name == "posix":
        os.chmod(pfad, 0o755)
    return pfad
