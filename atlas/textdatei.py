"""Eine Datei des Kits als Zeilen lesen, überall auf dieselbe Weise.

Zeilennummern im Bauplan beziehen sich auf genau diese Zerlegung: UTF-8, Trennung an
\\n, ein \\r am Zeilenende wird entfernt. Zeile n ist zeilen[n - 1].
"""
from __future__ import annotations

from pathlib import Path


def zerlege(text: str) -> list[str]:
    return [z[:-1] if z.endswith("\r") else z for z in text.split("\n")]


def lies_zeilen(kit: Path, datei: str) -> list[str]:
    """Wirft OSError oder UnicodeDecodeError, wenn die Datei nicht lesbar ist."""
    with open(kit / datei, encoding="utf-8", newline="") as f:
        return zerlege(f.read())
