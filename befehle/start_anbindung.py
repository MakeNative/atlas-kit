"""Anbindung der Start-App an Ticket 21 (specs/start-app.yaml begriffe.anbindung): die einzige Stelle, die dessen
Funktionen aufruft. Server und Seite kennen nur diese drei Funktionen.

- zuletzt(daten, home): die zuletzt geöffneten Kits als [{kit_id, pfad}] (höchstens 10, neueste zuerst)
- kann_anlegen(): legt diese Fassung neue Kits an (/atlas:neu)?
- pruefe_name(name): None, wenn der Name passt, sonst ein Satz für die Seite
"""
from __future__ import annotations

from pathlib import Path

NAME_PASST_NICHT = ("Der Name passt so nicht. Nimm Kleinbuchstaben, Ziffern und Bindestriche, zum Beispiel "
                    "belege-pruefen.")

try:  # Ticket 21: befehle/zuletzt.py
    import zuletzt as _zuletzt
except ImportError:  # pragma: no cover - vor dem Zusammenführen mit Ticket 21
    _zuletzt = None

try:  # Ticket 21: befehle/kit_anlegen.py
    import kit_anlegen as _kit_anlegen
except ImportError:  # pragma: no cover
    _kit_anlegen = None


def zuletzt(daten: Path, home: Path) -> list[dict]:
    """daten ist der Datenordner des Plugins (dort liegt werkstatt-zuletzt.json aus Ticket 21)."""
    if _zuletzt is None:
        return []
    return [{"kit_id": kit.kit_id, "pfad": str(kit.ordner)} for kit in _zuletzt.kits(daten, home)]


def kann_anlegen() -> bool:
    return _kit_anlegen is not None


def pruefe_name(name: object) -> str | None:
    if _kit_anlegen is None:
        return "Neue Kits legt diese Fassung noch nicht an."
    if not isinstance(name, str) or not _kit_anlegen.name_gueltig(name):
        return NAME_PASST_NICHT
    return None
