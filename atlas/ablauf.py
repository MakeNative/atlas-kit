"""Prosa-Schritte, Schritt-Knoten und Schritt-Kanten aus atlas.schritte einer Skill."""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from befunde import Befunde
from vokabular import AUSFUEHRENDE, MENSCH, WERKZEUG_PFAD
from yaml_lesen import Frontmatter, ganzzahl_feld, listen_feld, pfad_text, text_feld

PROSA_FETT = re.compile(r"^\s*(\d+)\. \*\*")
PROSA_SCHLICHT = re.compile(r"^\s*(\d+)\. \S")
ZAUN = re.compile(r"^\s*```")
SCHRITT_SCHLUESSEL = ("id", "titel", "mensch", "agent", "parallel", "werkzeug", "skill", "bei_befund")
BEI_BEFUND_SCHLUESSEL = ("zurueck", "max")


@dataclass
class ProsaSchritt:
    nr: int
    zeile: int
    zitat: str


def prosa_schritte(zeilen: list[str], text_ab: int) -> list[ProsaSchritt]:
    """SC-P1: Zeilen ab text_ab außerhalb von ```-Blöcken, fett '<zahl>. **' oder schlicht '<zahl>. x'.

    Je Datei gilt eine Form: gibt es eine fette Zeile, zählen nur die fetten, sonst die schlichten.
    """
    fett = []
    schlicht = []
    im_code = False
    for nr in range(text_ab, len(zeilen) + 1):
        text = zeilen[nr - 1]
        if ZAUN.match(text):
            im_code = not im_code
            continue
        if im_code:
            continue
        m = PROSA_SCHLICHT.match(text)
        if not m:
            continue
        schritt = ProsaSchritt(int(m.group(1)), nr, text.lstrip())
        schlicht.append(schritt)
        if PROSA_FETT.match(text):
            fett.append(schritt)
    return fett or schlicht


@dataclass
class _Schritt:
    pos: int                    # Index in atlas.schritte
    id: str | None = None
    titel: str | None = None
    mensch: str | None = None
    agent: str | None = None
    parallel: list | None = None
    werkzeug: str | None = None
    skill: str | None = None
    zurueck: str | None = None
    max: int | None = None


# ---------------------------------------------------------------- Schritte lesen

def _id_oder_none(roh: dict) -> str | None:
    return roh["id"] if isinstance(roh.get("id"), str) else None


def _lies_bei_befund(fm: Frontmatter, basis: tuple, s: _Schritt, befunde: Befunde) -> None:
    """bei_befund eines Schritts: zurueck (SC-E3) und max (SC-E5)."""
    pfad = basis + ("bei_befund",)
    bb = fm.hole(*pfad)
    if bb is None:
        return
    if not isinstance(bb, dict):
        befunde.melde("nicht_unterstuetzt",
                      f"{pfad_text(pfad)}: erwartet eine Abbildung mit zurueck und max",
                      fm.datei, fm.zeile(*pfad), schritt=s.id, aussage="bei_befund")
        return
    for schluessel in bb:
        if schluessel not in BEI_BEFUND_SCHLUESSEL:
            befunde.melde("nicht_unterstuetzt",
                          f"Unbekannter Schlüssel {schluessel} in {pfad_text(pfad)}; "
                          f"erlaubt sind zurueck, max",
                          fm.datei, fm.zeile(*pfad, schluessel), schritt=s.id, aussage=f"bei_befund.{schluessel}")
    if fm.hat(*pfad, "zurueck"):
        s.zurueck = text_feld(fm, pfad + ("zurueck",), befunde)
    else:
        befunde.melde("pflichtfeld_fehlt",
                      f"{pfad_text(pfad)} von Schritt {s.id or s.pos + 1} hat kein "
                      f"Pflichtfeld zurueck; keine zurueck-Kante",
                      fm.datei, fm.zeile(*pfad), schritt=s.id, aussage="zurueck")
    s.max = ganzzahl_feld(fm, pfad + ("max",), befunde, minimum=1,
                          folge="die Kante bekommt kein max")


