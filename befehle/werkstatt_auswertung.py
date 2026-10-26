from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import werkstatt_gedaechtnis as gd
import werkstatt_gutachten as gutachten_form
import werkstatt_pfade as pfade
from werkstatt_kennzahlen import LaufKopie

SCHEMA = "atlas-auswertung/0.1"
GROESSE_HOECHSTENS = 128 * 1024
LUECKEN_HOECHSTENS = 10
LUECKE_HOECHSTENS = 300
BEWERTUNGEN_HOECHSTENS = 30
VORSCHLAEGE_HOECHSTENS = 10
GEDAECHTNIS_HOECHSTENS = 30
BELEGE_HOECHSTENS = 10
SKIZZE_HOECHSTENS = 20
URTEIL_HOECHSTENS = 400
TITEL_HOECHSTENS = 80
BEGRUENDUNG_HOECHSTENS = 400
ERWARTUNG_HOECHSTENS = 300
ZITAT_MINDESTENS = 5
ZITAT_HOECHSTENS = 300
ANZEIGE_HOECHSTENS = 300
ZEILEN_DANEBEN = 3
EINORDNUNGEN = ("gut", "auffaellig", "nicht_bewertbar")
ARTEN = ("prompt_schaerfen", "regel_aendern", "schleife", "mensch_freigabe", "rechte_einschraenken",
         "schritt_zusammenlegen", "agent_ergaenzen", "sonstiges")
AKTIONEN = ("zeile_entfernen", "datei_entfernen")
GRUENDE = ("falsch", "veraltet", "doppelt")
FEHLT_ARTEN = ("agent", "skill", "werkzeug", "datei")
TEXTFELDER = ("antwort", "beschreibung", "fehler", "text", "kurz", "name", "subagent_type", "werkzeug", "grund")
OBEN = ("schema", "datenlage", "bewertungen", "vorschlaege", "gedaechtnis")
BEWERTUNG = ("ziel", "einordnung", "urteil", "belege")
VORSCHLAG = ("id", "art", "titel", "begruendung", "erwartung", "belege", "skizze")
GEDAECHTNIS = ("id", "gedaechtnis", "datei", "aktion", "zeile", "zitat", "grund", "begruendung", "belege")
GEDAECHTNIS_PFLICHT = ("id", "gedaechtnis", "datei", "aktion", "grund", "begruendung", "belege")
VORSCHLAG_ID = re.compile(r"^v[1-9][0-9]?$")
EINTRAG_ID = re.compile(r"^m[1-9][0-9]?$")
NAME = r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}"
ZIEL = re.compile(rf"^(?:(?:agent|werkzeug|skill):{NAME}|schritt:{NAME}/{NAME})$")

BELEG_FALSCH = "Der Beleg steht so nicht im Lauf."
ZAHL_FALSCH = "Die Zahl stimmt nicht mit den Läufen überein."
LAUF_FEHLT = "Diesen Lauf hat die Werkstatt nicht ausgewertet."
ZEILE_FEHLT = "Diese Zeile gibt es im Lauf nicht."
ZEILE_UNGUELTIG = "Die Zeile im Lauf ist kein gültiges Ereignis."
ZITAT_FORM = "Ein Zitat ist eine Zeile mit 5 bis 300 Zeichen."
KENNZAHL_FEHLT = "Diese Kennzahl gibt es nicht."
FEHLT_DOCH = "Was als fehlend belegt ist, gibt es im Kit."
FEHLT_FORM = "Ein Fehlt-Beleg nennt agent, skill, werkzeug oder datei und einen Namen."
BELEG_FORM = "Ein Beleg hat nicht die Form eines Lauf-, Kennzahl-, Kit-, Fehlt- oder Gedächtnis-Belegs."
KIT_BELEG = "Der Beleg steht so nicht im Kit."
GEDAECHTNIS_FEHLT = "Dieses Memory hat die Werkstatt nicht kopiert."
GEDAECHTNIS_BELEG = "Der Beleg steht so nicht im Memory."
ZIEL_FEHLT = "Das Ziel gibt es weder im Kit noch in einem belegten Lauf."
PFLICHT_LAUF = "Es fehlt ein Beleg aus einem Lauf oder eine Kennzahl."
PFLICHT_VERALTET = "Ein veralteter Eintrag braucht einen Beleg aus dem Kit oder ein belegtes Fehlen."
PFLICHT_DOPPELT = "Ein doppelter Eintrag braucht einen Beleg auf einen anderen Eintrag desselben Agenten."
FRONTMATTER = "Die Zeile steht im Frontmatter der Memory-Datei."
LEERE_ZEILE = "Die Zeile ist leer."
MEMORY_MD_GANZ = "MEMORY.md entfernt die Werkstatt nie ganz."
KOPIE_GEAENDERT = "Die Kopie der Memory-Datei wurde seit dem Kopieren geändert."
ID_DOPPELT = "Die Kennung kommt doppelt vor."


