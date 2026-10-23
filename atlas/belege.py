"""Belegprüfung vor dem Schreiben (SC-B1): dieselben Regeln wie ST-1, ST-3 und ST-4.

Im Normalfall entsteht hier nichts. Ein Befund beleg_falsch zeigt einen Fehler im
Scanner an (SC-B2).
"""
from __future__ import annotations

from pathlib import Path

from befunde import Befunde
from textdatei import lies_zeilen


class _Dateien:
    def __init__(self, kit: Path):
        self.kit = kit
        self._cache: dict[str, list[str] | None] = {}

    def zeilen(self, datei) -> list[str] | None:
        if not isinstance(datei, str):
            return None
        if datei not in self._cache:
            try:
                (self.kit / datei).resolve().relative_to(self.kit.resolve())
                self._cache[datei] = lies_zeilen(self.kit, datei)
            except (OSError, ValueError, UnicodeDecodeError):
                self._cache[datei] = None
        return self._cache[datei]


def _zitat_stimmt(dateien: _Dateien, beleg: dict) -> bool:
    zeilen = dateien.zeilen(beleg.get("datei"))
    zeile = beleg.get("zeile")
    zitat = beleg.get("zitat")
    return (zeilen is not None and isinstance(zeile, int) and not isinstance(zeile, bool)
            and 1 <= zeile <= len(zeilen) and isinstance(zitat, str) and zitat in zeilen[zeile - 1])


def pruefe_belege(kit: Path, knoten: list[dict], kanten: list[dict],
                  befunde: Befunde) -> tuple[list[dict], list[dict]]:
    """Gibt Knoten und Kanten zurück, die die Prüfung bestehen (Knoten ggf. ohne beleg)."""
    dateien = _Dateien(kit)
    ids = {k["id"] for k in knoten}

    for k in knoten:
        beleg = k.get("beleg")
        if isinstance(beleg, dict) and "zitat" in beleg and not _zitat_stimmt(dateien, beleg):
            befunde.melde("beleg_falsch",
                          f"Das Zitat am Knoten {k['id']} steht nicht in {beleg.get('datei')} "
                          f"Zeile {beleg.get('zeile')}; der Beleg entfällt",
                          beleg.get("datei") or k.get("datei") or "", beleg.get("zeile"))
            del k["beleg"]
            if k.get("typ") == "hook":
                k.pop("bewacht", None)
        rolle_beleg = k.get("rolle_beleg")
        if isinstance(rolle_beleg, dict) and not _zitat_stimmt(dateien, rolle_beleg):
            befunde.melde("beleg_falsch",
                          f"Das Zitat der Rolle am Knoten {k['id']} steht nicht in {rolle_beleg.get('datei')} "
                          f"Zeile {rolle_beleg.get('zeile')}; die Rolle entfällt",
                          rolle_beleg.get("datei") or k.get("datei") or "", rolle_beleg.get("zeile"))
            for feld in ("rolle", "rolle_herkunft", "rolle_beleg"):
                k.pop(feld, None)

    behalten = []
    for kante in kanten:
        beleg = kante.get("beleg") or {}
        grund = None
        if not _zitat_stimmt(dateien, beleg):
            grund = f"das Zitat steht nicht in {beleg.get('datei')} Zeile {beleg.get('zeile')}"
        elif kante.get("herkunft") in ("konvention", "textauswertung", "abgeleitet") and not str(beleg.get("zitat", "")).strip():
            grund = "das Zitat ist leer"
        else:
            for feld in ("von", "nach"):
                wert = kante.get(feld)
                if not (isinstance(wert, str) and wert.startswith("pfad:")) and wert not in ids:
                    grund = f"{feld} {wert} ist kein Knoten"
                    break
        if grund is None:
            behalten.append(kante)
        else:
            befunde.melde("beleg_falsch",
                          f"Kante {kante.get('von')} -{kante.get('typ')}-> {kante.get('nach')}: "
                          f"{grund}; die Kante entfällt",
                          beleg.get("datei") or "", beleg.get("zeile"))
    return knoten, behalten
