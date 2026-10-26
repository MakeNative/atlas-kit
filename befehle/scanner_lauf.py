"""Den Scanner des Pakets als eigenen Prozess in einen Temp-Ordner laufen lassen, an einer Stelle.

Genutzt von atlas_kit.py (scan, render, analyse, init) und von der Werkstatt. Nur Standardbibliothek.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

SCANNER = Path("atlas") / "scan.py"
LAUFZEIT_HOECHSTENS_S = 600
# So lange braucht die Werkstatt auf jedem Weg der Claude-Code-Sitzung höchstens für Sperre und Einlesen (Anlegen der
# Arbeitskopie, Prüfung einer Antwort samt allen Vorschlägen eines Gutachtens); die Befehle der Sitzung warten etwas
# länger (werkstatt_sitzung.ANTWORT_WARTEN_S), unter 600 s.
PRUEFUNG_HOECHSTENS_S = 240.0


class ScanFehler(Exception):
    """zeile: erste Zeile der Meldung; zeit_um: der Lauf wurde nach seiner Zeitgrenze abgebrochen."""

    def __init__(self, zeile: str, zeit_um: bool = False):
        super().__init__(zeile)
        self.zeile = zeile
        self.zeit_um = zeit_um


def erste_zeile(stderr: str, exit_code: int) -> str:
    zeilen = [z.strip() for z in stderr.splitlines() if z.strip()]
    if not zeilen:
        return f"Exit {exit_code}"
    if zeilen[0].startswith("Traceback"):
        return zeilen[-1]
    return zeilen[0]


def scanne(python: Path | str, scanner: Path, kit: Path, ableitung: Path | None = None,
           home: Path | None = None, laufzeit: float | None = None) -> tuple[bytes, dict]:
    """Liest das Kit. Gibt (atlas.json als Bytes, Bericht) zurück; wirft ScanFehler mit der ersten Zeile.

    laufzeit begrenzt den Lauf weiter als LAUFZEIT_HOECHSTENS_S (Restzeit einer Prüfung)."""
    grenze = LAUFZEIT_HOECHSTENS_S if laufzeit is None else min(LAUFZEIT_HOECHSTENS_S, laufzeit)
    umgebung = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    if home is not None:
        umgebung["HOME"] = str(home)
    with tempfile.TemporaryDirectory(prefix="atlas-kit-") as temp:
        ziel = Path(temp) / "bauplan.json"
        bericht = Path(temp) / "bericht.json"
        befehl = [str(python), "-X", "utf8", "-B", "-E", "-s", str(scanner), str(kit), str(ziel),
                  "--bericht", str(bericht)]
        if ableitung is not None:
            befehl += ["--ableitung", str(ableitung)]
        try:
            lauf = subprocess.run(befehl, cwd=temp, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=umgebung, timeout=grenze)
        except subprocess.TimeoutExpired:
            raise ScanFehler(f"Abbruch nach {grenze:g} s", zeit_um=True)
        if lauf.returncode != 0 or not ziel.is_file() or not bericht.is_file():
            raise ScanFehler(erste_zeile(lauf.stderr, lauf.returncode))
        return ziel.read_bytes(), json.loads(bericht.read_text(encoding="utf-8"))
