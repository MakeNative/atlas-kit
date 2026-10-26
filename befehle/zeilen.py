"""Zeilen einer Textdatei ändern, ohne andere Zeilen anzufassen (specs/werkstatt.yaml WS-W).

Eine Datei ist eine Liste von Zeilen samt ihrem Zeilenende. Ersetzt wird immer ein Bereich ganzer
Zeilen; neue Zeilen bekommen das Zeilenende der Datei (CRLF bleibt CRLF). Ein BOM am Anfang (Editoren unter
Windows) steht nicht in den Zeilen und kommt beim Schreiben wieder davor. Positionen im Frontmatter kommen aus
yaml.compose (Knoten mit Zeile und Spalte), nie aus einer Schätzung. Braucht PyYAML und läuft
deshalb nur im Server der Werkstatt (Python der eigenen Umgebung).
"""
from __future__ import annotations

import json
import re

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

ZEILE = re.compile(r"[^\n]*\n|[^\n]+$")
BOM = "\ufeff"
TRENNER = "---"
UNSICHER_AM_ANFANG = set("-?:,[]{}#&*!|>'\"%@`")
UNSICHER_IN_FLOW = set(",[]{}")
UEBERSCHRIFT = re.compile(r"^(#{1,6})[ \t]+\S")


class ZeilenFehler(Exception):
    """Eine Stelle lässt sich nicht sicher ändern."""


def ohne_ende(zeile: str) -> str:
    return zeile[:-2] if zeile.endswith("\r\n") else zeile[:-1] if zeile.endswith("\n") else zeile


def ist_leer(zeile: str) -> bool:
    return not ohne_ende(zeile).strip()


def ueberschrift(zeile: str) -> int | None:
    """Ebene einer Markdown-Überschrift (1–6) oder None."""
    m = UEBERSCHRIFT.match(ohne_ende(zeile))
    return len(m.group(1)) if m else None


class Textdatei:
    def __init__(self, text: str):
        self.bom = BOM if text.startswith(BOM) else ""
        self.zeilen: list[str] = ZEILE.findall(text[len(self.bom):])
        erste = next((z for z in self.zeilen if z.endswith("\n")), "\n")
        self.ende = "\r\n" if erste.endswith("\r\n") else "\n"

    @classmethod
    def aus_bytes(cls, inhalt: bytes) -> "Textdatei":
        return cls(inhalt.decode("utf-8"))

    def als_bytes(self) -> bytes:
        return (self.bom + "".join(self.zeilen)).encode("utf-8")

    def __len__(self) -> int:
        return len(self.zeilen)

    def text(self, i: int) -> str:
        return ohne_ende(self.zeilen[i])

    def texte(self) -> list[str]:
        return [ohne_ende(z) for z in self.zeilen]

    def ersetze(self, von: int, bis: int, neu: list[str]) -> None:
        """Zeilen von..bis-1 (0-basiert) durch neue Zeilen ohne Zeilenende ersetzen. Hatte die letzte Zeile der Datei
        kein Zeilenende und reicht der Bereich bis ans Ende, hat die letzte Zeile danach auch keins."""
        ohne_ende_am_schluss = bool(self.zeilen) and not self.zeilen[-1].endswith("\n") and bis >= len(self.zeilen)
        if ohne_ende_am_schluss and von >= len(self.zeilen):
            self.zeilen[-1] += self.ende
        self.zeilen[von:bis] = [z + self.ende for z in neu]
        if ohne_ende_am_schluss and self.zeilen:
            self.zeilen[-1] = ohne_ende(self.zeilen[-1])

    def fuege_ein(self, vor: int, neu: list[str]) -> None:
        self.ersetze(vor, vor, neu)

    def entferne(self, von: int, bis: int) -> None:
        self.ersetze(von, bis, [])

    # ------------------------------------------------------------ Frontmatter

    def frontmatter_ende(self) -> int | None:
        """Index der schließenden '---'-Zeile; None ohne Frontmatter."""
        if not self.zeilen or self.text(0) != TRENNER:
            return None
        for i in range(1, len(self.zeilen)):
            if self.text(i) == TRENNER:
                return i
        return None

    def frontmatter(self) -> "Frontmatter":
        ende = self.frontmatter_ende()
        if ende is None:
            raise ZeilenFehler("Die Datei hat kein Frontmatter zwischen zwei Zeilen '---'.")
        return Frontmatter(self, ende)

    def text_ab(self) -> int:
        """Index der ersten Zeile unter dem Frontmatter (0 ohne Frontmatter)."""
        ende = self.frontmatter_ende()
        return 0 if ende is None else ende + 1


def _letzte_zeile(knoten: Node) -> int:
    """0-basierte Zeile (relativ zum YAML-Text), in der der Knoten endet."""
    if isinstance(knoten, MappingNode) and not knoten.flow_style and knoten.value:
        schluessel, wert = knoten.value[-1]
        return max(_letzte_zeile(schluessel), _letzte_zeile(wert))
    if isinstance(knoten, SequenceNode) and not knoten.flow_style and knoten.value:
        return _letzte_zeile(knoten.value[-1])
    if knoten.start_mark.index == knoten.end_mark.index:
        return knoten.start_mark.line
    ende = knoten.end_mark
    return ende.line if ende.column > 0 else ende.line - 1


