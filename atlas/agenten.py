"""Agent-Knoten aus <agenten>/*.md (Kit: agents, Projekt: .claude/agents; nur diese Ebene)."""
from __future__ import annotations

from pathlib import Path

import agent_prompt
from ableitung import rolle_offen
from befunde import Befunde
from ordner import Ordner
from vokabular import ROLLEN, name_oder
from yaml_lesen import (Frontmatter, ganzzahl_feld, lies_frontmatter, listen_feld,
                        pfad_text, text_feld, wert_text)

ATLAS_SCHLUESSEL = ("rolle", "regeln")


def _atlas_feld(knoten: dict, fm: Frontmatter, befunde: Befunde) -> None:
    if "atlas" not in fm.werte:
        return                  # fehlt oder nach SC-Y5 doppelt (dort schon gemeldet)
    atlas = fm.werte["atlas"]
    if not isinstance(atlas, dict):
        befunde.melde("nicht_unterstuetzt",
                      f"atlas: erwartet eine Abbildung mit rolle und regeln, gefunden "
                      f"{'einen leeren Wert' if atlas is None else wert_text(atlas)}; "
                      f"wird nicht gelesen",
                      fm.datei, fm.zeile("atlas"), aussage="atlas")
        return
    for schluessel in atlas:
        if schluessel not in ATLAS_SCHLUESSEL:
            befunde.melde("nicht_unterstuetzt",
                          f"Unbekannter Schlüssel {pfad_text(('atlas', schluessel))} im Agent; "
                          f"erlaubt sind {', '.join(ATLAS_SCHLUESSEL)}",
                          fm.datei, fm.zeile("atlas", schluessel), aussage=pfad_text(("atlas", schluessel)))
    rolle = text_feld(fm, ("atlas", "rolle"), befunde)
    if rolle is not None:
        if rolle in ROLLEN:
            knoten["rolle"] = rolle
        else:
            befunde.melde("widerspruch",
                          f"atlas.rolle '{rolle}' ist keine erlaubte Rolle "
                          f"({' | '.join(ROLLEN)}); das Feld rolle entfällt",
                          fm.datei, fm.zeile("atlas", "rolle"), aussage="atlas.rolle")
    regeln = listen_feld(fm, ("atlas", "regeln"), befunde)
    if regeln is not None:
        knoten["regeln"] = regeln


def _nur_im_kit(kit: Path, datei: str) -> bool:
    """Wie der Pfadschutz der Werkstatt (WS-X5): kein Symlink auf dem Weg, aufgelöst unter dem Kit (Review r20-4)."""
    pfad = kit
    for teil in datei.split("/"):
        pfad = pfad / teil
        if pfad.is_symlink():
            return False
    try:
        pfad.resolve().relative_to(kit.resolve())
    except (OSError, ValueError):
        return False
    return True


def prompt_feld(fm: Frontmatter, kit: Path) -> dict:
    """SC-PR: Kopf, Teile, fehlende Teile, ggf. der abgeleitete Hinweis auf Fertig und der rohe Text der Datei.
    Eine Datei hinter einem Symlink bzw. außerhalb des Kits liest die Gliederung nicht (SC-PR7)."""
    if not _nur_im_kit(kit, fm.datei):
        return {"kopf": [], "teile": [], "fehlt": [], "roh": None, "grund": "symlink"}
    g = agent_prompt.gliedere(fm.zeilen, fm.text_ab)
    feld = {"kopf": agent_prompt.kopf(fm.werte, fm.zeile), "teile": g["teile"], "fehlt": g["fehlt"]}
    if "fertig_hinweis" in g:
        feld["fertig_hinweis"] = {"herkunft": "abgeleitet",
                                  "beleg": {"datei": fm.datei, **g["fertig_hinweis"]}}
    if "offener_zaun" in g:
        feld["offener_zaun"] = g["offener_zaun"]
    feld["roh"] = "\n".join(fm.zeilen)
    return feld


def lies_agenten(quelle: Ordner, befunde: Befunde) -> list[dict]:
    """Agent-Knoten nach Name sortiert. Unlesbares Frontmatter: kein Knoten (SC-F3)."""
    kit = quelle.wurzel
    knoten: dict[str, dict] = {}
    for pfad in quelle.agent_dateien():
        datei = f"{quelle.agenten}/{pfad.name}"
        fm = lies_frontmatter(kit, datei, befunde)
        if fm is None:
            continue
        name = name_oder(text_feld(fm, ("name",), befunde), pfad.stem)
        if name in knoten:
            befunde.melde("widerspruch",
                          f"Agent {name} ist doppelt definiert (auch in {knoten[name]['datei']}); "
                          f"{datei} wird nicht gelesen",
                          datei, fm.zeile("name"), aussage="name", ziel_art="agent", ziel=name)
            continue
        tools = listen_feld(fm, ("tools",), befunde, komma_string=True)
        if fm.hat("disallowedTools"):
            darf_nicht = listen_feld(fm, ("disallowedTools",), befunde, komma_string=True)
        else:
            darf_nicht = []
        k = {
            "id": f"agent:{name}",
            "typ": "agent",
            "name": name,
            "beschreibung": text_feld(fm, ("description",), befunde),
            "datei": datei,
            "tools": tools,
            "darf_nicht": darf_nicht,
            "model": text_feld(fm, ("model",), befunde),
            "max_runden": ganzzahl_feld(fm, ("maxTurns",), befunde),
        }
        _atlas_feld(k, fm, befunde)
        k["prompt"] = prompt_feld(fm, kit)
        k["_hat_atlas"] = "atlas" in fm.schluessel_oben
        k["_rolle_offen"] = "rolle" not in k and rolle_offen(fm.werte.get("atlas"))
        knoten[name] = k
    return [knoten[n] for n in sorted(knoten)]
