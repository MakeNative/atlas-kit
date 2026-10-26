from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from befunde import Befunde
from textdatei import lies_zeilen
from vokabular import AUSFUEHRENDE, MENSCH, ROLLEN, SCHRITT_ID, WERKZEUG_PFAD

SCHEMA = "atlas-ableitung/0.1"
ABGELEITET = "abgeleitet"
TITEL_HOECHSTENS = 40
WOERTER_MINDESTENS = 3

ZU_KURZ = "das Zitat ist zu kurz für einen Beleg"
ID_UNGUELTIG = "id fehlt oder ist ungültig"
TITEL_UNGUELTIG = "titel fehlt oder ist länger als 40 Zeichen"
KEINE_ERLAUBTE_ROLLE = f"keine erlaubte Rolle ({' | '.join(ROLLEN)})"
ART_IM_TEXT = {"agent": "Agent", "werkzeug": "Werkzeug", "skill": "Skill"}


class AbleitungFehler(Exception):
    pass


def aufbau_fehler(daten: object) -> str | None:
    if not isinstance(daten, dict):
        return "oberste Ebene ist kein Objekt"
    if daten.get("schema") != SCHEMA:
        return f"schema ist nicht {SCHEMA}"
    kit = daten.get("kit")
    if not isinstance(kit, dict) or not isinstance(kit.get("quelle"), str):
        return "kit.quelle fehlt"
    for feld in ("lauf", "skills", "agenten"):
        if not isinstance(daten.get(feld), dict):
            return f"{feld} fehlt oder ist kein Objekt"
    return None


def lies(pfad: Path) -> dict:
    try:
        text = pfad.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise AbleitungFehler(f"Die Ableitung {pfad} ist nicht als UTF-8 lesbar")
    except OSError as e:
        raise AbleitungFehler(f"Die Ableitung {pfad} lässt sich nicht lesen ({e.strerror})")
    try:
        daten = json.loads(text)
    except json.JSONDecodeError as e:
        raise AbleitungFehler(f"Die Ableitung {pfad} ist kein gültiges JSON (Zeile {e.lineno}: {e.msg})")
    grund = aufbau_fehler(daten)
    if grund is not None:
        raise AbleitungFehler(f"Die Ableitung {pfad} ist unbrauchbar: {grund}")
    return daten


def offene(skill_paare, agent_knoten: list[dict]) -> dict:
    return {
        "offen": {
            "skills": [{"name": s["name"], "datei": s["datei"]} for s, _ in skill_paare if not s["_schritte_eigen"]],
            "agenten": [{"name": a["name"], "datei": a["datei"]} for a in agent_knoten if a["_rolle_offen"]],
        },
        "eigene_schritte": [s["name"] for s, _ in skill_paare if s["_schritte_eigen"]],
    }


def ist_zahl(wert: object) -> bool:
    """Eine ganze Zahl, kein Wahrheitswert: so gelten Zeilen und Grenzen der Ableitung (auch in der Werkstatt)."""
    return isinstance(wert, int) and not isinstance(wert, bool)


def belegzeile(beleg: object) -> int | None:
    """Die Zeile eines Belegs, wenn sie eine ganze Zahl ist (nach ist_zahl), sonst None."""
    zeile = beleg.get("zeile") if isinstance(beleg, dict) else None
    return zeile if ist_zahl(zeile) else None


def name_belegt(zeilen: list[str], beleg: object, name: str) -> bool:
    """Der Beleg einer Aussage über einen Ausführenden: gültig nach beleg_grund und mit dem Namen im Zitat."""
    return beleg_grund(zeilen, beleg) is None and name in beleg["zitat"]


def grenze_belegt(grenze: object, zitat: str) -> bool:
    """Die Grenze einer Schleife (höchstens n Runden) steht als Zahl im Zitat ihres Belegs."""
    return ist_zahl(grenze) and grenze >= 1 and re.search(rf"(?<!\d){grenze}(?!\d)", zitat) is not None


def abgeleitete_schritte(eintrag: object) -> list | None:
    """Die Schritte eines Eintrags der Ableitung, wie der Scanner sie liest; None, wenn der Eintrag keine Liste von
    Schritten hat. Sie gelten nur für eine Skill ohne eigene atlas.schritte (vokabular.schritte_eigen)."""
    schritte = eintrag.get("schritte") if isinstance(eintrag, dict) else None
    return schritte if isinstance(schritte, list) else None