def doppelt_mit(eigene: str, andere: str) -> str:
    return f"Memory-Eintrag {eigene} doppelt sich mit {andere}."


def feld_falsch(feld: str) -> str:
    return f"Das Feld {feld} fehlt oder hat nicht die erwartete Form."


class AuswertungFehler(Exception):
    def __init__(self, meldungen: list[str]):
        super().__init__("; ".join(meldungen))
        self.meldungen = meldungen


@dataclass
class Arbeitsdaten:
    skill: str
    laeufe: dict[str, LaufKopie]
    kennzahlen: dict[str, int]
    kopie: Path
    kopie_stand: dict[str, str]
    bauplan: dict
    gedaechtnis: dict[int, str]
    gedaechtnis_dateien: dict[int, list[str]] = field(default_factory=dict)
    gedaechtnis_kopie: dict[int, Path] = field(default_factory=dict)
    gedaechtnis_stand: dict[tuple[int, str], gd.Stand] = field(default_factory=dict)
    weggelassen: int = 0
    home: Path | None = None


def _zeile_ok(wert: object, hoechstens: int) -> bool:
    return (isinstance(wert, str) and bool(wert.strip()) and len(wert) <= hoechstens and "\n" not in wert
            and "\r" not in wert and not any(ord(z) < 32 and z != "\t" for z in wert))


def _leerraum_weg(text: str) -> str:
    return " ".join(text.split())


def _zitat_ok(zitat: object) -> bool:
    return (isinstance(zitat, str) and len(zitat) <= ZITAT_HOECHSTENS and "\n" not in zitat and "\r" not in zitat
            and len(_leerraum_weg(zitat)) >= ZITAT_MINDESTENS)


def _ganzzahl(wert: object) -> bool:
    return isinstance(wert, int) and not isinstance(wert, bool)


def _kurz(text: str) -> str:
    return text if len(text) <= ANZEIGE_HOECHSTENS else text[:ANZEIGE_HOECHSTENS - 1] + "…"


