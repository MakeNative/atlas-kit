"""Befunde: was der Scanner nicht belegen kann oder was sich widerspricht.

Ein Befund ändert nie still Daten. Was er betrifft, fehlt im Bauplan oder steht
ausdrücklich als null darin (specs/scanner.yaml, befunde).
"""
from __future__ import annotations

from dataclasses import dataclass

ARTEN = (
    "widerspruch",
    "verweis_fehlt",
    "pflichtfeld_fehlt",
    "nicht_lesbar",
    "nicht_unterstuetzt",
    "beleg_falsch",
    "ohne_beleg",
)


@dataclass
class Befund:
    art: str
    text: str
    datei: str          # relativ zum Kit, mit /
    zeile: int | None
    herkunft: str | None = None

    def als_dict(self) -> dict:
        eintrag = {"art": self.art, "text": self.text, "datei": self.datei, "zeile": self.zeile}
        if self.herkunft is not None:
            eintrag["herkunft"] = self.herkunft
        return eintrag


class Befunde:
    """Sammelt Befunde in der Reihenfolge ihres Entstehens."""

    def __init__(self) -> None:
        self._liste: list[Befund] = []

    def melde(self, art: str, text: str, datei: str, zeile: int | None = None,
              herkunft: str | None = None) -> None:
        if art not in ARTEN:
            raise ValueError(f"unbekannte Befund-Art: {art}")
        self._liste.append(Befund(art, text, datei, zeile, herkunft))

    def __len__(self) -> int:
        return len(self._liste)

    def als_liste(self) -> list[dict]:
        """Sortiert nach datei, dann zeile; Befunde ohne Zeile stehen je Datei vorn."""
        sortiert = sorted(
            self._liste,
            key=lambda b: (b.datei, b.zeile is not None, b.zeile or 0),
        )
        return [b.als_dict() for b in sortiert]
