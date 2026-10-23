from __future__ import annotations

from pathlib import Path

from docstring import Docstring
from textdatei import lies_zeilen


def _kopfzeile(zeile: str) -> str | None:
    text = zeile.lstrip()
    if not text.startswith("#"):
        return None
    rest = text[1:]
    if not rest.strip():
        return None
    return rest[1:] if rest.startswith(" ") else rest


def kommentarkopf(ordner: Path, datei: str) -> Docstring | None:
    try:
        zeilen = lies_zeilen(ordner, datei)
    except (OSError, UnicodeDecodeError):
        return None
    anfang = 1 if zeilen and zeilen[0].startswith("#!") else 0
    kopf: list[tuple[int, str]] = []
    for index in range(anfang, len(zeilen)):
        text = _kopfzeile(zeilen[index])
        if text is None:
            break
        kopf.append((index + 1, text))
    return Docstring(kopf) if kopf else None