def rolle_offen(atlas: object) -> bool:
    """Gilt für einen Agenten mit diesem atlas-Feld eine abgeleitete Rolle? Nur ohne eigene atlas.rolle."""
    return not isinstance(atlas, dict) or atlas.get("rolle") is None


def _zeile_text(zeile: object) -> str:
    return str(zeile) if ist_zahl(zeile) else json.dumps(zeile, ensure_ascii=False)


def beleg_grund(zeilen: list[str], beleg: object) -> str | None:
    zeile = beleg.get("zeile") if isinstance(beleg, dict) else None
    if not ist_zahl(zeile) or not 1 <= zeile <= len(zeilen):
        return f"Zeile {_zeile_text(zeile)} gibt es nicht"
    zitat = beleg.get("zitat")
    nicht_drin = f"das Zitat steht nicht in Zeile {zeile}"
    if not isinstance(zitat, str) or "\n" in zitat or "\r" in zitat:
        return nicht_drin
    if not zitat.strip():
        return ZU_KURZ
    text = zeilen[zeile - 1]
    if zitat not in text:
        return nicht_drin
    if len(zitat.split()) < WOERTER_MINDESTENS and zitat != text.strip():
        return ZU_KURZ
    return None


def _zeile_in_datei(zeilen: list[str], beleg: object) -> int | None:
    zeile = belegzeile(beleg)
    return zeile if zeile is not None and 1 <= zeile <= len(zeilen) else None


@dataclass
class _Schritt:
    pos: int
    roh: dict
    id: str
    titel: str
    beleg: dict
    luecke_davor: int = 0

    @property
    def belege(self) -> dict:
        belege = self.roh.get("belege")
        return belege if isinstance(belege, dict) else {}


@dataclass
class Ergebnis:
    knoten: dict[str, list[dict]] = field(default_factory=dict)
    kanten: dict[str, list[dict]] = field(default_factory=dict)
    skills: dict[str, dict] = field(default_factory=dict)
    rollen_abgeleitet: int = 0
    rollen_entfallen: int = 0

    def bericht(self) -> dict:
        return {"skills": self.skills,
                "rollen": {"abgeleitet": self.rollen_abgeleitet, "entfallen": self.rollen_entfallen}}