def rahmen(auswertung: object) -> dict:
    if not isinstance(auswertung, dict) or set(auswertung) != set(OBEN):
        raise AuswertungFehler([f"Form: auswertung braucht genau {', '.join(OBEN)}."])
    if auswertung["schema"] != SCHEMA:
        raise AuswertungFehler([f"Form: auswertung.schema ist {SCHEMA}."])
    if len(json.dumps(auswertung, ensure_ascii=False).encode("utf-8")) > GROESSE_HOECHSTENS:
        raise AuswertungFehler(["Form: die Auswertung hat höchstens 128 KiB."])
    datenlage = auswertung["datenlage"]
    if not isinstance(datenlage, dict) or set(datenlage) != {"luecken"}:
        raise AuswertungFehler(["Form: datenlage hat genau luecken."])
    luecken = datenlage["luecken"]
    if not isinstance(luecken, list) or len(luecken) > LUECKEN_HOECHSTENS \
            or not all(_zeile_ok(z, LUECKE_HOECHSTENS) for z in luecken):
        raise AuswertungFehler([f"Form: datenlage.luecken hat höchstens {LUECKEN_HOECHSTENS} Zeilen mit 1 bis "
                                f"{LUECKE_HOECHSTENS} Zeichen."])
    for name, hoechstens, felder in (("bewertungen", BEWERTUNGEN_HOECHSTENS, BEWERTUNG),
                                     ("vorschlaege", VORSCHLAEGE_HOECHSTENS, VORSCHLAG),
                                     ("gedaechtnis", GEDAECHTNIS_HOECHSTENS, GEDAECHTNIS)):
        liste = auswertung[name]
        if not isinstance(liste, list) or len(liste) > hoechstens:
            raise AuswertungFehler([f"Form: {name} ist eine Liste mit höchstens {hoechstens} Einträgen."])
        for i, punkt in enumerate(liste, 1):
            if not isinstance(punkt, dict):
                raise AuswertungFehler([f"Form: {name}[{i}] ist kein Objekt."])
            fremd = sorted(set(punkt) - set(felder))
            if fremd:
                raise AuswertungFehler([f"Form: {name}[{i}] hat das unbekannte Feld {fremd[0][:40]}."])
    return auswertung


def _gedaechtnis_zeile(stand: gd.Stand, zeile: object, zitat: object) -> tuple[str | None, int | None]:
    if not _ganzzahl(zeile) or zeile < 1 or not _zitat_ok(zitat):
        return ZITAT_FORM, None
    gesucht = _leerraum_weg(zitat)
    for abstand in range(ZEILEN_DANEBEN + 1):
        for nummer in sorted({zeile - abstand, zeile + abstand}):
            if 1 <= nummer <= len(stand.zeilen) and gesucht in _leerraum_weg(stand.zeilen[nummer - 1]):
                return None, nummer
    return GEDAECHTNIS_BELEG, None


def _knoten(bauplan: dict) -> set[str]:
    return {k.get("id") for k in bauplan.get("knoten", []) if isinstance(k, dict) and isinstance(k.get("id"), str)}


