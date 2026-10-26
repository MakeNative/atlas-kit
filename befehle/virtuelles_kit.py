"""Virtuelles Kit der Werkstatt: Dateien bei Bedarf von der Platte, Änderungen im Speicher (WS-W).

Ordner, Muster und Namensregeln kommen aus dem Scanner (ordner.Ordner, vokabular), der Pfadschutz aus
werkstatt_pfade. Nichts hier schreibt auf die Platte.
"""
from __future__ import annotations

import copy
import difflib
import json
from pathlib import Path

import werkstatt_pfade as pfade
import zeilen as z
from ableitung import abgeleitete_schritte
from ordner import Ordner
from vokabular import AGENT_MUSTER, SKILL_DATEI, name_oder
from werkstatt_pfade import ABLEITUNG, sha
from zeilen import Textdatei, ZeilenFehler

SATZZEICHEN_AM_ENDE = (".", "!", "?", ":")


class SchreibFehler(Exception):
    """Ein Eintrag lässt sich nicht anwenden. fehlt nennt einen Bezug, den ein anderer Eintrag anlegen könnte."""

    def __init__(self, text: str, fehlt: tuple | None = None):
        super().__init__(text)
        self.text = text
        self.fehlt = fehlt


def zeilen_der_anweisung(anweisung: str) -> list[str]:
    return [zeile.rstrip() for zeile in anweisung.strip().splitlines() if zeile.strip()]


def mit_punkt(titel: str) -> str:
    return titel if titel.endswith(SATZZEICHEN_AM_ENDE) else f"{titel}."


