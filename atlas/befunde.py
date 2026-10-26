"""Befunde: was der Scanner nicht belegen kann oder was sich widerspricht.

Ein Befund ändert nie still Daten. Was er betrifft, fehlt im Bauplan oder steht
ausdrücklich als null darin (specs/scanner.yaml, befunde).
"""
from __future__ import annotations

from dataclasses import dataclass, field

ARTEN = (
    "widerspruch",
    "verweis_fehlt",
    "pflichtfeld_fehlt",
    "nicht_lesbar",
    "nicht_unterstuetzt",
    "beleg_falsch",
    "ohne_beleg",
)
ORT = ("herkunft", "skill", "schritt", "aussage", "ziel_art", "ziel")


@dataclass
class Befund:
    art: str
    text: str
    datei: str          # relativ zum Kit, mit /
    zeile: int | None
    herkunft: str | None = None
    # Wo ein Befund sitzt und worauf er zielt: Skill und Schritt-id, die Angabe (aussage, z. B. agent, zurueck,
    # atlas.rolle, frontmatter) und das Ziel (ziel_art: agent, skill, werkzeug, schritt, …; ziel: sein Name).
    # Die Werkstatt erkennt einen Befund an diesen Feldern wieder, nicht am Text (befund_identitaet).
    skill: str | None = None
    schritt: str | None = None
    aussage: str | None = None
    ziel_art: str | None = None
    ziel: str | None = None

    def als_dict(self) -> dict:
        eintrag = {"art": self.art, "text": self.text, "datei": self.datei, "zeile": self.zeile}
        for feld in ORT:
            if getattr(self, feld) is not None:
                eintrag[feld] = getattr(self, feld)
        return eintrag


class Befunde:
    """Sammelt Befunde in der Reihenfolge ihres Entstehens."""

    def __init__(self) -> None:
        self._liste: list[Befund] = []
        self._skill: str | None = None

    def mit_skill(self, skill: str) -> Befunde:
        """Dieselbe Sammlung; jede Meldung über sie nennt die Skill (deren Ablauf gerade gelesen wird)."""
        sicht = Befunde()
        sicht._liste = self._liste
        sicht._skill = skill
        return sicht

    def melde(self, art: str, text: str, datei: str, zeile: int | None = None,
              herkunft: str | None = None, *, aussage: str, skill: str | None = None, schritt: str | None = None,
              ziel_art: str | None = None, ziel: str | None = None) -> None:
        """aussage ist Pflicht: Jede Meldestelle sagt, welche Angabe der Befund betrifft."""
        if art not in ARTEN:
            raise ValueError(f"unbekannte Befund-Art: {art}")
        skill = skill if skill is not None else self._skill
        self._liste.append(Befund(art, text, datei, zeile, herkunft, skill, schritt, aussage, ziel_art, ziel))

    def __len__(self) -> int:
        return len(self._liste)

    def als_liste(self) -> list[dict]:
        """Sortiert nach datei, dann zeile; Befunde ohne Zeile stehen je Datei vorn."""
        sortiert = sorted(
            self._liste,
            key=lambda b: (b.datei, b.zeile is not None, b.zeile or 0),
        )
        return [b.als_dict() for b in sortiert]


@dataclass(frozen=True)
class Rueck:
    """Umbenennungen eines Änderungssatzes rückwärts: neue Datei → Datei vorher, neuer Agent-Name → Name vorher."""
    dateien: dict[str, str] = field(default_factory=dict)
    namen: dict[str, str] = field(default_factory=dict)


def befund_identitaet(befund: dict, rueck: Rueck | None = None) -> tuple:
    """Wer ein Befund ist, aus seinen Feldern (nie aus dem Text, nie aus der Zeile, die sich verschiebt): art, datei,
    skill, schritt, aussage, ziel_art, ziel. Mit rueck zählen Datei und Agent einer Umbenennung unter dem Namen
    vorher."""
    rueck = rueck or Rueck()
    datei, ziel = befund.get("datei"), befund.get("ziel")
    if befund.get("ziel_art") == "agent":
        ziel = rueck.namen.get(ziel, ziel)
    return (befund.get("art"), rueck.dateien.get(datei, datei), befund.get("skill"), befund.get("schritt"),
            befund.get("aussage"), befund.get("ziel_art"), ziel)
