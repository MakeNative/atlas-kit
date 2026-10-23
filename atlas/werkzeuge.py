"""Werkzeug-Knoten aus tools/*.py (nur diese Ebene)."""
from __future__ import annotations


from befunde import Befunde
from docstring import erster_absatz, modul_docstring, ohne_schlusspunkt
from ordner import Ordner


def lies_werkzeuge(quelle: Ordner, befunde: Befunde) -> list[dict]:
    """Werkzeug-Knoten nach Dateiname. Kein Docstring: beschreibung null ohne Befund."""
    if not quelle.mit_werkzeugen:
        return []
    kit = quelle.wurzel
    ordner = kit / "tools"
    if not ordner.is_dir():
        return []
    knoten = []
    for pfad in sorted(ordner.glob("*.py"), key=lambda p: p.name):
        if not pfad.is_file():
            continue
        datei = f"tools/{pfad.name}"
        doc = modul_docstring(kit, datei, befunde)
        beschreibung = None
        if doc is not None and doc.vorhanden:
            beschreibung = ohne_schlusspunkt(erster_absatz(doc)) or None
        knoten.append({
            "id": f"werkzeug:{pfad.stem}",
            "typ": "werkzeug",
            "datei": datei,
            "beschreibung": beschreibung,
        })
    return knoten