class _Pruefung:
    def __init__(self, daten: Arbeitsdaten, register: gd.Register):
        self.daten = daten
        self.register = register

    def _ziel(self, nr: object) -> tuple[gd.Ziel | None, str | None]:
        if not _ganzzahl(nr) or nr not in self.daten.gedaechtnis:
            return None, None
        gid = self.daten.gedaechtnis[nr]
        try:
            return self.register.hole(gid), gid
        except gd.GedaechtnisFehler:
            return None, None

    def beleg(self, beleg: object) -> tuple[str | None, dict]:
        if not isinstance(beleg, dict):
            return BELEG_FORM, {}
        schluessel = set(beleg)
        if "lauf" in beleg and schluessel in ({"lauf", "zeile"}, {"lauf", "zeile", "zitat"}):
            return self._lauf(beleg)
        if schluessel == {"kennzahl", "wert"}:
            return self._kennzahl(beleg)
        if schluessel == {"fehlt"}:
            return self._fehlt(beleg)
        if schluessel == {"gedaechtnis", "datei", "zeile", "zitat"}:
            return self._gedaechtnis(beleg)
        if schluessel == {"datei", "zeile", "zitat"}:
            return self._kit(beleg)
        return BELEG_FORM, {}

    def _lauf(self, beleg: dict) -> tuple[str | None, dict]:
        kopie = self.daten.laeufe.get(beleg["lauf"]) if isinstance(beleg["lauf"], str) else None
        if kopie is None:
            return LAUF_FEHLT, {}
        zeile = beleg["zeile"]
        if not _ganzzahl(zeile) or not 1 <= zeile <= len(kopie.zeilen):
            return ZEILE_FEHLT, {}
        ereignis = kopie.gueltig.get(zeile)
        if ereignis is None:
            return ZEILE_UNGUELTIG, {}
        felder = [(n, ereignis["daten"].get(n)) for n in TEXTFELDER if isinstance(ereignis["daten"].get(n), str)]
        if "zitat" in beleg:
            if not _zitat_ok(beleg["zitat"]):
                return ZITAT_FORM, {}
            gesucht = _leerraum_weg(beleg["zitat"])
            treffer = [wert for _, wert in felder if gesucht in _leerraum_weg(wert)]
            if not treffer:
                return BELEG_FALSCH, {}
            text = treffer[0]
        else:
            text = next((wert for _, wert in felder if wert.strip()), "")
        anzeige = {"lauf_zeit": kopie.lauf.get("gestartet"), "zeit": ereignis["zeit"], "art": ereignis["art"],
                   "text": _kurz(text)}
        return None, {**beleg, "anzeige": anzeige}

    def _kennzahl(self, beleg: dict) -> tuple[str | None, dict]:
        name, wert = beleg["kennzahl"], beleg["wert"]
        if not isinstance(name, str) or name not in self.daten.kennzahlen:
            return KENNZAHL_FEHLT, {}
        if not _ganzzahl(wert):
            return feld_falsch("wert"), {}
        if self.daten.kennzahlen[name] != wert:
            return ZAHL_FALSCH, {}
        return None, {**beleg, "anzeige": {"name": name, "wert": wert}}

    def _kit(self, beleg: dict) -> tuple[str | None, dict]:
        meldungen, geprueft = gutachten_form.pruefe_beleg(beleg, self.daten.kopie, self.daten.kopie_stand)
        if meldungen:
            return KIT_BELEG, {}
        return None, {**geprueft, "anzeige": dict(geprueft)}

    def _fehlt(self, beleg: dict) -> tuple[str | None, dict]:
        fehlt = beleg["fehlt"]
        if not isinstance(fehlt, dict) or set(fehlt) != {"art", "name"} or fehlt["art"] not in FEHLT_ARTEN \
                or not isinstance(fehlt["name"], str) or not fehlt["name"]:
            return FEHLT_FORM, {}
        art, name = fehlt["art"], fehlt["name"]
        if art == "datei":
            try:
                rel = pfade.pruefe_rel(name)
            except pfade.PfadFehler:
                return FEHLT_FORM, {}
            if re.match(r"^[A-Za-z]:", rel) or os.path.lexists(Path(self.daten.kopie) / rel) \
                    or any(k.get("datei") == rel for k in self.daten.bauplan.get("knoten", []) if isinstance(k, dict)):
                return FEHLT_DOCH, {}
        else:
            if not re.fullmatch(NAME, name):
                return FEHLT_FORM, {}
            if f"{art}:{name}" in _knoten(self.daten.bauplan):
                return FEHLT_DOCH, {}
        return None, {**beleg, "anzeige": {"art": art, "name": name}}

    def _gedaechtnis(self, beleg: dict) -> tuple[str | None, dict]:
        ziel, gid = self._ziel(beleg["gedaechtnis"])
        datei = beleg["datei"]
        if ziel is None or not isinstance(datei, str) or datei not in self.daten.gedaechtnis_dateien.get(
                beleg["gedaechtnis"], []):
            return GEDAECHTNIS_FEHLT, {}
        stand = self.daten.gedaechtnis_stand.get((beleg["gedaechtnis"], datei))
        if stand is None:
            return GEDAECHTNIS_FEHLT, {}
        meldung, zeile = _gedaechtnis_zeile(stand, beleg["zeile"], beleg["zitat"])
        if meldung is not None:
            return meldung, {}
        anzeige = {"agent": ziel.agent, "bereich": ziel.bereich, "datei": datei, "zeile": zeile,
                   "zitat": beleg["zitat"]}
        return None, {**beleg, "zeile": zeile, "anzeige": anzeige}

    def belege(self, liste: object) -> tuple[str | None, list[dict]]:
        if not isinstance(liste, list) or not 1 <= len(liste) <= BELEGE_HOECHSTENS:
            return feld_falsch("belege"), []
        geprueft = []
        for beleg in liste:
            meldung, ergebnis = self.beleg(beleg)
            if meldung is not None:
                return meldung, []
            geprueft.append(ergebnis)
        return None, geprueft


