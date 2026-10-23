"""Agent-Knoten aus <agenten>/*.md (Kit: agents, Projekt: .claude/agents; nur diese Ebene)."""
from __future__ import annotations


from befunde import Befunde
from ordner import Ordner
from yaml_lesen import (Frontmatter, ganzzahl_feld, lies_frontmatter, listen_feld,
                        pfad_text, text_feld, wert_text)

ROLLEN = ("planer", "umsetzer", "pruefer")
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
                      fm.datei, fm.zeile("atlas"))
        return
    for schluessel in atlas:
        if schluessel not in ATLAS_SCHLUESSEL:
            befunde.melde("nicht_unterstuetzt",
                          f"Unbekannter Schlüssel {pfad_text(('atlas', schluessel))} im Agent; "
                          f"erlaubt sind {', '.join(ATLAS_SCHLUESSEL)}",
                          fm.datei, fm.zeile("atlas", schluessel))
    rolle = text_feld(fm, ("atlas", "rolle"), befunde)
    if rolle is not None:
        if rolle in ROLLEN:
            knoten["rolle"] = rolle
        else:
            befunde.melde("widerspruch",
                          f"atlas.rolle '{rolle}' ist keine erlaubte Rolle "
                          f"({' | '.join(ROLLEN)}); das Feld rolle entfällt",
                          fm.datei, fm.zeile("atlas", "rolle"))
    regeln = listen_feld(fm, ("atlas", "regeln"), befunde)
    if regeln is not None:
        knoten["regeln"] = regeln


def lies_agenten(quelle: Ordner, befunde: Befunde) -> list[dict]:
    """Agent-Knoten nach Name sortiert. Unlesbares Frontmatter: kein Knoten (SC-F3)."""
    kit = quelle.wurzel
    ordner = kit / quelle.agenten
    if not ordner.is_dir():
        return []
    knoten: dict[str, dict] = {}
    for pfad in sorted(ordner.glob("*.md")):
        if not pfad.is_file():
            continue
        datei = f"{quelle.agenten}/{pfad.name}"
        fm = lies_frontmatter(kit, datei, befunde)
        if fm is None:
            continue
        name = text_feld(fm, ("name",), befunde) or pfad.stem
        if name in knoten:
            befunde.melde("widerspruch",
                          f"Agent {name} ist doppelt definiert (auch in {knoten[name]['datei']}); "
                          f"{datei} wird nicht gelesen",
                          datei, fm.zeile("name"))
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
        k["_hat_atlas"] = "atlas" in fm.schluessel_oben
        k["_rolle_offen"] = "rolle" not in k and fm.hole("atlas", "rolle") is None
        knoten[name] = k
    return [knoten[n] for n in sorted(knoten)]
