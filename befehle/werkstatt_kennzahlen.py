from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import werkstatt_lauf_sicht as sicht

ZUSTAENDE = (sicht.BEENDET, sicht.ABGEBROCHEN, sicht.ABGELEGT)
AGENT_FELDER = ("laeufe", "agentenlaeufe", "fehler", "dauer_s", "tokens", "tokens_unbekannt", "blockiert")
SCHRITT_FELDER = ("laeufe_aktiv", "runden_max", "laeufe_mit_runde_2", "fehler")
ZAEHL_ARTEN = {"fragen": "frage", "freigaben": "freigabe", "eingaben": "eingabe", "blockiert": "blockiert",
               "werkzeug_fehler": "werkzeug_fehler"}


@dataclass
class LaufKopie:
    id: str
    lauf: dict
    bauplan: dict
    zeilen: list[bytes]
    gueltig: dict[int, dict]
    ungueltig: int
    verloren: int
    marken: sicht.Marken
    kit_geaendert: bool
    ordner: str = ""
    kurz: dict = field(default_factory=dict)

    @property
    def ereignisse(self) -> list[dict]:
        return [self.gueltig[nr] for nr in sorted(self.gueltig)]

    @property
    def kit_id(self) -> str:
        kit = self.lauf.get("kit") if isinstance(self.lauf.get("kit"), dict) else {}
        return str(kit.get("id", ""))

    def sicht(self, jetzt: datetime) -> sicht.Sicht:
        return sicht.Sicht(self.lauf, self.bauplan, self.ereignisse, self.marken, jetzt, self.kit_id)