class Abbildung:
    """Eine Block-Abbildung im Frontmatter mit absoluten Zeilen."""

    def __init__(self, fm: "Frontmatter", knoten: MappingNode):
        if knoten.flow_style:
            raise ZeilenFehler("Das Feld steht in Kurzform ({ … }); die Werkstatt ändert es nicht.")
        self.fm = fm
        self.knoten = knoten

    @property
    def datei(self) -> Textdatei:
        return self.fm.datei

    def paare(self) -> list[tuple[ScalarNode, Node]]:
        return list(self.knoten.value)

    def schluessel(self) -> list[str]:
        return [k.value for k, _ in self.paare()]

    def paar(self, name: str) -> tuple[ScalarNode, Node] | None:
        for k, v in self.paare():
            if k.value == name:
                return k, v
        return None

    def einzug(self) -> int:
        return self.paare()[0][0].start_mark.column if self.paare() else 0

    def bereich(self, name: str) -> tuple[int, int]:
        k, v = self.paar(name)
        anfang = self.fm.absolut(k.start_mark.line)
        ende = self.fm.absolut(max(_letzte_zeile(k), _letzte_zeile(v)))
        while ende > anfang and ist_leer(self.datei.zeilen[ende]):
            ende -= 1
        return anfang, ende + 1

    def letzte(self) -> int:
        """Absoluter Index der letzten Zeile der Abbildung."""
        ende = self.fm.absolut(_letzte_zeile(self.knoten))
        anfang = self.fm.absolut(self.knoten.start_mark.line)
        while ende > anfang and ist_leer(self.datei.zeilen[ende]):
            ende -= 1
        return ende

    def unter(self, name: str) -> "Abbildung | None":
        paar = self.paar(name)
        if paar is None or not isinstance(paar[1], MappingNode):
            return None
        return Abbildung(self.fm, paar[1])

    def setze(self, name: str, wert: str) -> None:
        """Ersetzt das Feld (alle seine Zeilen) durch eine Zeile 'name: wert' oder hängt es an."""
        paar = self.paar(name)
        if paar is None:
            self.datei.fuege_ein(self.letzte() + 1, [" " * self.einzug() + f"{name}: {wert}"])
            return
        anfang, ende = self.bereich(name)
        vorne = self.datei.text(anfang)[:paar[0].start_mark.column]
        self.datei.ersetze(anfang, ende, [f"{vorne}{name}: {wert}"])

    def setze_zeilen(self, name: str, zeilen: list[str]) -> None:
        """Ersetzt das Feld durch mehrere Zeilen (erste mit 'name:'), Einzug wie die Geschwister."""
        einzug = " " * self.einzug()
        paar = self.paar(name)
        if paar is None:
            self.datei.fuege_ein(self.letzte() + 1, [einzug + z for z in zeilen])
            return
        anfang, ende = self.bereich(name)
        vorne = self.datei.text(anfang)[:paar[0].start_mark.column]
        self.datei.ersetze(anfang, ende, [vorne + zeilen[0], *(einzug + z for z in zeilen[1:])])

    def entferne(self, name: str) -> None:
        if self.paar(name) is None:
            return
        anfang, _ = self.bereich(name)
        vorne = self.datei.text(anfang)[:self.paar(name)[0].start_mark.column]
        if vorne.strip():
            raise ZeilenFehler(f"Das Feld {name} steht am Anfang eines Listeneintrags und lässt sich nicht allein entfernen.")
        self.datei.entferne(*self.bereich(name))


class Frontmatter:
    def __init__(self, datei: Textdatei, ende: int):
        self.datei = datei
        self.ende = ende
        quelle = "".join(datei.zeilen[1:ende])
        try:
            knoten = yaml.compose(quelle, Loader=yaml.SafeLoader)
        except yaml.YAMLError as fehler:
            raise ZeilenFehler(f"Das Frontmatter ist kein lesbares YAML ({fehler.__class__.__name__}).")
        if knoten is None:
            self.wurzel = None
        elif isinstance(knoten, MappingNode) and not knoten.flow_style:
            self.wurzel = Abbildung(self, knoten)
        else:
            raise ZeilenFehler("Das Frontmatter ist keine Abbildung.")

    def absolut(self, zeile: int) -> int:
        return zeile + 1

    def werte(self) -> dict:
        text = "".join(self.datei.zeilen[1:self.ende])
        try:
            daten = yaml.safe_load(text)
        except yaml.YAMLError:
            return {}
        return daten if isinstance(daten, dict) else {}

    def setze(self, name: str, wert: str) -> None:
        if self.wurzel is None:
            self.datei.fuege_ein(self.ende, [f"{name}: {wert}"])
        else:
            self.wurzel.setze(name, wert)

    def haenge_an(self, zeilen: list[str]) -> None:
        """Neue Zeilen direkt vor das schließende '---'."""
        self.datei.fuege_ein(self.ende, zeilen)


# ---------------------------------------------------------------- YAML-Werte schreiben

def _laedt_als(text: str, wert: object) -> bool:
    try:
        return yaml.safe_load(text) == wert
    except yaml.YAMLError:
        return False


def skalar(wert: str, in_flow: bool = False) -> str:
    """Text als YAML-Skalar: Klartext, wenn YAML ihn unverändert liest, sonst doppelt gequotet."""
    klar = (
        wert
        and wert == wert.strip()
        and wert[0] not in UNSICHER_AM_ANFANG
        and ": " not in wert
        and " #" not in wert
        and not wert.endswith(":")
        and "\t" not in wert
        and not (in_flow and set(wert) & UNSICHER_IN_FLOW)
        and _laedt_als(f"x: {wert}", {"x": wert})
        and (not in_flow or _laedt_als(f"x: [{wert}]", {"x": [wert]}))
    )
    return wert if klar else json.dumps(wert, ensure_ascii=False)


def flow_liste(werte: list[str]) -> str:
    return "[" + ", ".join(skalar(w, in_flow=True) for w in werte) + "]"


def bei_befund(zurueck: str, grenze: int | None) -> str:
    teile = [f"zurueck: {skalar(zurueck, in_flow=True)}"]
    if grenze is not None:
        teile.append(f"max: {grenze}")
    return "{ " + ", ".join(teile) + " }"
