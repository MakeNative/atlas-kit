"""Gültiges JSON als Baum mit Offsets im Text: Scanner (Beleg eines Werts der Hook-Datei) und der Zeilen-Editor der
Werkstatt (paket befehle/hookdatei.py) lesen dieselbe Zerlegung. Vorher prüft der Aufrufer mit json.loads, dass der Text
gültiges JSON ist; hier wird nur noch zerlegt."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

LEERRAUM = " \t\r\n"


@dataclass
class Wert:
    art: str
    anfang: int
    ende: int
    paare: list[Paar] = field(default_factory=list)
    elemente: list[Wert] = field(default_factory=list)

    def paar(self, schluessel: str) -> Paar | None:
        return next((p for p in self.paare if p.schluessel == schluessel), None)


@dataclass
class Paar:
    schluessel: str
    anfang: int
    wert: Wert


class _Leser:
    def __init__(self, text: str):
        self.text = text
        self.i = 0

    def leer(self) -> None:
        while self.i < len(self.text) and self.text[self.i] in LEERRAUM:
            self.i += 1

    def zeichenkette(self) -> tuple[int, int]:
        anfang = self.i
        self.i += 1
        while self.text[self.i] != '"':
            self.i += 2 if self.text[self.i] == "\\" else 1
        self.i += 1
        return anfang, self.i

    def wert(self) -> Wert:
        self.leer()
        zeichen = self.text[self.i]
        anfang = self.i
        if zeichen == "{":
            return self.objekt()
        if zeichen == "[":
            return self.liste()
        if zeichen == '"':
            _, ende = self.zeichenkette()
            return Wert("text", anfang, ende)
        while self.i < len(self.text) and self.text[self.i] not in LEERRAUM + ",]}":
            self.i += 1
        return Wert("zahl" if zeichen in "-0123456789" else "wort", anfang, self.i)

    def objekt(self) -> Wert:
        w = Wert("objekt", self.i, self.i)
        self.i += 1
        self.leer()
        if self.text[self.i] == "}":
            self.i += 1
            w.ende = self.i
            return w
        while True:
            self.leer()
            k_anfang, k_ende = self.zeichenkette()
            self.leer()
            self.i += 1
            w.paare.append(Paar(json.loads(self.text[k_anfang:k_ende]), k_anfang, self.wert()))
            self.leer()
            zeichen = self.text[self.i]
            self.i += 1
            if zeichen == "}":
                w.ende = self.i
                return w

    def liste(self) -> Wert:
        w = Wert("liste", self.i, self.i)
        self.i += 1
        self.leer()
        if self.text[self.i] == "]":
            self.i += 1
            w.ende = self.i
            return w
        while True:
            w.elemente.append(self.wert())
            self.leer()
            zeichen = self.text[self.i]
            self.i += 1
            if zeichen == "]":
                w.ende = self.i
                return w


def zerlege(text: str, anfang: int = 0) -> Wert:
    """Der oberste Wert eines gültigen JSON-Texts ab anfang (z. B. nach einem BOM)."""
    leser = _Leser(text)
    leser.i = anfang
    return leser.wert()


def zeile(text: str, offset: int) -> int:
    """Zeilennummer (1-basiert) eines Offsets, getrennt an \\n wie textdatei.zerlege."""
    return text.count("\n", 0, offset) + 1
