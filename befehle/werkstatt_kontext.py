"""Panel „Memory und Kontext“ im Server (specs/kontext.yaml KX-O, KX-A, KX-S1, KX-S3).

GET /api/kontext, GET /api/kontext/datei und POST /api/kontext/satz, nur für den Seiten-Schlüssel. Die Freigabeliste
(KontextRegister) entsteht nur aus der Suche; ein Satz trägt nur die Kennung der Datei, sein Ziel merkt sich der
Vorschlag. Inhalte gehen nur in Antworten an die Seite, nie in Protokoll, Ereignisse oder Verlauf.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import aenderungssatz
import texte
import werkstatt_einstellungen
import werkstatt_gedaechtnis as gd
import werkstatt_kennzahlen as kennzahlen
import werkstatt_kontext_schutz as ks
import werkstatt_kontext_suche as suche
import werkstatt_laeufe
from schreiber import VirtuellesKit
from virtuelles_kit import diff

SCHLUESSEL = ks.SCHLUESSEL
SCHEMA = aenderungssatz.SCHEMA
LISTEN_HOECHSTENS = 8
SATZ_FELDER = ("id", "sha256", "text")
BESCHREIBUNG_HOECHSTENS = 300
TITEL_NAME_HOECHSTENS = 100
BEREICH_WORT = {"project": "Projekt", "local": "Projekt, nur auf diesem Rechner", "user": "Alle Projekte"}

ABLAUF_FEHLT = "Diesen Ablauf gibt es im Kit nicht."
WURZEL_KEIN_ORT = "Die Wurzel eines Laufwerks ist kein Arbeitsordner."
TEXT_UNGUELTIG = "Der Text ist ungültig: höchstens 256 KiB, keine NUL-Zeichen."
NICHTS_GEAENDERT = "Am Text hat sich nichts geändert."
GEMISCHT = "Die Datei hatte gemischte Zeilenenden; sie bekommt durchgehend LF."
ALLE_PROJEKTE = "Diese Datei gilt in allen Projekten auf diesem Rechner, nicht nur hier."
LAUF_OFFEN = ("Gerade läuft ein Lauf dieses Kits; er hat seinen Kontext schon geladen, Agenten können ihr Memory "
              "gleichzeitig ändern. Die Werkstatt prüft die Datei unmittelbar vor dem Schreiben.")


def unbekanntes_feld(feld: str) -> str:
    return f"Unbekanntes Feld: {feld}."


def geaendert_seit_oeffnen(anzeige: str) -> str:
    return f"Die Datei {anzeige} hat sich geändert, seit du sie geöffnet hast. Lad sie neu."


def memory_alle(agent: str) -> str:
    return f"{agent} nutzt dieses Memory in allen Projekten."


def ueber_ort(ordner: str) -> str:
    return f"Diese Datei gilt für alle Projekte unter {ordner}, nicht nur für den Arbeitsordner."


def ausserhalb(anzeige: str) -> str:
    return f"Diese Datei liegt außerhalb von Kit und Arbeitsordner ({anzeige}); sie kann auch woanders eingebunden sein."


def ist_kontext_satz(satz: object) -> bool:
    return aenderungssatz.ist_kontext_satz(satz)


def ohne_inhalt(eintraege: list[dict]) -> list[dict]:
    ergebnis = []
    for eintrag in eintraege:
        op = eintrag.get("datei_ersetzen")
        if isinstance(op, dict):
            eintrag = {**eintrag, "datei_ersetzen": {k: v for k, v in op.items() if k != "inhalt"}}
        ergebnis.append(eintrag)
    return ergebnis


def fuer_sitzung(vorschlaege: list[dict]) -> list[dict]:
    return [v for v in vorschlaege if not v.get("kontext")]


def _gekuerzt(text: str, hoechstens: int) -> str:
    return text if len(text) <= hoechstens else "…" + text[-(hoechstens - 1):]


def beschreibung(z: ks.KontextZiel, gemischt: bool) -> str:
    zusatz = f" {GEMISCHT}" if gemischt else ""
    platz = BESCHREIBUNG_HOECHSTENS - len(zusatz)
    if z.memory:
        wo = f"Memory von {z.agent} ({BEREICH_WORT.get(z.bereich or '', z.bereich)}): "
        text = wo + f"{_gekuerzt(z.datei, max(20, platz - len(wo) - 8))} ändern"
    elif z.begleit is not None:
        text = f"Begleitdatei {_gekuerzt(z.begleit, 120)} der Skill {z.skill} ändern"
    else:
        text = f"{_gekuerzt(z.anzeige, platz - 7)} ändern"
    return _gekuerzt(text, platz) + zusatz


def titel(z: ks.KontextZiel) -> str:
    return f"{_gekuerzt(z.datei, TITEL_NAME_HOECHSTENS)} ändern"


def warnungen(z: ks.KontextZiel, lauf_offen: bool) -> list[dict]:
    liste: list[dict] = []
    if z.reichweite == "alle_projekte":
        text = memory_alle(z.agent) if z.memory and z.agent else ALLE_PROJEKTE
        liste.append({"pfad": z.anzeige, "art": "kontext_reichweite", "text": text})
    elif z.reichweite == "ueber_ort":
        liste.append({"pfad": z.anzeige, "art": "kontext_reichweite", "text": ueber_ort(z.anker_anzeige or str(z.anker))})
    elif z.reichweite == "ausserhalb":
        liste.append({"pfad": z.anzeige, "art": "kontext_reichweite", "text": ausserhalb(z.anzeige)})
    if lauf_offen:
        liste.append({"pfad": None, "art": "kontext_lauf", "text": LAUF_OFFEN})
    return liste


@dataclass
class Berechnung:
    eintraege: list = field(default_factory=list)
    dateien: list = field(default_factory=list)
    warnungen: list = field(default_factory=list)
    fehler: list = field(default_factory=list)
    aenderungen: dict[str, tuple[bytes, bytes]] = field(default_factory=dict)
    ziele: dict[str, ks.KontextZiel] = field(default_factory=dict)
    anzeigen: dict[str, str] = field(default_factory=dict)
    kontext_kit: list[str] = field(default_factory=list)
    nur_ausserhalb: bool = True


def kit_relativ(z: ks.KontextZiel, kit: Path) -> str | None:
    if z.reichweite != "kit" or z.memory or not suche.unter(z.pfad, kit):
        return None
    return Path(os.path.relpath(z.pfad, kit)).as_posix()


def berechne(satz: dict, ziel: ks.KontextZiel, verboten: gd.Verboten, kit: Path, lauf_offen: bool) -> Berechnung:
    [eintrag] = satz["eintraege"]
    _, op = aenderungssatz.operation(eintrag)
    if op["datei_id"] != ziel.id:
        raise ks.KontextFehler(ks.UNBEKANNT, 404)
    ks.pruefe(ziel, verboten)
    alt = ks.lies(ziel, verboten)
    if hashlib.sha256(alt).hexdigest() != op["sha256"]:
        raise ks.KontextFehler(ks.seit_vorschau(ziel.anzeige))
    try:
        _, bom, zeilenende = ks.text_von(alt)
    except UnicodeDecodeError:
        raise ks.KontextFehler(ks.liest_nicht(suche.KEIN_TEXT))
    neu = ks.bytes_von(op["inhalt"], bom, zeilenende)
    schluessel = ks.schluessel(ziel.id)
    b = Berechnung()
    b.eintraege = [{"id": eintrag["id"], "beschreibung": eintrag["beschreibung"], "operation": "datei_ersetzen",
                    "gewaehlt": True, "dateien": [ziel.anzeige]}]
    b.dateien = [{"pfad": ziel.anzeige, "art": "geaendert", "diff": diff(ziel.anzeige, alt, neu),
                  "im_kit": ziel.reichweite == "kit",
                  "kontext": {"reichweite": ziel.reichweite, "agent": ziel.agent if ziel.memory else None}}]
    b.warnungen = warnungen(ziel, lauf_offen)
    if neu != alt:
        b.aenderungen[schluessel] = (alt, neu)
    b.ziele[schluessel] = ziel
    b.anzeigen[schluessel] = ziel.anzeige
    rel = kit_relativ(ziel, kit)
    b.kontext_kit = [rel] if rel is not None else []
    b.nur_ausserhalb = rel is None
    return b


class KontextRegister:
    def __init__(self) -> None:
        self._listen: OrderedDict[tuple, dict[str, ks.KontextZiel]] = OrderedDict()
        self._sperre = threading.Lock()

    def ersetze(self, skill: str, ort: Path | None, ziele: list[ks.KontextZiel]) -> None:
        schluessel = (skill, suche.schluessel(ort) if ort is not None else None)
        with self._sperre:
            self._listen.pop(schluessel, None)
            self._listen[schluessel] = {z.id: z for z in ziele}
            while len(self._listen) > LISTEN_HOECHSTENS:
                self._listen.popitem(last=False)

    def hole(self, kid: object) -> ks.KontextZiel:
        if isinstance(kid, str) and ks.KID.fullmatch(kid):
            with self._sperre:
                listen = list(self._listen.values())
            for liste in reversed(listen):
                if kid in liste:
                    return liste[kid]
        raise ks.KontextFehler(ks.UNBEKANNT, 404)


class Kontext:
    def __init__(self, werkstatt, laeufe: werkstatt_laeufe.Laeufe | None, register: KontextRegister | None = None) -> None:
        self.werkstatt = werkstatt
        self.laeufe = laeufe
        self.register = register or KontextRegister()

    @property
    def _verboten(self) -> gd.Verboten:
        return self.werkstatt.verboten

    def _ort(self, roh: object) -> Path | None:
        w = self.werkstatt
        if roh == "":
            roh = None
        if w.art != "plugin":
            if roh is not None:
                raise ks.KontextFehler(werkstatt_laeufe.ORDNER_IM_PROJEKT, 400)
            return Path(w.kit)
        if roh is None:
            return None
        if not isinstance(roh, str) or self.laeufe is None:
            raise ks.KontextFehler(werkstatt_laeufe.ORDNER_NOETIG, 400)
        try:
            ort = self.laeufe.pruefe_ort(roh)
        except werkstatt_laeufe.LaufFehler as fehler:
            raise ks.KontextFehler(str(fehler.daten.get("fehler")), 400)
        ort = Path(os.path.realpath(ort))
        if suche.ist_wurzel(ort):
            raise ks.KontextFehler(WURZEL_KEIN_ORT, 400)
        return ort

    def _orte(self) -> list[Path]:
        w = self.werkstatt
        if w.art != "plugin" or self.laeufe is None:
            return []
        bekannte = [o for o in [self.laeufe.zuletzt_benutzt(), *self.laeufe.arbeitsordner_der_laeufe()] if o is not None]
        return gd.orte(bekannte, self._verboten)

    def _umgebung(self, skill: str) -> suche.Umgebung:
        w = self.werkstatt
        kit = VirtuellesKit(w.ordner, w.kit_id, None, None)
        rel = kit.skills().get(skill)
        skill_ordner = (Path(w.kit) / rel).parent if rel is not None else None
        return suche.Umgebung(home=Path(w.home), claude_ordner=Path(w.konfiguration), kit=Path(w.kit), kit_art=w.art,
                              kit_id=w.kit_id, ergebnis=Path(w.ergebnis), schreibbar=w.schreibbar,
                              skill_ordner=skill_ordner,
                              agent_dateien=frozenset(suche.schluessel(p) for p in w.ordner.agent_dateien()),
                              skill_dateien=frozenset(suche.schluessel(p) for p in w.ordner.skill_dateien()),
                              nur_gelesen=texte.WERKSTATT_NUR_LESEN)

    @staticmethod
    def _skills(bauplan: dict) -> set[str]:
        return {k.get("name") for k in bauplan.get("knoten", [])
                if isinstance(k, dict) and k.get("typ") == "skill" and isinstance(k.get("name"), str)}

    @staticmethod
    def _rollen(bauplan: dict) -> dict[str, str | None]:
        return {k["name"]: k.get("rolle") if isinstance(k.get("rolle"), str) else None
                for k in bauplan.get("knoten", [])
                if isinstance(k, dict) and k.get("typ") == "agent" and isinstance(k.get("name"), str)}

    def liste(self, skill: object, ort: object) -> dict:
        w = self.werkstatt
        bauplan = w.bauplan()
        if not isinstance(skill, str) or skill not in self._skills(bauplan):
            raise ks.KontextFehler(ABLAUF_FEHLT, 404)
        ort_pfad = self._ort(ort)
        felder, _ = w.agenten_memory()
        rollen = self._rollen(bauplan)
        agenten = [(name, rollen.get(name), felder.get(name)) for name in kennzahlen.agenten_der_pipeline(bauplan, skill)]
        u = self._umgebung(skill)
        gefunden = suche.suche(skill, ort_pfad, agenten, u)
        ziele: list[ks.KontextZiel] = []
        ids: dict[str, str] = {}

        def eintrag(fund: suche.Fund) -> dict | None:
            ziel = ks.ziel_aus(fund, w.ablage.geheimnis, u.home, skill, w.ablage.kontext_nachfolger)
            if ziel is None:
                return None
            ziele.append(ziel)
            ids[fund.schluessel] = ziel.id
            return {"id": ziel.id, "anzeige": ziel.anzeige, "name": fund.datei, "bytes": fund.bytes,
                    "geaendert": fund.geaendert, "reichweite": fund.reichweite, "bearbeitbar": fund.bearbeitbar,
                    "grund": fund.grund, "_fund": fund}

        def fertig(dateien: list[dict]) -> list[dict]:
            for d in dateien:
                fund = d.pop("_fund")
                von = fund.eingebunden_von
                d["eingebunden_von"] = ids.get(suche.schluessel(von)) if von is not None else None
                d["einbindung"] = fund.einbindung if d["eingebunden_von"] is not None else None
                d["auch_von"] = [ids[suche.schluessel(p)] for p in fund.auch_von if suche.schluessel(p) in ids]
            return dateien

        memory = []
        for m in gefunden.memory:
            dateien = [d for d in (eintrag(f) for f in m["dateien"]) if d is not None]
            memory.append({"agent": m["agent"], "rolle": m["rolle"], "bereich": m["bereich"],
                           "ordner": suche.anzeige(m["ordner"], u.home) if m["ordner"] is not None else None,
                           "gibt_es": m["gibt_es"], "dateien": dateien, "weitere": m["weitere"],
                           **({"weitere_dateien": m["weitere_dateien"]} if m.get("weitere_dateien") else {})})
        kontext = [{"gruppe": name, "dateien": [d for d in (eintrag(f) for f in funde) if d is not None],
                    **({"weitere": weg} if weg else {})}
                   for name, funde, weg in gefunden.gruppen]
        for m in memory:
            fertig(m["dateien"])
        for g in kontext:
            fertig(g["dateien"])
        verweise = [{"von": ids[suche.schluessel(von)], "pfad": roh, "grund": grund}
                    for von, roh, grund in gefunden.verweise if suche.schluessel(von) in ids]
        self.register.ersetze(skill, ort_pfad, ziele)
        return {"ort": {"pfad": str(ort_pfad), "anzeige": suche.anzeige(ort_pfad, u.home)} if ort_pfad is not None else None,
                "orte": [{"pfad": str(o), "anzeige": suche.anzeige(o, u.home)} for o in self._orte()],
                "auto_memory_aus": werkstatt_einstellungen.auto_memory_aus(os.environ, w.konfiguration),
                "schreibbar": w.schreibbar, "memory": memory, "kontext": kontext, "weitere": gefunden.weitere,
                "verweise_ohne_datei": verweise, **({"gekuerzt": True} if gefunden.gekuerzt else {})}

    def datei(self, kid: object) -> dict:
        ziel = self.register.hole(kid)
        roh = ks.lies_zur_anzeige(ziel, self._verboten)
        try:
            if b"\x00" in roh:
                raise UnicodeDecodeError("utf-8", roh, 0, 1, "NUL")
            text, bom, zeilenende = ks.text_von(roh)
        except UnicodeDecodeError:
            raise ks.KontextFehler(ks.liest_nicht(suche.KEIN_TEXT))
        return {"id": ziel.id, "anzeige": ziel.anzeige, "text": text, "sha256": hashlib.sha256(roh).hexdigest(),
                "bom": bom, "zeilenende": zeilenende, "bearbeitbar": ziel.bearbeitbar, "grund": ziel.grund}

    @staticmethod
    def _text(text: object) -> str:
        if not isinstance(text, str) or "\x00" in text:
            raise ks.KontextFehler(TEXT_UNGUELTIG, 400)
        try:
            if len(text.encode("utf-8")) > ks.GRENZE_BYTES:
                raise ks.KontextFehler(TEXT_UNGUELTIG, 400)
        except UnicodeEncodeError:
            raise ks.KontextFehler(TEXT_UNGUELTIG, 400)
        return text

    def satz(self, anfrage: dict) -> dict:
        w = self.werkstatt
        for feld in anfrage:
            if feld not in SATZ_FELDER:
                raise ks.KontextFehler(unbekanntes_feld(str(feld)[:40]), 400)
        if not w.schreibbar:
            raise ks.KontextFehler(texte.WERKSTATT_NUR_LESEN, 403)
        ziel = self.register.hole(anfrage.get("id"))
        if not ziel.bearbeitbar:
            raise ks.KontextFehler(str(ziel.grund))
        text = self._text(anfrage.get("text"))
        roh = ks.lies(ziel, self._verboten)
        sha256 = hashlib.sha256(roh).hexdigest()
        if anfrage.get("sha256") != sha256:
            raise ks.KontextFehler(geaendert_seit_oeffnen(ziel.anzeige))
        try:
            alt, _, zeilenende = ks.text_von(roh)
        except UnicodeDecodeError:
            raise ks.KontextFehler(ks.liest_nicht(suche.KEIN_TEXT))
        if text.replace("\r\n", "\n") == alt:
            raise ks.KontextFehler(NICHTS_GEAENDERT, 400)
        satz = {"schema": SCHEMA, "titel": titel(ziel), "herkunft": "hand",
                "eintraege": [{"id": "k1", "beschreibung": beschreibung(ziel, zeilenende == ks.GEMISCHT),
                               "datei_ersetzen": {"datei_id": ziel.id, "sha256": sha256, "inhalt": text}}]}
        return {"ok": True, "vorschlag": w.lege_vor_kontext(satz, {"k1": ziel})}
