"""Skill-Knoten aus <skills>/<ordner>/SKILL.md (Kit: skills, Projekt: .claude/skills), Feld agenten und atlas-Feld (ohne schritte)."""
from __future__ import annotations

import re

from befunde import Befunde
from ordner import Ordner
from yaml_lesen import Frontmatter, lies_frontmatter, listen_feld, pfad_text, text_feld, wert_text

ATLAS_SCHLUESSEL = ("nutzen", "eingabe", "ergebnis", "schritte")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def beteiligte_agenten(zeilen: list[str], text_ab: int, praefix: str | None,
                       agent_namen: set[str]) -> list[dict]:
    """SC-K1 bis SC-K3: je Fundstelle eines Agenten-Namens als Inline-Code ein Eintrag."""
    funde: set[tuple[int, str]] = set()
    for nr in range(text_ab, len(zeilen) + 1):
        for m in _INLINE_CODE.finditer(zeilen[nr - 1]):
            name = m.group(1)
            if praefix is not None and name.startswith(praefix):
                name = name[len(praefix):]
            if name in agent_namen:
                funde.add((nr, name))
    return [{"name": name, "zeile": nr} for nr, name in sorted(funde)]


def _atlas_felder(knoten: dict, fm: Frontmatter, befunde: Befunde) -> None:
    """nutzen, eingabe, ergebnis und unbekannte Schlüssel. schritte liest ablauf.py."""
    if "atlas" not in fm.werte:
        return                  # fehlt oder nach SC-Y5 doppelt (dort schon gemeldet)
    atlas = fm.werte["atlas"]
    if not isinstance(atlas, dict):
        befunde.melde("nicht_unterstuetzt",
                      f"atlas: erwartet eine Abbildung mit nutzen und schritte, gefunden "
                      f"{'einen leeren Wert' if atlas is None else wert_text(atlas)}; "
                      f"keine Felder und keine Schritte aus atlas",
                      fm.datei, fm.zeile("atlas"))
        return
    for schluessel in atlas:
        if schluessel not in ATLAS_SCHLUESSEL:
            befunde.melde("nicht_unterstuetzt",
                          f"Unbekannter Schlüssel {pfad_text(('atlas', schluessel))} in der Skill; "
                          f"erlaubt sind {', '.join(ATLAS_SCHLUESSEL)}",
                          fm.datei, fm.zeile("atlas", schluessel))
    if not fm.hat("atlas", "nutzen"):
        befunde.melde("pflichtfeld_fehlt", "Im atlas-Feld fehlt das Pflichtfeld nutzen",
                      fm.datei, fm.zeile("atlas"))
    else:
        nutzen = text_feld(fm, ("atlas", "nutzen"), befunde)
        if nutzen is not None:
            knoten["nutzen"] = nutzen
    for feld in ("eingabe", "ergebnis"):
        liste = listen_feld(fm, ("atlas", feld), befunde)
        if liste is not None:
            knoten[feld] = liste


def _schritte_eigen(fm: Frontmatter) -> bool:
    atlas = fm.werte.get("atlas")
    return isinstance(atlas, dict) and "schritte" in atlas


def lies_skills(quelle: Ordner, kit_id: str, agent_namen: set[str],
                befunde: Befunde) -> list[tuple[dict, Frontmatter]]:
    """Skill-Knoten nach Name sortiert, je mit ihrem Frontmatter."""
    kit = quelle.wurzel
    befehl = "/" if quelle.ist_projekt else f"/{kit_id}:"
    praefix = None if quelle.ist_projekt else f"{kit_id}:"
    ordner = kit / quelle.skills
    if not ordner.is_dir():
        return []
    gefunden: dict[str, tuple[dict, Frontmatter]] = {}
    for unter in sorted(p for p in ordner.iterdir() if p.is_dir()):
        if not (unter / "SKILL.md").is_file():
            continue
        datei = f"{quelle.skills}/{unter.name}/SKILL.md"
        fm = lies_frontmatter(kit, datei, befunde)
        if fm is None:
            continue
        name = text_feld(fm, ("name",), befunde) or unter.name
        if name in gefunden:
            befunde.melde("widerspruch",
                          f"Skill {name} ist doppelt definiert (auch in {gefunden[name][0]['datei']}); "
                          f"{datei} wird nicht gelesen",
                          datei, fm.zeile("name"))
            continue
        hinweis = text_feld(fm, ("argument-hint",), befunde)
        einstieg = f"{befehl}{name}" + (f" {hinweis}" if hinweis else "")
        knoten = {
            "id": f"skill:{name}",
            "typ": "skill",
            "name": name,
            "beschreibung": text_feld(fm, ("description",), befunde),
            "einstieg": einstieg,
            "datei": datei,
            "beleg": {"datei": datei, "zeile": fm.zeile("name") if "name" in fm.werte else 1},
        }
        _atlas_felder(knoten, fm, befunde)
        knoten["agenten"] = beteiligte_agenten(fm.zeilen, fm.text_ab, praefix, agent_namen)
        knoten["_hat_atlas"] = "atlas" in fm.schluessel_oben
        knoten["_schritte_eigen"] = _schritte_eigen(fm)
        gefunden[name] = (knoten, fm)
    return [gefunden[n] for n in sorted(gefunden)]