def _hat(belege: list[dict], *arten: str) -> bool:
    form = {"lauf": "lauf", "kennzahl": "kennzahl", "fehlt": "fehlt", "gedaechtnis": "gedaechtnis"}
    for beleg in belege:
        art = next((form[k] for k in form if k in beleg), "kit")
        if art in arten:
            return True
    return False


def pruefe_punkt(art: str, punkt: object, daten: Arbeitsdaten, register: gd.Register,
                 gesehen: dict[object, str]) -> tuple[list[str], dict]:
    pruefung = _Pruefung(daten, register)
    if not isinstance(punkt, dict):
        return [feld_falsch(art)], {}
    if art == "bewertung":
        return _bewertung(punkt, pruefung, daten)
    if art == "vorschlag":
        return _vorschlag(punkt, pruefung, gesehen)
    return _eintrag(punkt, pruefung, daten, register, gesehen)


def _bewertung(punkt: dict, pruefung: _Pruefung, daten: Arbeitsdaten) -> tuple[list[str], dict]:
    if set(punkt) != set(BEWERTUNG):
        return [feld_falsch(next(iter(sorted(set(BEWERTUNG) - set(punkt))), "ziel"))], {}
    if not isinstance(punkt["ziel"], str) or not ZIEL.fullmatch(punkt["ziel"]):
        return [feld_falsch("ziel")], {}
    if punkt["einordnung"] not in EINORDNUNGEN:
        return [feld_falsch("einordnung")], {}
    if not _zeile_ok(punkt["urteil"], URTEIL_HOECHSTENS):
        return [feld_falsch("urteil")], {}
    meldung, belege = pruefung.belege(punkt["belege"])
    if meldung is not None:
        return [meldung], {}
    if not _hat(belege, "lauf", "kennzahl"):
        return [PFLICHT_LAUF], {}
    bekannt = _knoten(daten.bauplan)
    for beleg in belege:
        if "lauf" in beleg:
            bekannt |= _knoten(daten.laeufe[beleg["lauf"]].bauplan)
    if punkt["ziel"] not in bekannt:
        return [ZIEL_FEHLT], {}
    return [], {**punkt, "belege": belege}


def _vorschlag(punkt: dict, pruefung: _Pruefung, gesehen: dict[object, str]) -> tuple[list[str], dict]:
    if set(punkt) != set(VORSCHLAG):
        return [feld_falsch(next(iter(sorted(set(VORSCHLAG) - set(punkt))), "id"))], {}
    vid = punkt["id"]
    if not isinstance(vid, str) or not VORSCHLAG_ID.fullmatch(vid):
        return [feld_falsch("id")], {}
    if ("vorschlag", vid) in gesehen:
        return [ID_DOPPELT], {}
    gesehen[("vorschlag", vid)] = vid
    if punkt["art"] not in ARTEN:
        return [feld_falsch("art")], {}
    for feld, hoechstens in (("titel", TITEL_HOECHSTENS), ("begruendung", BEGRUENDUNG_HOECHSTENS),
                             ("erwartung", ERWARTUNG_HOECHSTENS)):
        if not _zeile_ok(punkt[feld], hoechstens):
            return [feld_falsch(feld)], {}
    skizze = punkt["skizze"]
    if not isinstance(skizze, list) or not 1 <= len(skizze) <= SKIZZE_HOECHSTENS \
            or not all(isinstance(e, dict) for e in skizze):
        return [feld_falsch("skizze")], {}
    meldung, belege = pruefung.belege(punkt["belege"])
    if meldung is not None:
        return [meldung], {}
    if not _hat(belege, "lauf", "kennzahl"):
        return [PFLICHT_LAUF], {}
    return [], {**punkt, "belege": belege}


