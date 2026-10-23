"""Modul-Docstring eines Python-Skripts mit Zeilennummern (Hooks und Werkzeuge)."""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from befunde import Befunde
from textdatei import lies_zeilen

_OEFFNEN = re.compile(r'^[rRuU]{0,2}("""|\'\'\'|"|\')')


@dataclass
class Docstring:
    # (Zeilennummer in der Datei, Rohtext ohne die Anführungszeichen des Docstrings).
    # Leer, wenn das Modul keinen Docstring hat.
    zeilen: list[tuple[int, str]] = field(default_factory=list)

    @property
    def vorhanden(self) -> bool:
        return any(t.strip() for _, t in self.zeilen)

    def erste_zeile(self) -> tuple[int, str] | None:
        """Erste nicht leere Docstring-Zeile ohne führende Leerzeichen."""
        for nr, text in self.zeilen:
            if text.strip():
                return nr, text.lstrip()
        return None


def modul_docstring(kit: Path, datei: str, befunde: Befunde) -> Docstring | None:
    """Liest den Modul-Docstring von kit/datei.

    Rückgabe: None, wenn die Datei nicht lesbar ist (Befund nicht_lesbar ist dann
    gemeldet); Docstring mit leeren zeilen, wenn das Modul keinen Docstring hat.
    """
    try:
        quelltext = "\n".join(lies_zeilen(kit, datei))
    except UnicodeDecodeError:
        befunde.melde("nicht_lesbar", f"{datei} ist nicht als UTF-8 lesbar", datei, None)
        return None
    except OSError as e:
        befunde.melde("nicht_lesbar", f"{datei} lässt sich nicht öffnen ({e.strerror})",
                      datei, None)
        return None
    try:
        modul = ast.parse(quelltext, filename=datei)
    except SyntaxError as e:
        befunde.melde("nicht_lesbar", f"{datei} ist kein gültiges Python: {e.msg}",
                      datei, e.lineno)
        return None
    except ValueError as e:
        befunde.melde("nicht_lesbar", f"{datei} ist kein gültiges Python: {e}", datei, None)
        return None

    if not modul.body:
        return Docstring()
    erster = modul.body[0]
    if not (isinstance(erster, ast.Expr) and isinstance(erster.value, ast.Constant)
            and isinstance(erster.value.value, str)):
        return Docstring()
    segment = ast.get_source_segment(quelltext, erster.value)
    if segment is None:
        return Docstring()
    m = _OEFFNEN.match(segment)
    if m is None:
        return Docstring()
    zeichen = m.group(1)
    innen = segment[m.end():]
    if innen.endswith(zeichen):
        innen = innen[: -len(zeichen)]
    zeilen = []
    for i, text in enumerate(innen.split("\n")):
        zeilen.append((erster.value.lineno + i, text))
    return Docstring(zeilen)


def erster_absatz(doc: Docstring) -> str:
    """Erster Absatz (bis zur ersten Leerzeile), Zeilen getrimmt und mit Leerzeichen verbunden."""
    teile: list[str] = []
    for _, text in doc.zeilen:
        t = text.strip()
        if not t:
            if teile:
                break
            continue
        teile.append(t)
    return " ".join(teile)


def ohne_schlusspunkt(text: str) -> str:
    """Entfernt einen abschließenden Punkt (bewacht am Hook, beschreibung am Werkzeug)."""
    return text[:-1] if text.endswith(".") else text