class VirtuellesKit:
    def __init__(self, ordner: Ordner, kit_id: str, ableitung_pfad: Path | None, ableitung: dict | None):
        self.wurzel = ordner.wurzel
        self.ordner = ordner
        self.art = ordner.art
        self.kit_id = kit_id
        self.skills_ordner = ordner.skills
        self.agenten_ordner = ordner.agenten
        self.ableitung_pfad = ableitung_pfad
        self._ableitung_original = ableitung
        self.ableitung = copy.deepcopy(ableitung)
        self._neu: dict[str, bytes | None] = {}
        # Inhalt der Platte beim ersten Lesen; Vorschau und Prüfsumme (gelesen) beziehen sich auf genau diesen Stand.
        self._platte: dict[str, bytes | None] = {}
        self._ableitung_platte: tuple[bytes | None] | None = None
        self.gelesen: dict[str, str | None] = {}
        self.zurueckgesetzt = 0  # zählt zuruecksetzen(); ein gemerkter Stand von aenderungen() gilt nur bis dahin
        # Name aus dem Frontmatter je Datei, gemerkt mit dem Inhalt, aus dem er stammt: agenten() und skills() lesen
        # das YAML nur neu, wenn sich die Datei geändert hat.
        self._namen: dict[str, tuple[bytes | None, str]] = {}

    # ------------------------------------------------------------ Dateien

    def _von_platte(self, rel: str) -> bytes | None:
        if rel in self._platte:
            return self._platte[rel]
        pfad = self.wurzel / rel
        try:
            inhalt = pfad.read_bytes() if pfad.is_file() else None
        except OSError:
            inhalt = None
        self._platte[rel] = inhalt
        self.gelesen.setdefault(rel, sha(inhalt))
        return inhalt

    def von_platte(self, rel: str) -> bytes | None:
        """Inhalt auf der Platte, ohne die Änderungen dieses Satzes."""
        return self._von_platte(rel)

    def lies(self, rel: str) -> bytes | None:
        if rel in self._neu:
            return self._neu[rel]
        return self._von_platte(rel)

    def existiert(self, rel: str) -> bool:
        return self.lies(rel) is not None

    def textdatei(self, rel: str) -> Textdatei:
        inhalt = self.lies(rel)
        if inhalt is None:
            raise SchreibFehler(f"{rel} gibt es nicht.")
        try:
            return Textdatei.aus_bytes(inhalt)
        except UnicodeDecodeError:
            raise SchreibFehler(f"{rel} ist nicht als UTF-8 lesbar.")

    def pruefe_ziel(self, rel: str) -> None:
        """WS-X5: nur unter dem Kit, kein Symlink auf dem Weg, kein '..'."""
        try:
            pfade.pruefe_freigabe(rel, self.skills_ordner, self.agenten_ordner)
            pfade.pruefe_ziel(self.wurzel, rel)
        except pfade.PfadFehler as fehler:
            raise SchreibFehler(str(fehler))

    def schreibe(self, rel: str, inhalt: bytes) -> None:
        self.pruefe_ziel(rel)
        self.lies(rel)
        self._neu[rel] = inhalt

    def schreibe_text(self, rel: str, datei: Textdatei) -> None:
        self.schreibe(rel, datei.als_bytes())

    def loesche(self, rel: str) -> None:
        self.pruefe_ziel(rel)
        self.lies(rel)
        self._neu[rel] = None

    def dateien_in(self, ordner: str, muster: str) -> list[str]:
        """Dateien eines Ordners (eine Ebene bzw. <ordner>/*/SKILL.md), Platte plus neue, ohne gelöschte. Die Platte
        liest der Ordner des Scanners (Ordner.agent_dateien bzw. skill_dateien), damit beide dieselben Dateien sehen."""
        auf_platte = self.ordner.agent_dateien() if muster == AGENT_MUSTER else self.ordner.skill_dateien()
        gefunden = {p.relative_to(self.wurzel).as_posix() for p in auf_platte}
        tiefe = 1 if muster == AGENT_MUSTER else 2
        for rel in self._neu:
            teile = rel.split("/")
            if rel.startswith(ordner + "/") and len(teile) == len(ordner.split("/")) + tiefe:
                if muster == AGENT_MUSTER and rel.endswith(".md") or muster != AGENT_MUSTER and rel.endswith(f"/{SKILL_DATEI}"):
                    gefunden.add(rel)
        return sorted(rel for rel in gefunden if self.existiert(rel))

    # ------------------------------------------------------------ Verzeichnisse

    def _name_aus(self, rel: str, rueckfall: str) -> str:
        inhalt = self.lies(rel)
        gemerkt = self._namen.get(rel)
        if gemerkt is not None and gemerkt[0] is inhalt:
            return gemerkt[1]
        try:
            datei = self.textdatei(rel)
            name = name_oder(datei.frontmatter().werte().get("name"), rueckfall)
        except (SchreibFehler, ZeilenFehler):
            name = rueckfall
        self._namen[rel] = (inhalt, name)
        return name

    def agenten(self) -> dict[str, str]:
        verzeichnis: dict[str, str] = {}
        for rel in self.dateien_in(self.agenten_ordner, AGENT_MUSTER):
            verzeichnis.setdefault(self._name_aus(rel, Path(rel).stem), rel)
        return verzeichnis

    def skills(self) -> dict[str, str]:
        verzeichnis: dict[str, str] = {}
        for rel in self.dateien_in(self.skills_ordner, SKILL_DATEI):
            verzeichnis.setdefault(self._name_aus(rel, rel.split("/")[-2]), rel)
        return verzeichnis

    def werkzeuge(self) -> set[str]:
        return {p.relative_to(self.wurzel).as_posix() for p in self.ordner.werkzeug_dateien()}

    def ableitung_von(self, skill: str) -> list | None:
        """Die abgeleiteten Schritte der Skill (ableitung.abgeleitete_schritte, dieselbe Liste); None ohne Schritte."""
        if not isinstance(self.ableitung, dict):
            return None
        return abgeleitete_schritte(self.ableitung.get("skills", {}).get(skill)) or None

    # ------------------------------------------------------------ Stand

    def schnappschuss(self) -> tuple:
        return dict(self._neu), copy.deepcopy(self.ableitung)

    def zuruecksetzen(self, stand: tuple) -> None:
        self._neu, self.ableitung = dict(stand[0]), copy.deepcopy(stand[1])
        self.zurueckgesetzt += 1

    def ableitung_als_bytes(self, daten: dict | None) -> bytes | None:
        return None if daten is None else (json.dumps(daten, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def aenderungen(self) -> dict[str, tuple[bytes | None, bytes | None]]:
        """Schlüssel → (vorher, nachher); nur echte Änderungen. Die Ableitung heißt ABLEITUNG."""
        ergebnis = {}
        for rel, neu in self._neu.items():
            alt = self._von_platte(rel)
            if alt != neu:
                ergebnis[rel] = (alt, neu)
        if self.ableitung != self._ableitung_original and self.ableitung_pfad is not None:
            if self._ableitung_platte is None:
                try:
                    self._ableitung_platte = (self.ableitung_pfad.read_bytes(),)
                except OSError:
                    self._ableitung_platte = (None,)
            ergebnis[ABLEITUNG] = (self._ableitung_platte[0], self.ableitung_als_bytes(self.ableitung))
        return ergebnis


def anzeige(schluessel: str, ableitung_pfad: Path | None) -> str:
    if schluessel == ABLEITUNG and ableitung_pfad is not None:
        return f"{ableitung_pfad.parent.name}/{ableitung_pfad.name}"
    return schluessel


def diff(pfad: str, alt: bytes | None, neu: bytes | None) -> str:
    """Nur zur Anzeige: Bytes, die kein UTF-8 sind (z. B. eine alte Latin-1-Datei, die wegfällt), werden ersetzt."""
    a = [] if alt is None else [z.ohne_ende(x) for x in Textdatei(alt.decode("utf-8", "replace")).zeilen]
    b = [] if neu is None else [z.ohne_ende(x) for x in Textdatei(neu.decode("utf-8", "replace")).zeilen]
    zeilen = difflib.unified_diff(a, b, fromfile=f"a/{pfad}" if alt is not None else "/dev/null",
                                  tofile=f"b/{pfad}" if neu is not None else "/dev/null", lineterm="")
    return "".join(f"{zeile}\n" for zeile in zeilen)