def _frontmatter_ende(zeilen: list[str]) -> int:
    if not zeilen or zeilen[0].strip() != "---":
        return 0
    for i in range(1, len(zeilen)):
        if zeilen[i].strip() == "---":
            return i + 1
    return 0


def _eintrag(punkt: dict, pruefung: _Pruefung, daten: Arbeitsdaten, register: gd.Register,
             gesehen: dict[object, str]) -> tuple[list[str], dict]:
    fehlen = [f for f in GEDAECHTNIS_PFLICHT if f not in punkt]
    if fehlen:
        return [feld_falsch(fehlen[0])], {}
    mid = punkt["id"]
    if not isinstance(mid, str) or not EINTRAG_ID.fullmatch(mid):
        return [feld_falsch("id")], {}
    if ("eintrag", mid) in gesehen:
        return [ID_DOPPELT], {}
    gesehen[("eintrag", mid)] = mid
    if punkt["aktion"] not in AKTIONEN:
        return [feld_falsch("aktion")], {}
    if punkt["grund"] not in GRUENDE:
        return [feld_falsch("grund")], {}
    if not _zeile_ok(punkt["begruendung"], BEGRUENDUNG_HOECHSTENS):
        return [feld_falsch("begruendung")], {}
    datei = punkt["datei"]
    if not isinstance(datei, str) or not gd.DATEI.fullmatch(datei):
        return [feld_falsch("datei")], {}
    ziel, gid = pruefung._ziel(punkt["gedaechtnis"])
    nr = punkt["gedaechtnis"]
    if ziel is None or datei not in daten.gedaechtnis_dateien.get(nr, []):
        return [GEDAECHTNIS_FEHLT], {}
    stand = daten.gedaechtnis_stand.get((nr, datei))
    if stand is None:
        return [GEDAECHTNIS_FEHLT], {}
    ergebnis = dict(punkt)
    zeilentext = None
    if punkt["aktion"] == "zeile_entfernen":
        if "zeile" not in punkt or "zitat" not in punkt:
            return [feld_falsch("zeile" if "zeile" not in punkt else "zitat")], {}
        meldung, zeile = _gedaechtnis_zeile(stand, punkt["zeile"], punkt["zitat"])
        if meldung is not None:
            return [meldung], {}
        if zeile <= _frontmatter_ende(stand.zeilen):
            return [FRONTMATTER], {}
        zeilentext = stand.zeilen[zeile - 1]
        if not zeilentext.strip():
            return [LEERE_ZEILE], {}
        ergebnis["zeile"] = zeile
        schluessel = ("zeile", nr, datei, zeile)
        frueher = gesehen.get(schluessel) or gesehen.get(("datei", nr, datei))
    else:
        if datei.casefold() == gd.MEMORY_MD:
            return [MEMORY_MD_GANZ], {}
        try:
            kopie = pfade.lies_regulaer(daten.gedaechtnis_kopie[nr] / datei, gd.DATEI_HOECHSTENS)
        except (pfade.LeseFehler, KeyError):
            return [KOPIE_GEAENDERT], {}
        if pfade.sha(kopie) != stand.sha256:
            return [KOPIE_GEAENDERT], {}
        ergebnis.pop("zeile", None)
        ergebnis.pop("zitat", None)
        schluessel = ("datei", nr, datei)
        frueher = gesehen.get(schluessel) or next(
            (eid for k, eid in gesehen.items() if isinstance(k, tuple) and k[:3] == ("zeile", nr, datei)), None)
    if frueher is not None:
        return [doppelt_mit(mid, frueher)], {}
    meldung, belege = pruefung.belege(punkt["belege"])
    if meldung is not None:
        return [meldung], {}
    if punkt["grund"] == "falsch" and not _hat(belege, "lauf", "kennzahl"):
        return [PFLICHT_LAUF], {}
    if punkt["grund"] == "veraltet" and not _hat(belege, "kit", "fehlt"):
        return [PFLICHT_VERALTET], {}
    if punkt["grund"] == "doppelt":
        anderer = False
        for beleg in belege:
            if "gedaechtnis" not in beleg:
                continue
            anderes_ziel, _ = pruefung._ziel(beleg["gedaechtnis"])
            selbst = beleg["gedaechtnis"] == nr and beleg["datei"] == datei and beleg["zeile"] == ergebnis.get("zeile")
            if anderes_ziel is not None and anderes_ziel.agent == ziel.agent and not selbst:
                anderer = True
        if not anderer:
            return [PFLICHT_DOPPELT], {}
    gesehen[schluessel] = mid
    ergebnis["belege"] = belege
    ergebnis.update(agent=ziel.agent, bereich=ziel.bereich, anzeige=gd.tilde(Path(ziel.ordner), daten.home),
                    zeilentext=zeilentext)
    ergebnis["_intern"] = {"gid": gid, "sha256": stand.sha256}
    return [], ergebnis