class _SkillPruefung:
    def __init__(self, skill: dict, zeilen: list[str], knoten_ids: set[str], befunde: Befunde):
        self.skill = skill
        self.name = skill["name"]
        self.datei = skill["datei"]
        self.zeilen = zeilen
        self.knoten_ids = knoten_ids
        self.befunde = befunde

    def melde(self, art: str, text: str, beleg: object, schritt: object, *, aussage: str,
              ziel_art: str | None = None, ziel: object = None) -> None:
        """Ein Befund mit seinem Ort: Skill, Schritt-id (wenn sie ein Text ist), Aussage und Ziel."""
        self.befunde.melde(art, text, self.datei, _zeile_in_datei(self.zeilen, beleg), herkunft=ABGELEITET,
                           skill=self.name, schritt=schritt if isinstance(schritt, str) else None, aussage=aussage,
                           ziel_art=ziel_art, ziel=None if ziel is None else str(ziel))

    def schritt_id(self, schritt_id: str) -> str:
        return f"schritt:{self.name}/{schritt_id}"

    def voller_beleg(self, beleg: dict) -> dict:
        return {"datei": self.datei, "zeile": beleg["zeile"], "zitat": beleg["zitat"]}

    def kante(self, von: str, nach: str, typ: str, beleg: dict, **extra) -> dict:
        kante = {"von": von, "nach": nach, "typ": typ, "herkunft": ABGELEITET, "beleg": self.voller_beleg(beleg)}
        kante.update(extra)
        return kante

    def _form_grund(self, roh: dict, gesehen: set[str]) -> str | None:
        schritt_id = roh.get("id")
        if not isinstance(schritt_id, str) or not SCHRITT_ID.fullmatch(schritt_id):
            return ID_UNGUELTIG
        if schritt_id in gesehen:
            return f"id '{schritt_id}' kommt doppelt vor"
        titel = roh.get("titel")
        if (not isinstance(titel, str) or "\n" in titel or "\r" in titel
                or not 1 <= len(titel.strip()) <= TITEL_HOECHSTENS):
            return TITEL_UNGUELTIG
        return beleg_grund(self.zeilen, roh.get("beleg"))

    def _titel_im_text(self, roh: dict, pos: int) -> str:
        titel = roh.get("titel")
        if isinstance(titel, str) and "\n" not in titel and "\r" not in titel \
                and 1 <= len(titel.strip()) <= TITEL_HOECHSTENS:
            return f"'{titel.strip()}'"
        return f"Nr. {pos + 1}"

    def form(self, schritte: list) -> tuple[list[_Schritt], int]:
        behalten: list[_Schritt] = []
        gesehen: set[str] = set()
        entfallen = 0
        letzte_pos = None
        for pos, roh in enumerate(schritte):
            roh = roh if isinstance(roh, dict) else {}
            grund = self._form_grund(roh, gesehen)
            if isinstance(roh.get("id"), str) and SCHRITT_ID.fullmatch(roh["id"]):
                gesehen.add(roh["id"])
            if grund is not None:
                art = "ohne_beleg"
                if grund in (ID_UNGUELTIG, TITEL_UNGUELTIG):
                    art = "pflichtfeld_fehlt"
                elif grund.startswith("id '"):
                    art = "widerspruch"
                self.melde(art, f"Abgeleiteter Schritt {self._titel_im_text(roh, pos)} entfällt: {grund}.",
                           roh.get("beleg"), roh.get("id"), aussage="schritt")
                entfallen += 1
                continue
            schritt = _Schritt(pos, roh, roh["id"], roh["titel"].strip(), roh["beleg"])
            if letzte_pos is not None and pos - letzte_pos > 1:
                schritt.luecke_davor = pos - letzte_pos - 1
            letzte_pos = pos
            behalten.append(schritt)
        return behalten, entfallen

    def _aussage_zeile_beleg(self, s: _Schritt, eintrag: object) -> object:
        return eintrag if isinstance(eintrag, dict) else s.beleg

    def _beleg_mit_name(self, eintrag: object, name: str) -> bool:
        return name_belegt(self.zeilen, eintrag, name)

    def _startziel(self, s: _Schritt, art: str, wert: str, ziel: str | None, name_im_zitat: str,
                   eintrag: object, aussage: str | None = None) -> dict | None:
        ort = self._aussage_zeile_beleg(s, eintrag)
        aussage = aussage or art
        if ziel is None or ziel not in self.knoten_ids:
            self.melde("verweis_fehlt", f"Abgeleiteter Schritt '{s.titel}': {ART_IM_TEXT[art]} {wert} gibt es nicht.",
                       ort, s.id, aussage=aussage, ziel_art=art, ziel=wert)
            return None
        if not self._beleg_mit_name(eintrag, name_im_zitat):
            self.melde("ohne_beleg",
                       f"Abgeleiteter Schritt '{s.titel}': Dass er {ART_IM_TEXT[art]} {wert} startet, ist nicht belegt.",
                       ort, s.id, aussage=aussage, ziel_art=art, ziel=wert)
            return None
        return self.kante(self.schritt_id(s.id), ziel, "startet", eintrag)

    def _startet(self, s: _Schritt) -> list[dict]:
        kanten = []
        agent = s.roh.get("agent")
        if isinstance(agent, str):
            kante = self._startziel(s, "agent", agent, f"agent:{agent}", agent, s.belege.get("agent"))
            kanten += [kante] if kante else []
        werkzeug = s.roh.get("werkzeug")
        if isinstance(werkzeug, str):
            m = WERKZEUG_PFAD.match(posixpath.normpath(werkzeug))
            ziel = f"werkzeug:{m.group(1)}" if m else None
            kante = self._startziel(s, "werkzeug", werkzeug, ziel, m.group(1) if m else werkzeug,
                                    s.belege.get("werkzeug"))
            kanten += [kante] if kante else []
        skill = s.roh.get("skill")
        if isinstance(skill, str):
            eintrag = s.belege.get("skill")
            if skill == self.name:
                self.melde("widerspruch", f"Abgeleiteter Schritt '{s.titel}' kann sich nicht selbst starten.",
                           self._aussage_zeile_beleg(s, eintrag), s.id, aussage="skill", ziel_art="skill", ziel=skill)
            else:
                kante = self._startziel(s, "skill", skill, f"skill:{skill}", skill, eintrag)
                kanten += [kante] if kante else []
        return kanten

    def _parallel(self, s: _Schritt) -> list[dict]:
        namen = s.roh.get("parallel")
        if not isinstance(namen, list):
            return []
        belege = s.belege.get("parallel")
        belege = belege if isinstance(belege, dict) else {}
        kanten = []
        for name in dict.fromkeys(n for n in namen if isinstance(n, str)):
            eintrag = belege.get(name)
            kante = self._startziel(s, "agent", name, f"agent:{name}", name, eintrag, "parallel")
            if kante:
                kanten.append({**kante, "typ": "parallel"})
        return kanten

    def _mehrere_startziele(self, s: _Schritt) -> None:
        genannt = [f for f in AUSFUEHRENDE if s.roh.get(f) is not None]
        if len(genannt) > 1:
            self.melde("widerspruch", f"Abgeleiteter Schritt '{s.titel}' nennt mehr als ein Startziel.", s.beleg,
                       s.id, aussage="startziele")

    def mensch(self, s: _Schritt) -> str | None:
        wert = s.roh.get("mensch")
        if wert is None:
            return None
        eintrag = s.belege.get("mensch")
        ort = self._aussage_zeile_beleg(s, eintrag)
        if wert not in MENSCH:
            self.melde("widerspruch",
                       f"Abgeleiteter Schritt '{s.titel}': mensch '{wert}' ist kein erlaubter Wert; "
                       f"die Mensch-Marke entfällt.", ort, s.id, aussage="mensch")
            return None
        if beleg_grund(self.zeilen, eintrag) is not None:
            self.melde("ohne_beleg", f"Abgeleiteter Schritt '{s.titel}': Die Mensch-Marke ist nicht belegt und entfällt.",
                       ort, s.id, aussage="mensch")
            return None
        return wert

    def _zurueck(self, s: _Schritt, behalten: list[_Schritt]) -> list[dict]:
        bei_befund = s.roh.get("bei_befund")
        if not isinstance(bei_befund, dict) or bei_befund.get("zurueck") is None:
            return []
        ziel = bei_befund["zurueck"]
        eintrag = s.belege.get("zurueck")
        ort = self._aussage_zeile_beleg(s, eintrag)
        schleife = f"Abgeleiteter Schritt '{s.titel}': Die Schleife zurück zu '{ziel}'"
        ziel_schritt = next((t for t in behalten if t.id == ziel), None)
        if ziel == s.id or (ziel_schritt is not None and ziel_schritt.pos >= s.pos):
            self.melde("widerspruch", f"{schleife} führt nicht zurück.", ort, s.id, aussage="zurueck",
                       ziel_art="schritt", ziel=ziel)
            return []
        if ziel_schritt is None:
            self.melde("verweis_fehlt", f"{schleife} hat kein Ziel.", ort, s.id, aussage="zurueck", ziel_art="schritt",
                       ziel=ziel)
            return []
        if beleg_grund(self.zeilen, eintrag) is not None:
            self.melde("ohne_beleg", f"{schleife} ist nicht belegt.", ort, s.id, aussage="zurueck",
                       ziel_art="schritt", ziel=ziel)
            return []
        extra = {}
        grenze = bei_befund.get("max")
        if grenze is not None:
            if grenze_belegt(grenze, eintrag["zitat"]):
                extra["max"] = grenze
            else:
                self.melde("ohne_beleg", f"Abgeleiteter Schritt '{s.titel}': 'höchstens {grenze}' steht nicht "
                                         f"im Zitat; die Grenze entfällt.", ort, s.id, aussage="max",
                           ziel_art="schritt", ziel=ziel)
        return [self.kante(self.schritt_id(s.id), self.schritt_id(ziel), "zurueck", eintrag, **extra)]

    def uebernimm(self, schritte: list) -> tuple[list[dict], list[dict], int]:
        behalten, entfallen = self.form(schritte)
        knoten = []
        kanten = []
        for nr, s in enumerate(behalten, start=1):
            k = {
                "id": self.schritt_id(s.id),
                "typ": "schritt",
                "skill": self.skill["id"],
                "nr": nr,
                "titel": s.titel,
                "mensch": self.mensch(s),
                "beleg": self.voller_beleg(s.beleg),
                "herkunft": ABGELEITET,
            }
            if s.luecke_davor:
                k["luecke_davor"] = s.luecke_davor
            knoten.append(k)
        for i, s in enumerate(behalten):
            if i + 1 < len(behalten) and not behalten[i + 1].luecke_davor:
                folgender = behalten[i + 1]
                kanten.append(self.kante(self.schritt_id(s.id), self.schritt_id(folgender.id), "folgt",
                                         folgender.beleg))
            self._mehrere_startziele(s)
            kanten += self._startet(s)
            kanten += self._parallel(s)
            kanten += self._zurueck(s, behalten)
        return knoten, kanten, entfallen


