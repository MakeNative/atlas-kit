"""Schreiber der Werkstatt: Einträge eines Änderungssatzes werden zu Datei-Inhalten (specs/werkstatt.yaml WS-W).

Alles geschieht im Speicher auf einem virtuellen Kit: Dateien werden bei Bedarf von der Platte gelesen,
Änderungen bleiben als neue Inhalte liegen, bis die Werkstatt sie schreibt. Jede Operation ändert nur
die Zeilen, die sie betrifft (zeilen.Textdatei). Die nummerierten Prosa-Schritte zählt dieselbe
Funktion wie der Scanner (ablauf.prosa_schritte), damit Atlas-Feld und Text im Gleichschritt bleiben.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from ableitung import abgeleitete_schritte
from schreiber_abgeleitet import AbgeleitetMixin
from schreiber_agenten import AgentenMixin
from schreiber_gleichschritt import GleichschrittMixin
from schreiber_konvention import KonventionMixin
from schreiber_nur_ableitung import NurAbleitungMixin
from virtuelles_kit import SchreibFehler, VirtuellesKit
from vokabular import AUSFUEHRENDE, SKILL_DATEI, schritte_eigen
from zeilen import Textdatei, ZeilenFehler

__all__ = ["Schreiber", "SchreibFehler", "VirtuellesKit"]


def frontmatter_geschuetzt(kurz: str, rel: str) -> str:
    return (f"„{kurz}“ würde das Frontmatter von {rel} ändern. text_ersetzen ändert nur den Text darunter; Name, "
            f"Beschreibung, Werkzeuge, Modell und atlas-Angaben ändern agent_aendern bzw. die Schritt-Operationen.")


def _kopf(datei: Textdatei) -> str:
    """Frontmatter samt Trennzeilen ('' ohne Frontmatter)."""
    return "".join(datei.zeilen[:datei.text_ab()])


@dataclass
class Buchfuehrung:
    """Was der Schreiber über die Einträge eines Satzes festhält; ein gescheiterter Eintrag setzt sie zusammen mit dem
    Kit zurück."""
    geloeschte_agenten: dict[str, str] = field(default_factory=dict)
    entfernt: list[tuple[str, str, str, list[str], set[str]]] = field(default_factory=list)
    weg_belege: list[tuple[str, str, str, str, dict]] = field(default_factory=list)
    # Ausführende, die eine Änderung von Hand weggenommen hat: nennt der Text sie noch, warnt die Vorschau.
    weg_genannt: list[tuple[str, str, str, set[str]]] = field(default_factory=list)
    # Umbenannte Agent-Dateien: alte Datei → neue Datei (auch über mehrere Umbenennungen im selben Satz).
    umbenannt: dict[str, str] = field(default_factory=dict)
    # Agent-Dateien, die beim Umbenennen nicht geändert wurden, obwohl sie den alten Namen nennen: (Datei, Name,
    # Grund).
    nicht_umbenannt: list[tuple[str, str, str]] = field(default_factory=list)
    # Welcher Agent einen Namen trägt, wenn es nicht der Agent des Kits mit diesem Namen ist: Name → ("kit", Name vor
    # dem Satz) nach einer Umbenennung, ("neu", Eintrag) für einen angelegten.
    wer_heisst: dict[str, tuple[str, str]] = field(default_factory=dict)


class Schreiber(AgentenMixin, KonventionMixin, AbgeleitetMixin, NurAbleitungMixin, GleichschrittMixin):
    """Verteilt jeden Eintrag an die Operation; die Skill-Art entscheidet, welches Modul schreibt."""

    def __init__(self, kit: VirtuellesKit):
        self.kit = kit
        self.buch = Buchfuehrung()
        self.eintrag = ""
        # Schlüssel, die der letzte erfolgreiche Eintrag geändert hat (für die Vorschau je Eintrag).
        self.zuletzt_geaendert: list[str] = []
        # Stand von kit.aenderungen() nach dem letzten Eintrag, solange das Kit nicht zurückgesetzt wurde: so wird
        # die Ableitung je Eintrag nur einmal serialisiert.
        self._stand: tuple[int, dict] | None = None

    def wende_an(self, art: str, op: dict, eintrag_id: str) -> None:
        """Scheitert der Eintrag, sind Kit und Buchführung danach wie vorher, und er kommt als SchreibFehler."""
        gemerkt = self._stand
        vorher = gemerkt[1] if gemerkt is not None and gemerkt[0] == self.kit.zurueckgesetzt else self.kit.aenderungen()
        self._stand = None
        self.eintrag = eintrag_id
        schnappschuss, buch = self.kit.schnappschuss(), copy.deepcopy(self.buch)
        try:
            getattr(self, art)(op, eintrag_id)
        except (SchreibFehler, ZeilenFehler) as fehler:
            self.kit.zuruecksetzen(schnappschuss)
            self.buch = buch
            if isinstance(fehler, SchreibFehler):
                raise
            raise SchreibFehler(str(fehler))
        nachher = self.kit.aenderungen()
        self._stand = (self.kit.zurueckgesetzt, nachher)
        self.zuletzt_geaendert = [rel for rel, stand in nachher.items() if vorher.get(rel) != stand]

    # ------------------------------------------------------------ Nachschlagen

    def _agent(self, name: str) -> str:
        rel = self.kit.agenten().get(name)
        if rel is None:
            raise SchreibFehler(f"Agent {name} gibt es nicht.", ("agent", name))
        return rel

    def schritte_der_skill(self, skill: str, rel: str) -> list[dict]:
        """Die Schritte der Skill, wie der Scanner sie liest: eigene atlas.schritte vor der Ableitung
        (vokabular.schritte_eigen, ableitung.abgeleitete_schritte). Eine Datei, die er nicht liest (nicht als UTF-8
        lesbar, Frontmatter kaputt), hat keine."""
        try:
            datei = self.kit.textdatei(rel)
            if self._hat_eigene(datei):
                return self._schritt_werte(datei)
        except (SchreibFehler, ZeilenFehler):
            return []
        eintrag = self.kit.ableitung.get("skills", {}).get(skill) if isinstance(self.kit.ableitung, dict) else None
        return [s for s in abgeleitete_schritte(eintrag) or [] if isinstance(s, dict)]

    def _skill(self, name: str) -> str:
        rel = self.kit.skills().get(name)
        if rel is None:
            raise SchreibFehler(f"Skill {name} gibt es nicht.", ("skill", name))
        return rel

    def _pruefe_ausfuehrende(self, schritt: dict, skill: str) -> None:
        for name in ([schritt["agent"]] if schritt.get("agent") else []) + list(schritt.get("parallel") or []):
            self._agent(name)
        if schritt.get("werkzeug") and schritt["werkzeug"] not in self.kit.werkzeuge():
            raise SchreibFehler(f"Werkzeug {schritt['werkzeug']} gibt es nicht.")
        if schritt.get("skill"):
            if schritt["skill"] == skill:
                raise SchreibFehler(f"Skill {skill} kann sich nicht selbst starten.")
            self._skill(schritt["skill"])

    def _mit_frontmatter(self, rel: str) -> bool:
        """SKILL.md und Agent-Dateien: ihr Frontmatter ändern nur die eigenen Operationen."""
        return rel.endswith(f"/{SKILL_DATEI}") or rel.rsplit("/", 1)[0] == self.kit.agenten_ordner

    def _art(self, skill: str) -> tuple[str, str]:
        rel = self._skill(skill)
        werte = self.kit.textdatei(rel).frontmatter().werte()
        if schritte_eigen(werte.get("atlas")):
            return "konvention", rel
        if self.kit.ableitung_von(skill) is not None:
            return "abgeleitet", rel
        return "ohne", rel


    def schritt_einfuegen(self, op: dict, _eid: str) -> None:
        skill = op["skill"]
        art, rel = self._art(skill)
        self._pruefe_ausfuehrende(op["schritt"], skill)
        if op.get("nur_ableitung"):
            self._nur_einfuegen(skill, op)
        elif art == "konvention":
            self._konvention_einfuegen(skill, rel, op)
        elif art == "abgeleitet":
            self._abgeleitet_einfuegen(skill, rel, op)
        else:
            self._ohne_einfuegen(skill, rel, op)

    def _position(self, skill: str, ids: list[str], nach: str | None, neu: str) -> int:
        if neu in ids:
            raise SchreibFehler(f"Schritt {neu} gibt es in {skill} schon.")
        if nach is None:
            return 0
        if nach not in ids:
            raise SchreibFehler(f"Schritt {nach} gibt es in {skill} nicht.", ("schritt", skill, nach))
        return ids.index(nach) + 1


    def schritt_aendern(self, op: dict, _eid: str) -> None:
        skill = op["skill"]
        art, rel = self._art(skill)
        if art == "ohne":
            raise SchreibFehler(f"Die Skill {skill} hat keinen Ablauf.")
        aenderung = op.get("schritt") or {}
        neu = {f: aenderung[f] for f in AUSFUEHRENDE if aenderung.get(f) is not None}
        self._pruefe_ausfuehrende(neu, skill)
        if op.get("nur_ableitung"):
            self._nur_aendern(skill, op["id"], aenderung, op.get("belege") or {})
        elif art == "konvention":
            self._konvention_aendern(skill, rel, op["id"], aenderung, op.get("anweisung"))
        else:
            self._abgeleitet_aendern(skill, rel, op["id"], aenderung, op.get("anweisung"))


    def schritt_entfernen(self, op: dict, _eid: str) -> None:
        skill = op["skill"]
        art, rel = self._art(skill)
        if art == "ohne":
            raise SchreibFehler(f"Die Skill {skill} hat keinen Ablauf.")
        if op.get("nur_ableitung"):
            self._nur_entfernen(skill, op["id"])
        elif art == "konvention":
            self._konvention_entfernen(skill, rel, op["id"])
        else:
            self._abgeleitet_entfernen(skill, rel, op["id"])


    def schleife_setzen(self, op: dict, _eid: str) -> None:
        skill = op["skill"]
        art, rel = self._art(skill)
        if art == "ohne":
            raise SchreibFehler(f"Die Skill {skill} hat keinen Ablauf.")
        if op.get("nur_ableitung"):
            self._nur_schleife(skill, op)
        elif art == "konvention":
            self._konvention_schleife(skill, rel, op)
        else:
            self._abgeleitet_schleife(skill, rel, op)

    # ------------------------------------------------------------ Text ersetzen (WS-W11)

    def text_ersetzen(self, op: dict, _eid: str) -> None:
        rel = op["datei"]
        projekt = self.kit.art == "projekt"
        if rel.startswith(".claude/") != projekt:
            raise SchreibFehler(f"{rel} gibt es nicht.")
        self.kit.pruefe_ziel(rel)  # vor dem Lesen: Inhalte hinter Symlinks kommen nie in eine Meldung
        inhalt = self.kit.lies(rel)
        if inhalt is None:
            raise SchreibFehler(f"{rel} gibt es nicht.")
        try:
            text = inhalt.decode("utf-8")
        except UnicodeDecodeError:
            raise SchreibFehler(f"{rel} ist nicht als UTF-8 lesbar.")
        datei = Textdatei(text)
        ende = datei.ende
        alt, neu = op["alt"], op["neu"]
        if ende == "\r\n":
            alt = alt.replace("\r\n", "\n").replace("\n", "\r\n")
            neu = neu.replace("\r\n", "\n").replace("\n", "\r\n")
        anzahl = text.count(alt)
        kurz = op["alt"].splitlines()[0][:60] if op["alt"].strip() else op["alt"][:60]
        if anzahl == 0:
            raise SchreibFehler(f"„{kurz}“ kommt in {rel} nicht vor.")
        if anzahl > 1:
            raise SchreibFehler(f"„{kurz}“ kommt in {rel} {anzahl}-mal vor; er muss genau einmal vorkommen.")
        stelle = text.index(alt)
        von_zeile = text.count("\n", 0, stelle) + 1
        nach_zeile = text.count("\n", 0, stelle + len(alt)) + 1
        versatz = neu.count("\n") - alt.count("\n")
        neuer_text = text[:stelle] + neu + text[stelle + len(alt):]
        if self._mit_frontmatter(rel) and _kopf(Textdatei(neuer_text)) != _kopf(datei):
            raise SchreibFehler(frontmatter_geschuetzt(kurz, rel))
        self.kit.schreibe(rel, neuer_text.encode("utf-8"))
        self._belege_nachziehen(rel, Textdatei(text).texte(), Textdatei(neuer_text).texte(), von_zeile, nach_zeile,
                                versatz)