def ohne_intern(punkt: dict) -> dict:
    return {k: v for k, v in punkt.items() if k != "_intern"}


def mit_anzeige(geprueft: dict, daten: Arbeitsdaten, register: gd.Register | None = None) -> dict:
    gestartet = sorted(str(k.lauf.get("gestartet")) for k in daten.laeufe.values())
    datenlage = {"laeufe": len(daten.laeufe), "von": gestartet[0] if gestartet else None,
                 "bis": gestartet[-1] if gestartet else None, "weggelassen": daten.weggelassen,
                 "kit_geaendert": sum(1 for k in daten.laeufe.values() if k.kit_geaendert),
                 "luecken": list(geprueft["datenlage"]["luecken"])}
    return {"datenlage": datenlage, "bewertungen": copy.deepcopy(geprueft["bewertungen"]),
            "vorschlaege": copy.deepcopy(geprueft["vorschlaege"]),
            "gedaechtnis": [ohne_intern(p) for p in copy.deepcopy(geprueft["gedaechtnis"])],
            "verworfen": geprueft["verworfen"]}


def pruefe_auswahl(auswertung: object, auswahl: object) -> list[dict]:
    if not isinstance(auswertung, dict):
        raise ValueError("Dieser Auftrag hat keine Auswertung.")
    if (not isinstance(auswahl, list) or not 1 <= len(auswahl) <= VORSCHLAEGE_HOECHSTENS
            or not all(isinstance(a, str) for a in auswahl) or len(set(auswahl)) != len(auswahl)):
        raise ValueError(f"auswahl ist eine Liste mit 1 bis {VORSCHLAEGE_HOECHSTENS} verschiedenen Vorschlags-ids.")
    vorhanden = {v["id"] for v in auswertung["vorschlaege"]}
    fremd = [a for a in auswahl if a not in vorhanden]
    if fremd:
        raise ValueError(f"Den Vorschlag {fremd[0][:20]} gibt es in dieser Auswertung nicht.")
    felder = ("id", "art", "titel", "begruendung", "erwartung", "belege", "skizze")
    return [{k: copy.deepcopy(v[k]) for k in felder} for v in auswertung["vorschlaege"] if v["id"] in auswahl]


def auftragstext(vorschlaege: list[dict], zusatz: str | None) -> str:
    teile = "; ".join(f"{v['id']} „{v['titel']}“" for v in vorschlaege)
    text = f"Baue genau die ausgewählten Vorschläge aus der Auswertung ein: {teile}"
    return f"{text} Dazu: {zusatz}" if zusatz else text