def _rolle(agent: dict, eintrag: object, wurzel: Path, befunde: Befunde, ergebnis: Ergebnis) -> None:
    if not isinstance(eintrag, dict) or eintrag.get("rolle") is None:
        return
    rolle = eintrag["rolle"]
    beleg = eintrag.get("beleg")
    try:
        zeilen = lies_zeilen(wurzel, agent["datei"])
    except (OSError, UnicodeDecodeError):
        zeilen = []
    zeile = _zeile_in_datei(zeilen, beleg)
    if rolle not in ROLLEN:
        befunde.melde("widerspruch", f"Abgeleitete Rolle '{rolle}' für {agent['name']} entfällt: {KEINE_ERLAUBTE_ROLLE}.",
                      agent["datei"], zeile, herkunft=ABGELEITET, aussage="rolle", ziel_art="agent",
                      ziel=agent["name"])
        return
    grund = beleg_grund(zeilen, beleg)
    if grund is not None:
        befunde.melde("ohne_beleg", f"Abgeleitete Rolle '{rolle}' für {agent['name']} entfällt: {grund}.",
                      agent["datei"], zeile, herkunft=ABGELEITET, aussage="rolle", ziel_art="agent",
                      ziel=agent["name"])
        ergebnis.rollen_entfallen += 1
        return
    agent["rolle"] = rolle
    agent["rolle_herkunft"] = ABGELEITET
    agent["rolle_beleg"] = {"datei": agent["datei"], "zeile": beleg["zeile"], "zitat": beleg["zitat"]}
    ergebnis.rollen_abgeleitet += 1


