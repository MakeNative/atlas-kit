"""Vorgelegte Änderungssätze (Vorschläge) der Werkstatt, je mit eigener Kennung (specs/werkstatt.yaml WS-A7).

Mehrere Vorschläge können nebeneinander liegen; von Hand und von der KI zählen getrennt (je höchstens 20). Übernehmen und Verwerfen
gelten genau einer Kennung; ein neuer Vorschlag ersetzt nie einen anderen. Die Herkunft setzt der Server
nach dem Schlüssel der Anfrage, nicht der Absender. Nur Standardbibliothek.
"""
from __future__ import annotations

import copy
import threading
import time

OFFEN_HOECHSTENS = 20
AUSGANG_HOECHSTENS = 200
HERKUNFT_TEXT = {"hand": "von Hand", "ki": "der KI"}


class VorschlagFehler(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
        self.text = text


class Vorschlaege:
    def __init__(self) -> None:
        self._liste: list[dict] = []
        # Ausgang erledigter Vorschläge (übernommen mit gewählten Einträgen, verworfen), für das KI-Panel (WS-K13)
        self._ausgang: dict[str, dict] = {}
        self._nummer = 0
        self._sperre = threading.Lock()

    def lege_vor(self, satz: dict, herkunft: str, *, gedaechtnis: bool = False, pipeline: dict | None = None) -> str:
        with self._sperre:
            if sum(1 for e in self._liste if e["herkunft"] == herkunft) >= OFFEN_HOECHSTENS:
                raise VorschlagFehler(409, f"Es liegen schon {OFFEN_HOECHSTENS} Vorschläge {HERKUNFT_TEXT.get(herkunft, '')} "
                                           f"vor. Übernimm oder verwirf zuerst einen.".replace("  ", " "))
            self._nummer += 1
            eintrag = {"id": f"v{self._nummer}", "herkunft": herkunft, "zeit": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "satz": {**copy.deepcopy(satz), "herkunft": herkunft}}
            if gedaechtnis:
                eintrag["gedaechtnis"] = True
            if pipeline is not None:
                eintrag["pipeline"] = dict(pipeline)
            self._liste.append(eintrag)
            return eintrag["id"]

    def liste(self) -> list[dict]:
        with self._sperre:
            return copy.deepcopy(self._liste)

    def hole(self, vid: object) -> dict:
        with self._sperre:
            for eintrag in self._liste:
                if eintrag["id"] == vid:
                    return copy.deepcopy(eintrag)
        raise VorschlagFehler(404, "Diesen Vorschlag gibt es nicht (mehr).")

    def entferne(self, vid: object, ausgang: dict | None = None) -> bool:
        """Nimmt den Vorschlag aus der Liste; mit ausgang ({art: uebernommen, gewaehlt, von} bzw. {art: verworfen})
        bleibt vermerkt, was aus ihm wurde."""
        with self._sperre:
            vorher = len(self._liste)
            self._liste = [e for e in self._liste if e["id"] != vid]
            weg = len(self._liste) != vorher
            if weg and ausgang is not None and isinstance(vid, str):
                self._ausgang[vid] = dict(ausgang)
                while len(self._ausgang) > AUSGANG_HOECHSTENS:
                    del self._ausgang[next(iter(self._ausgang))]
            return weg

    def ausgang(self, vid: object) -> dict | None:
        with self._sperre:
            gefunden = self._ausgang.get(vid) if isinstance(vid, str) else None
            return dict(gefunden) if gefunden is not None else None
