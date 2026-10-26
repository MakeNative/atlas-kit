"""Werkzeug-Knoten aus tools/*.py (nur diese Ebene)."""
from __future__ import annotations


from befunde import Befunde
from docstring import erster_absatz, modul_docstring, ohne_schlusspunkt
from ordner import Ordner
from vokabular import WERKZEUG_ORDNER


def lies_werkzeuge(quelle: Ordner, befunde: Befunde) -> list[dict]:
    """Werkzeug-Knoten nach Dateiname. Kein Docstring: beschreibung null ohne Befund."""
    kit = quelle.wurzel
    knoten = []
    for pfad in quelle.werkzeug_dateien():
        datei = f"{WERKZEUG_ORDNER}/{pfad.name}"
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
