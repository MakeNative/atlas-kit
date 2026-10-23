"""atlas.json zusammensetzen, schreiben und die Zusammenfassung für stdout bilden."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from befunde import Befunde

SCHEMA = "atlas/0.1"


def _ohne_intern(obj: dict) -> dict:
    """Entfernt interne Hilfsfelder (Schlüssel mit führendem _)."""
    return {k: v for k, v in obj.items() if not k.startswith("_")}


def baue_atlas(kit: dict, knoten: list[dict], kanten: list[dict], befunde: Befunde) -> dict:
    """Knoten und Kanten kommen schon in Ausgabereihenfolge; Befunde werden hier sortiert."""
    return {
        "schema": SCHEMA,
        "erzeugt": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "kit": kit,
        "knoten": [_ohne_intern(k) for k in knoten],
        "kanten": kanten,
        "befunde": befunde.als_liste(),
    }


def schreibe(pfad: Path, atlas: dict) -> None:
    """UTF-8, Einrückung 2, kein \\u-Escaping, Zeilenumbruch am Ende. Legt Elternordner an."""
    pfad.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(atlas, ensure_ascii=False, indent=2) + "\n"
    with open(pfad, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def zusammenfassung(atlas: dict, pfad: Path, entfallen: int | None = None) -> str:
    """Je Zahl eine Zeile '<Bezeichnung>: <Zahl>', dazu 'Ausgabe: <Pfad>'."""
    def anzahl(typ: str) -> int:
        return sum(1 for k in atlas["knoten"] if k.get("typ") == typ)

    zeilen = [
        f"Bauplan von {atlas['kit']['id']} geschrieben.",
        f"Skills: {anzahl('skill')}",
        f"Agenten: {anzahl('agent')}",
        f"Hooks: {anzahl('hook')}",
        f"Werkzeuge: {anzahl('werkzeug')}",
        f"Schritte: {anzahl('schritt')}",
        f"Kanten: {len(atlas['kanten'])}",
        f"Befunde: {len(atlas['befunde'])}",
    ]
    if entfallen is not None:
        abgeleitete = [k for k in atlas["knoten"] if k.get("typ") == "schritt" and k.get("herkunft") == "abgeleitet"]
        rollen = sum(1 for k in atlas["knoten"] if k.get("rolle_herkunft") == "abgeleitet")
        zeilen += [
            f"Abgeleitete Abläufe: {len({k['skill'] for k in abgeleitete})}",
            f"Abgeleitete Schritte: {len(abgeleitete)}",
            f"Abgeleitete Rollen: {rollen}",
            f"Entfallene Schritte: {entfallen}",
        ]
    zeilen.append(f"Ausgabe: {os.path.abspath(pfad)}")
    return "\n".join(zeilen) + "\n"