def _lies_schritt(fm: Frontmatter, i: int, befunde: Befunde) -> tuple[_Schritt, bool]:
    """Liest Eintrag i aus atlas.schritte. Rückgabe: Schritt und ob er die Skill trägt (SC-S1)."""
    basis = ("atlas", "schritte", i)
    s = _Schritt(pos=i)
    roh = fm.hole(*basis)
    ort = f"Schritt {i + 1} in atlas.schritte"
    if not isinstance(roh, dict):
        befunde.melde("nicht_unterstuetzt",
                      f"{ort} ist keine Abbildung mit id und titel", fm.datei, fm.zeile(*basis),
                      aussage="schritt")
        return s, False
    for schluessel in roh:
        if schluessel not in SCHRITT_SCHLUESSEL:
            befunde.melde("nicht_unterstuetzt",
                          f"Unbekannter Schlüssel {schluessel} in {ort} "
                          f"({pfad_text(basis + (schluessel,))}); erlaubt sind "
                          f"{', '.join(SCHRITT_SCHLUESSEL)}",
                          fm.datei, fm.zeile(*basis, schluessel), schritt=_id_oder_none(roh),
                          aussage=f"schritt.{schluessel}")
    gut = True
    for feld in ("id", "titel"):
        if not fm.hat(*basis, feld):
            befunde.melde("pflichtfeld_fehlt", f"{ort} hat kein Pflichtfeld {feld}",
                          fm.datei, fm.zeile(*basis), schritt=_id_oder_none(roh), aussage=feld)
            gut = False
    s.id = text_feld(fm, basis + ("id",), befunde)
    if s.id is None:
        gut = False
    s.titel = text_feld(fm, basis + ("titel",), befunde)

    mensch = text_feld(fm, basis + ("mensch",), befunde)
    if mensch in MENSCH:
        s.mensch = mensch
    elif mensch is not None:
        befunde.melde("widerspruch",
                      f"{ort}: mensch '{mensch}' ist nicht erlaubt ({' | '.join(MENSCH)}); "
                      f"mensch wird null",
                      fm.datei, fm.zeile(*basis, "mensch"), schritt=s.id, aussage="mensch")

    s.agent = text_feld(fm, basis + ("agent",), befunde)
    s.parallel = listen_feld(fm, basis + ("parallel",), befunde)
    s.werkzeug = text_feld(fm, basis + ("werkzeug",), befunde)
    s.skill = text_feld(fm, basis + ("skill",), befunde)
    genannt = [f for f in AUSFUEHRENDE if fm.hat(*basis, f)]
    if len(genannt) > 1:
        befunde.melde("widerspruch",
                      f"Schritt {s.id or i + 1} nennt mehr als einen Ausführenden "
                      f"({', '.join(genannt)})",
                      fm.datei, fm.zeile(*basis), schritt=s.id, aussage="startziele")
    _lies_bei_befund(fm, basis, s, befunde)
    return s, gut


def _ids_eindeutig(schritte: list[_Schritt], fm: Frontmatter, befunde: Befunde) -> bool:
    """SC-S2: doppelte Schritt-ids sind ein Widerspruch."""
    gesehen: dict[str, int] = {}
    eindeutig = True
    for s in schritte:
        if s.id is None:
            continue
        if s.id in gesehen:
            befunde.melde("widerspruch",
                          f"Die Schritt-id {s.id} steht in atlas.schritte doppelt "
                          f"(Schritt {gesehen[s.id] + 1} und {s.pos + 1})",
                          fm.datei, fm.zeile("atlas", "schritte", s.pos), schritt=s.id, aussage="id")
            eindeutig = False
        else:
            gesehen[s.id] = s.pos
    return eindeutig


