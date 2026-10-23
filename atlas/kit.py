"""Kit-Angaben aus .claude-plugin/plugin.json (im Projekt aus dem Ordnernamen), dazu commit und quelle."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from befunde import Befunde
from ordner import Ordner

PLUGIN_JSON = ".claude-plugin/plugin.json"


class KitFehler(Exception):
    """plugin.json fehlt, ist kein JSON oder hat kein name (SC-A3). scan.py macht Exit 2 daraus."""


def _commit(kit: Path) -> str | None:
    """git rev-parse --short HEAD im Kit-Ordner (auch in einem Unterordner eines Repos).

    Kein Git-Repo, git nicht vorhanden oder Fehler -> None, kein Befund.
    """
    umgebung = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        kurz = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(kit),
                              capture_output=True, text=True, env=umgebung, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if kurz.returncode != 0:
        return None
    return kurz.stdout.strip() or None


def _quelle(kit: Path) -> str:
    """Absoluter Kit-Pfad, das Home-Verzeichnis als ~."""
    absolut = os.path.abspath(kit)
    home = os.path.expanduser("~")
    for pfad, basis in ((absolut, os.path.abspath(home)),
                        (os.path.realpath(absolut), os.path.realpath(home))):
        if basis and basis != "/" and (pfad == basis or pfad.startswith(basis + os.sep)):
            rest = pfad[len(basis):].replace(os.sep, "/")
            return "~" + rest
    return absolut


def _projekt(wurzel: Path) -> dict:
    return {
        "id": Path(os.path.realpath(wurzel)).name,
        "version": None,
        "commit": _commit(wurzel),
        "beschreibung": None,
        "quelle": _quelle(wurzel),
        "konvention": False,
        "art": "projekt",
    }


def lies_kit(ordner: Ordner, befunde: Befunde) -> dict:
    """Liest plugin.json (im Projekt: SC-M3). Wirft KitFehler bei SC-A3. konvention setzt scan.py."""
    kit = ordner.wurzel
    if ordner.ist_projekt:
        return _projekt(kit)
    datei = kit / PLUGIN_JSON
    if not datei.is_file():
        raise KitFehler(f"{PLUGIN_JSON} fehlt in {kit}")
    try:
        daten = json.loads(datei.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        raise KitFehler(f"{PLUGIN_JSON} ist nicht als UTF-8 lesbar")
    except json.JSONDecodeError as e:
        raise KitFehler(f"{PLUGIN_JSON} ist kein gültiges JSON (Zeile {e.lineno}: {e.msg})")
    if not isinstance(daten, dict) or not isinstance(daten.get("name"), str) or not daten["name"].strip():
        raise KitFehler(f"{PLUGIN_JSON} hat kein Feld name")

    version = daten.get("version")
    if version is None:
        befunde.melde("pflichtfeld_fehlt", f"{PLUGIN_JSON} hat kein Feld version", PLUGIN_JSON, None)
    elif not isinstance(version, str):
        befunde.melde("nicht_unterstuetzt",
                      f"{PLUGIN_JSON}: version ist kein Text ({version!r}); wird nicht übernommen",
                      PLUGIN_JSON, None)
        version = None
    beschreibung = daten.get("description")
    if beschreibung is not None and not isinstance(beschreibung, str):
        befunde.melde("nicht_unterstuetzt",
                      f"{PLUGIN_JSON}: description ist kein Text; wird nicht übernommen",
                      PLUGIN_JSON, None)
        beschreibung = None

    return {
        "id": daten["name"],
        "version": version,
        "commit": _commit(kit),
        "beschreibung": beschreibung,
        "quelle": _quelle(kit),
        "konvention": False,
        "art": "plugin",
    }
