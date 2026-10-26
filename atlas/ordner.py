from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from vokabular import AGENT_MUSTER, SKILL_DATEI, WERKZEUG_MUSTER, WERKZEUG_ORDNER

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

    def skill_ordner(self) -> list[Path]:
        """Alle Unterordner des Skill-Ordners (auch über Symlinks), sortiert; eine Skill ist darin SKILL.md."""
        ordner = self.wurzel / self.skills
        if not ordner.is_dir():
            return []
        return sorted(p for p in ordner.iterdir() if p.is_dir())

    def skill_dateien(self, skill_ordner: list[Path] | None = None) -> list[Path]:
        """SKILL.md je Skill-Ordner; skill_ordner, wenn der Aufrufer skill_ordner() schon gelesen hat."""
        ordner = self.skill_ordner() if skill_ordner is None else skill_ordner
        return [unter / SKILL_DATEI for unter in ordner if (unter / SKILL_DATEI).is_file()]

    def agent_dateien(self) -> list[Path]:
        ordner = self.wurzel / self.agenten
        if not ordner.is_dir():
            return []
        return [p for p in sorted(ordner.glob(AGENT_MUSTER)) if p.is_file()]

    def werkzeug_dateien(self) -> list[Path]:
        ordner = self.wurzel / WERKZEUG_ORDNER
        if not self.mit_werkzeugen or not ordner.is_dir():
            return []
        return [p for p in sorted(ordner.glob(WERKZEUG_MUSTER), key=lambda p: p.name) if p.is_file()]

    def dateien(self, skill_ordner: list[Path] | None = None) -> dict[str, list[str]]:
        """Die Dateien, die der Scanner liest, je Quelle, relativ zur Wurzel (ohne die Skripte der Hooks, die
        atlas/hooks.skripte auflöst). Dieselbe Liste nehmen Probe-Kopie, Arbeitskopie und Beobachtung der Werkstatt;
        skill_ordner wie bei skill_dateien."""
        def rel(pfade: list[Path]) -> list[str]:
            return [p.relative_to(self.wurzel).as_posix() for p in pfade]
        manifest = [] if self.ist_projekt or not (self.wurzel / PLUGIN_JSON).is_file() else [PLUGIN_JSON]
        return {
            "manifest": manifest,
            "skills": rel(self.skill_dateien(skill_ordner)),
            "agenten": rel(self.agent_dateien()),
            "werkzeuge": rel(self.werkzeug_dateien()),
            "hooks": [self.hooks] if (self.wurzel / self.hooks).is_file() else [],
        }


KIT_FORM = dict(art=PLUGIN, skills="skills", agenten="agents", hooks="hooks/hooks.json", mit_werkzeugen=True)
PROJEKT_FORM = dict(art=PROJEKT, skills=".claude/skills", agenten=".claude/agents",
                    hooks=".claude/settings.json", mit_werkzeugen=False)


def _ist_home(wurzel: Path) -> bool:
    return os.path.realpath(wurzel) == os.path.realpath(Path.home())


def _hat_eigene_skills_oder_agenten(wurzel: Path) -> bool:
    projekt = Ordner(wurzel=wurzel, **PROJEKT_FORM)
    return bool(projekt.skill_dateien() or projekt.agent_dateien())


def erkenne(wurzel: Path) -> Ordner:
    if (wurzel / PLUGIN_JSON).is_file():
        return Ordner(wurzel=wurzel, **KIT_FORM)
    if not _ist_home(wurzel) and _hat_eigene_skills_oder_agenten(wurzel):
        return Ordner(wurzel=wurzel, **PROJEKT_FORM)
    raise OrdnerFehler(f"In {wurzel} liegt weder ein Kit ({PLUGIN_JSON}) noch ein Projekt "
                       f"mit .claude/skills oder .claude/agents.")