def _passt_zur_prosa(prosa: list[ProsaSchritt], anzahl: int, fm: Frontmatter,
                     befunde: Befunde) -> bool:
    """SC-P4 und SC-P5. True, wenn Feld und Prosa zusammenpassen."""
    gut = True
    for i, p in enumerate(prosa):
        if p.nr != i + 1:
            vorher = prosa[i - 1].nr if i else None
            folge = ", ".join(str(q.nr) for q in prosa)
            befunde.melde("widerspruch",
                          f"Die nummerierten Prosa-Schritte laufen nicht lückenlos 1, 2, …: "
                          f"Schritt {p.nr} in Zeile {p.zeile} "
                          f"{'steht am Anfang' if vorher is None else f'folgt auf {vorher}'} "
                          f"(Folge {folge})",
                          fm.datei, p.zeile, aussage="prosa")
            gut = False
            break
    if anzahl != len(prosa):
        befunde.melde("widerspruch",
                      f"atlas.schritte nennt {anzahl} Schritte, die Prosa {len(prosa)}",
                      fm.datei, fm.zeile("atlas", "schritte"), aussage="anzahl")
        gut = False
    return gut


def _lies_schritte(fm: Frontmatter, befunde: Befunde) -> tuple[list[_Schritt], bool] | None:
    """atlas.schritte lesen (SC-C1, SC-C2, SC-S1, SC-S2).

    None: es gibt keine Schritt-Liste. Sonst die Schritte und ob sie die Skill tragen
    (False bei SC-S1/SC-S2, dann entstehen wie bei SC-P6 keine Schritte).
    """
    if not isinstance(fm.hole("atlas"), dict):
        return None             # skills.py hat den Befund zum atlas-Feld schon gemeldet
    if not fm.hat("atlas", "schritte"):
        befunde.melde("pflichtfeld_fehlt", "Im atlas-Feld fehlt das Pflichtfeld schritte",
                      fm.datei, fm.zeile("atlas"), aussage="atlas.schritte")
        return None
    roh = fm.hole("atlas", "schritte")
    if not isinstance(roh, list) or not roh:
        befunde.melde("nicht_unterstuetzt",
                      f"atlas.schritte: erwartet eine nicht leere Liste, gefunden "
                      f"{'eine leere Liste' if roh == [] else repr(roh)}; keine Schritte",
                      fm.datei, fm.zeile("atlas", "schritte"), aussage="atlas.schritte")
        return None
    schritte = []
    gut = True
    for i in range(len(roh)):
        s, ok = _lies_schritt(fm, i, befunde)
        schritte.append(s)
        gut = gut and ok
    gut = _ids_eindeutig(schritte, fm, befunde) and gut
    return schritte, gut


# ---------------------------------------------------------------- Knoten und Kanten

def _beleg(datei: str, p: ProsaSchritt) -> dict:
    return {"datei": datei, "zeile": p.zeile, "zitat": p.zitat}


def _kante(von: str, nach: str, typ: str, beleg: dict, **extra) -> dict:
    k = {"von": von, "nach": nach, "typ": typ, "herkunft": "konvention", "beleg": beleg}
    k.update(extra)
    return k


def _verweis_fehlt(befunde: Befunde, datei: str, s: _Schritt, p: ProsaSchritt,
                   was: str, wert: str, aussage: str, ziel_art: str) -> None:
    """SC-E2: Befund mit Datei und Zeile des Prosa-Schritts."""
    befunde.melde("verweis_fehlt", f"Schritt {s.id}: {was} {wert} gibt es im Kit nicht; keine Kante",
                  datei, p.zeile, schritt=s.id, aussage=aussage, ziel_art=ziel_art, ziel=wert)


def _schritt_knoten(skill: dict, schritte: list[_Schritt], prosa: list[ProsaSchritt],
                    sid: dict[str, str]) -> list[dict]:
    return [{
        "id": sid[s.id],
        "typ": "schritt",
        "skill": skill["id"],
        "nr": s.pos + 1,
        "titel": s.titel,
        "mensch": s.mensch,
        "beleg": _beleg(skill["datei"], p),
    } for s, p in zip(schritte, prosa)]


