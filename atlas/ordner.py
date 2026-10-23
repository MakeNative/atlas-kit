from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PLUGIN_JSON = ".claude-plugin/plugin.json"
PLUGIN = "plugin"
PROJEKT = "projekt"


class OrdnerFehler(Exception):
    pass


@dataclass(frozen=True)
class Ordner:
    wurzel: Path
    art: str
    skills: str
    agenten: str
    hooks: str
    mit_werkzeugen: bool

    @property
    def ist_projekt(self) -> bool:
        return self.art == PROJEKT


KIT_FORM = dict(art=PLUGIN, skills="skills", agenten="agents", hooks="hooks/hooks.json", mit_werkzeugen=True)
PROJEKT_FORM = dict(art=PROJEKT, skills=".claude/skills", agenten=".claude/agents",
                    hooks=".claude/settings.json", mit_werkzeugen=False)


def _ist_home(wurzel: Path) -> bool:
    return os.path.realpath(wurzel) == os.path.realpath(Path.home())


def _hat_eigene_skills_oder_agenten(wurzel: Path) -> bool:
    skills = wurzel / PROJEKT_FORM["skills"]
    if skills.is_dir() and any((unter / "SKILL.md").is_file() for unter in skills.iterdir() if unter.is_dir()):
        return True
    agenten = wurzel / PROJEKT_FORM["agenten"]
    return agenten.is_dir() and any(pfad.is_file() for pfad in agenten.glob("*.md"))


def erkenne(wurzel: Path) -> Ordner:
    if (wurzel / PLUGIN_JSON).is_file():
        return Ordner(wurzel=wurzel, **KIT_FORM)
    if not _ist_home(wurzel) and _hat_eigene_skills_oder_agenten(wurzel):
        return Ordner(wurzel=wurzel, **PROJEKT_FORM)
    raise OrdnerFehler(f"In {wurzel} liegt weder ein Kit ({PLUGIN_JSON}) noch ein Projekt "
                       f"mit .claude/skills oder .claude/agents.")