def zerlege(roh: bytes) -> tuple[list[bytes], dict[int, dict], int]:
    zeilen = roh.split(b"\n")
    if zeilen and zeilen[-1] == b"":
        zeilen.pop()
    gueltig: dict[int, dict] = {}
    ungueltig = 0
    for nr, zeile in enumerate(zeilen, 1):
        zeile = zeile.rstrip(b"\r")
        if not zeile.strip():
            continue
        try:
            daten = json.loads(zeile.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            daten = None
        if sicht.gueltig(daten):
            gueltig[nr] = daten
        else:
            ungueltig += 1
    return [z.rstrip(b"\r") for z in zeilen], gueltig, ungueltig


def agenten_der_pipeline(bauplan: dict, skill: str) -> list[str]:
    plan = sicht.Ablaufplan(bauplan if isinstance(bauplan, dict) else {}, skill)
    namen: list[str] = []
    for schritt in plan.schritte:
        for ziel in plan.ziele.get(schritt["id"], []):
            if ziel.startswith("agent:") and ziel[6:] not in namen:
                namen.append(ziel[6:])
    return namen


def skills_je_agent(bauplan: dict) -> dict[str, list[str]]:
    skills = [k.get("name") for k in bauplan.get("knoten", []) if isinstance(k, dict) and k.get("typ") == "skill"
              and isinstance(k.get("name"), str)]
    ergebnis: dict[str, list[str]] = {}
    for skill in skills:
        for agent in agenten_der_pipeline(bauplan, skill):
            ergebnis.setdefault(agent, []).append(skill)
    return ergebnis


def _schritt_schluessel(knoten_id: str) -> str:
    return knoten_id.split("/", 1)[1] if "/" in knoten_id else knoten_id.split(":", 1)[-1]


def kennzahlen(laeufe: list[LaufKopie], bauplan: dict, skill: str, kit_id: str, jetzt: datetime) -> dict:
    plan = sicht.Ablaufplan(bauplan if isinstance(bauplan, dict) else {}, skill)
    agenten = agenten_der_pipeline(bauplan, skill)
    schritte = [s["id"] for s in plan.mit_ausfuehrendem()]
    werkzeuge = list(plan.werkzeuge)
    zahlen: dict[str, int] = {"laeufe": len(laeufe), **{f"laeufe.{z}": 0 for z in ZUSTAENDE}}
    je_agent: dict[str, dict[str, int]] = {a: dict.fromkeys(AGENT_FELDER, 0) for a in agenten}
    je_schritt: dict[str, dict[str, int]] = {s: dict.fromkeys(SCHRITT_FELDER, 0) for s in schritte}
    je_werkzeug: dict[str, dict[str, int]] = {d: {"laeufe": 0, "aufrufe": 0} for _, d in werkzeuge}
    gesamt = dict.fromkeys((*ZAEHL_ARTEN, "ungueltig", "verloren", "nicht_zugeordnet"), 0)
    for kopie in laeufe:
        s = kopie.sicht(jetzt)
        if s.stand.zustand in ZUSTAENDE:
            zahlen[f"laeufe.{s.stand.zustand}"] += 1
        gesamt["ungueltig"] += kopie.ungueltig
        gesamt["verloren"] += kopie.verloren
        for name, art in ZAEHL_ARTEN.items():
            gesamt[name] += sum(1 for e in kopie.ereignisse if e["art"] == art)
        im_lauf: set[str] = set()
        for a in s.agentenlaeufe:
            if a.zuordnung.knoten is None:
                gesamt["nicht_zugeordnet"] += 1
                continue
            if a.name is None or not a.zuordnung.knoten.startswith("agent:"):
                continue
            zeile = je_agent.setdefault(a.name, dict.fromkeys(AGENT_FELDER, 0))
            im_lauf.add(a.name)
            zeile["agentenlaeufe"] += 1
            if a.fehler is not None:
                zeile["fehler"] += 1
            if a.geendet:
                zeile["dauer_s"] += s.dauer_s(a) or 0
                if a.tokens is None:
                    zeile["tokens_unbekannt"] += 1
                else:
                    zeile["tokens"] += a.tokens
        for name in im_lauf:
            je_agent[name]["laeufe"] += 1
        for e in kopie.ereignisse:
            if e["art"] == "blockiert":
                name = sicht._name(e.get("agent_typ"), kopie.kit_id or kit_id, s.plugin)
                if name in je_agent:
                    je_agent[name]["blockiert"] += 1
        runden = {eintrag["knoten"]: eintrag for eintrag in s.schritte()}
        for sid in schritte:
            agentenlaeufe = [a for a in s.agentenlaeufe if sid in a.zuordnung.zaehlende_schritte]
            einzeln = [w for w in s.einzellaeufe if sid in w.zuordnung.zaehlende_schritte]
            if not agentenlaeufe and not einzeln:
                continue
            zeile = je_schritt[sid]
            zeile["laeufe_aktiv"] += 1
            r = runden.get(sid, {}).get("runden", 0)
            zeile["runden_max"] = max(zeile["runden_max"], r)
            if r >= 2:
                zeile["laeufe_mit_runde_2"] += 1
            zeile["fehler"] += sum(1 for a in agentenlaeufe if a.fehler is not None)
        for kid, datei in werkzeuge:
            aufrufe = sum(1 for w in s.einzellaeufe if w.art == "werkzeug" and w.zuordnung.knoten == kid)
            je_werkzeug[datei]["aufrufe"] += aufrufe
            if aufrufe:
                je_werkzeug[datei]["laeufe"] += 1
    for name, zeile in je_agent.items():
        zahlen.update({f"agent.{name}.{feld}": zeile[feld] for feld in AGENT_FELDER})
    for sid, zeile in je_schritt.items():
        zahlen.update({f"schritt.{_schritt_schluessel(sid)}.{feld}": zeile[feld] for feld in SCHRITT_FELDER})
    for datei, zeile in je_werkzeug.items():
        zahlen.update({f"werkzeug.{datei}.laeufe": zeile["laeufe"], f"werkzeug.{datei}.aufrufe": zeile["aufrufe"]})
    zahlen.update(gesamt)
    return zahlen