def _kanten_eines_schritts(n: int, schritte: list[_Schritt], prosa: list[ProsaSchritt],
                           sid: dict[str, str], datei: str, knoten_ids: set[str],
                           fm: Frontmatter, befunde: Befunde) -> list[dict]:
    """Kanten von Schritt n in der Reihenfolge folgt, startet, parallel, zurueck."""
    s, p = schritte[n], prosa[n]
    von = sid[s.id]
    beleg = _beleg(datei, p)
    kanten = []
    if n + 1 < len(schritte):
        kanten.append(_kante(von, sid[schritte[n + 1].id], "folgt", _beleg(datei, prosa[n + 1])))

    werkzeug_ziel = None
    if s.werkzeug is not None:
        m = WERKZEUG_PFAD.match(posixpath.normpath(s.werkzeug))
        werkzeug_ziel = f"werkzeug:{m.group(1)}" if m else None
    startet = [
        (s.agent, f"agent:{s.agent}", "der Agent", "agent"),
        (s.werkzeug, werkzeug_ziel, "das Werkzeug (Datei direkt unter tools/)", "werkzeug"),
        (s.skill, f"skill:{s.skill}", "die Skill", "skill"),
    ]
    for wert, ziel, was, art in startet:
        if wert is None:
            continue
        if ziel in knoten_ids:
            kanten.append(_kante(von, ziel, "startet", beleg))
        else:
            _verweis_fehlt(befunde, datei, s, p, was, wert, art, art)
    for agent in s.parallel or []:
        if f"agent:{agent}" in knoten_ids:
            kanten.append(_kante(von, f"agent:{agent}", "parallel", beleg))
        else:
            _verweis_fehlt(befunde, datei, s, p, "der Agent", agent, "parallel", "agent")

    if s.zurueck is not None:
        ziel = next((t for t in schritte if t.id == s.zurueck), None)
        if ziel is None:
            _verweis_fehlt(befunde, datei, s, p, "der Schritt", s.zurueck, "zurueck", "schritt")
        elif ziel.pos >= s.pos:
            befunde.melde("widerspruch",
                          f"Schritt {s.id}: bei_befund.zurueck zeigt auf {s.zurueck}, "
                          f"denselben oder einen späteren Schritt; keine Kante",
                          datei, fm.zeile("atlas", "schritte", s.pos, "bei_befund", "zurueck"), schritt=s.id,
                          aussage="zurueck", ziel_art="schritt", ziel=s.zurueck)
        else:
            extra = {"max": s.max} if s.max is not None else {}
            kanten.append(_kante(von, sid[s.zurueck], "zurueck", beleg, **extra))
    return kanten


def schritte_und_kanten(skill: dict, fm: Frontmatter, knoten_ids: set[str],
                        befunde: Befunde) -> tuple[list[dict], list[dict]]:
    """Schritt-Knoten und Schritt-Kanten einer Skill. Bei Widerspruch (SC-P6) beide leer."""
    befunde = befunde.mit_skill(skill["name"])
    gelesen = _lies_schritte(fm, befunde)
    if gelesen is None:
        return [], []
    schritte, gut = gelesen
    prosa = prosa_schritte(fm.zeilen, fm.text_ab)
    if not _passt_zur_prosa(prosa, len(schritte), fm, befunde) or not gut:
        return [], []

    sid = {s.id: f"schritt:{skill['name']}/{s.id}" for s in schritte}
    knoten = _schritt_knoten(skill, schritte, prosa, sid)
    kanten = []
    for n in range(len(schritte)):
        kanten.extend(_kanten_eines_schritts(n, schritte, prosa, sid, skill["datei"],
                                             knoten_ids, fm, befunde))
    return knoten, kanten