def uebernimm(ableitung: dict, skill_paare, agent_knoten: list[dict], knoten_ids: set[str],
              wurzel: Path, befunde: Befunde) -> Ergebnis:
    ergebnis = Ergebnis()
    eintraege = ableitung["skills"]
    for skill, fm in skill_paare:
        schritte = abgeleitete_schritte(eintraege.get(skill["name"]))
        if skill["_schritte_eigen"] or schritte is None:
            continue
        pruefung = _SkillPruefung(skill, fm.zeilen, knoten_ids, befunde)
        knoten, kanten, entfallen = pruefung.uebernimm(schritte)
        ergebnis.knoten[skill["name"]] = knoten
        ergebnis.kanten[skill["name"]] = kanten
        ergebnis.skills[skill["name"]] = {"schritte": len(knoten), "entfallen": entfallen}
    rollen = ableitung["agenten"]
    for agent in agent_knoten:
        if agent["_rolle_offen"] and agent["name"] in rollen:
            _rolle(agent, rollen[agent["name"]], wurzel, befunde, ergebnis)
    return ergebnis


def kit_feld(ableitung: dict) -> dict:
    lauf = ableitung["lauf"]
    erzeugt = ableitung.get("erzeugt")
    return {
        "erzeugt": erzeugt if isinstance(erzeugt, str) else None,
        "vollstaendig": lauf.get("vollstaendig") is not False,
        "abgebrochen_bei": lauf.get("abgebrochen_bei") if isinstance(lauf.get("abgebrochen_bei"), str) else None,
    }
