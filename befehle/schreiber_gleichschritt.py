"""Bild und Text im Gleichschritt bei Änderungen von Hand (specs/werkstatt.yaml WS-W5, WS-W7).

Ändert schritt_aendern den Ausführenden oder die Mensch-Art oder schleife_setzen die Schleife, gilt in beiden
Skill-Arten dieselbe Regel für die Zeilen des Schritts (Abschnitt bzw. Prosa-Block):
1. Steht dort der Satz, den die Werkstatt selbst erzeugt (Startsatz, Mensch-Satz, Schleifen-Satz, vokabular), wird er
   ersetzt bzw. entfernt.
2. Sonst wird ein alter Agent bzw. eine alte Skill, die dort genau in Backticks oder als subagent_type steht, durch den
   neuen Namen ersetzt (nur ein alter und ein neuer Name derselben Art).
3. Sonst bleibt der Text, und die Vorschau warnt, dass er das Weggefallene noch nennt (Bestätigung nötig).
"""
from __future__ import annotations

import re

import vokabular as vok
from zeilen import Textdatei

# Ausführende, deren Name sich in einer Zeile ersetzen lässt: (Feld, Muster mit dem Namen als Gruppe 2).
_NAME_IN_ZEILE = (r"(`(?:[a-z0-9-]+:)?){name}(`)", r"(subagent_type:\s*[\"'](?:[a-z0-9-]+:)?){name}([\"'])")


def ausfuehrender(schritt: dict) -> dict:
    return {f: schritt[f] for f in vok.AUSFUEHRENDE if schritt.get(f)}


def agenten_von(schritt: dict) -> set[str]:
    """Die Agenten, die ein Schritt startet, wie der Scanner sie liest: agent als Text, parallel nur als Liste, darin
    nur Texte; alles andere zählt wie fehlend."""
    agent, parallel = schritt.get("agent"), schritt.get("parallel")
    namen = {agent} if isinstance(agent, str) and agent else set()
    return namen | ({n for n in parallel if isinstance(n, str)} if isinstance(parallel, list) else set())


def _einziger_name(schritt: dict) -> tuple[str, str] | None:
    if schritt.get("agent"):
        return "agent", schritt["agent"]
    if schritt.get("skill"):
        return "skill", schritt["skill"]
    return None


class Treffer:
    """Ergebnis eines Abgleichs: Zeile (0-basiert) mit dem neuen Text bzw. None; entfernt: die Zeile ist weggefallen."""

    def __init__(self, zeile: int | None, entfernt: bool = False):
        self.zeile = zeile
        self.entfernt = entfernt


class GleichschrittMixin:
    @staticmethod
    def _setze_zeile(datei: Textdatei, i: int, text: str) -> Treffer:
        """Neuer Text der Zeile i; ist er leer, fällt die Zeile weg."""
        if text.strip():
            datei.ersetze(i, i + 1, [text])
            return Treffer(i)
        datei.entferne(i, i + 1)
        return Treffer(i, entfernt=True)

    def _satz_tauschen(self, datei: Textdatei, zeilen: list[int], finde, neu: str | None) -> Treffer | None:
        """Regel 1 für Mensch- und Schleifen-Satz: erster erzeugter Satz in den Zeilen → neu bzw. weg."""
        for i in zeilen:
            treffer = finde(datei.text(i))
            if treffer:
                return self._setze_zeile(datei, i, vok.ersetze_satz(datei.text(i), treffer, neu))
        return None

    def _ausfuehrenden_tauschen(self, datei: Textdatei, zeilen: list[int], alt: dict, neu: dict) -> Treffer | None:
        """Regeln 1 und 2 für den Ausführenden; None, wenn der Text den alten nirgends so nennt."""
        if not alt:
            return None
        for i in zeilen:
            text = datei.text(i)
            treffer = vok.finde_startsatz(text, alt)
            if not treffer:
                continue
            kopf = vok.aufruf_kopf(neu)
            if kopf is not None:
                anfang = treffer.start() + len(treffer.group("vor"))
                return self._setze_zeile(datei, i, text[:anfang] + kopf + text[treffer.end("zeichen") - 1:])
            if treffer.group("zeichen") == ":":
                rest = text[treffer.end():].lstrip()
                return self._setze_zeile(datei, i, text[:treffer.start()] + treffer.group("vor") + rest)
            return self._setze_zeile(datei, i, vok.ersetze_satz(text, treffer, None))
        alter, neuer = _einziger_name(alt), _einziger_name(neu)
        if alter is None or neuer is None or alter[0] != neuer[0] or alter[1] == neuer[1]:
            return None
        muster = [re.compile(m.format(name=re.escape(alter[1]))) for m in _NAME_IN_ZEILE]
        erste = None
        for i in zeilen:
            text = datei.text(i)
            neu_text = text
            for m in muster:
                neu_text = m.sub(lambda t: f"{t.group(1)}{neuer[1]}{t.group(2)}", neu_text)
            if neu_text != text:
                datei.ersetze(i, i + 1, [neu_text])
                erste = i if erste is None else erste
        return Treffer(erste) if erste is not None else None
