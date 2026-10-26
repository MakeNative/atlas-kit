"""Vokabular des Bauplans und Regeln zur Lage der Dateien, an einer Stelle.

Scanner (agenten, skills, werkzeuge, ablauf, ableitung) und die Werkstatt des Pakets (Änderungssatz,
Schreiber) lesen diese Werte von hier, damit eine Rolle, eine Mensch-Art oder eine Namensregel nie an
zwei Stellen verschieden gilt. Nur Standardbibliothek.
"""
from __future__ import annotations

import re

ROLLEN = ("planer", "umsetzer", "pruefer")
MENSCH = ("rueckfrage", "freigabe", "abnahme")
AUSFUEHRENDE = ("agent", "parallel", "werkzeug", "skill")
SCHRITT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
WERKZEUG_PFAD = re.compile(r"^tools/([^/]+)\.py$")
WERKZEUG_ORDNER = "tools"
WERKZEUG_MUSTER = "*.py"
AGENT_MUSTER = "*.md"
SKILL_DATEI = "SKILL.md"
# Die Sätze, die die Werkstatt selbst in den Text einer Skill schreibt (WS-W5 bis WS-W7, WS-KE3): Startsatz, Mensch-Satz,
# Schleifen-Satz. Bauen und Erkennen geschieht nur hier; die Leinwand hat dieselben Bausteine in
# web/lib/werkstatt/saetze.ts (ein Test ruft beide auf und vergleicht).
MENSCH_SATZ = {
    "rueckfrage": "Frag den Nutzer, wenn etwas fehlt.",
    "freigabe": "Warte auf die Freigabe des Nutzers, bevor es weitergeht.",
    "abnahme": "Der Nutzer nimmt das Ergebnis ab.",
}


def name_oder(wert: object, rueckfall: str) -> str:
    """Name eines Agenten bzw. einer Skill: das Feld name, sonst Datei- bzw. Ordnername."""
    return wert if isinstance(wert, str) and wert else rueckfall


def schritte_eigen(atlas: object) -> bool:
    """Trägt das atlas-Feld einer Skill eigene Schritte (AB-V1)?"""
    return isinstance(atlas, dict) and "schritte" in atlas


def aufruf_kopf(schritt: dict) -> str | None:
    """Anfang des Satzes, der den Ausführenden eines Schritts ausdrücklich startet (WS-W6), ohne Satzzeichen;
    None ohne Ausführenden."""
    if schritt.get("agent"):
        return f"Starte den Agenten `{schritt['agent']}`"
    if schritt.get("parallel"):
        return "Starte gleichzeitig die Agenten " + ", ".join(f"`{n}`" for n in schritt["parallel"])
    if schritt.get("werkzeug"):
        return f"Führ das Werkzeug `{schritt['werkzeug']}` aus"
    if schritt.get("skill"):
        return f"Starte die Skill `{schritt['skill']}`"
    return None


# Ein Satz beginnt am Anfang einer Zeile, auch einer späteren (Einrückung zählt dazu; die Muster gelten mit re.M),
# oder nach einem Satzende bzw. einer fetten Überschrift.
SATZ_ANFANG = r"(?P<vor>^[ \t]*|(?<=[.!?:*])\s+)"
SATZ_ENDE = r"(?=\s|$)"
# Titel in „…“, auch mit Komma, Punkt und paarweisen „…“ darin; ohne Anführungszeichen bis zum Satzende.
_TITEL_IN_ZEICHEN = r"„(?:[^„“\n]|„[^„“\n]*“)+“"
_TITEL_FREI = r"[^„“\n]+?"
_GRENZE = r"(?:, höchstens \d+ Runden?)?\."


def satz_mit_punkt(text: str) -> str:
    """Ein Punkt ans Ende, wenn der Text nicht schon mit einem Satzzeichen oder schließenden Anführungszeichen endet
    (wie satzMitPunkt in web/lib/werkstatt/saetze.ts)."""
    return text if re.search(r"[.!?:…“\"]$", text) else f"{text}."


def schleifen_satz(titel: str, grenze: int | None) -> str:
    """'Bei Befund zurück zu „<titel>“, höchstens <n> Runde(n).', ohne Grenze 'Bei Befund zurück zu „<titel>“.'"""
    if grenze is None:
        return f"Bei Befund zurück zu „{titel}“."
    return f"Bei Befund zurück zu „{titel}“, höchstens {grenze} {'Runde' if grenze == 1 else 'Runden'}."


def _schleifen_muster(titel: list[str] | None) -> re.Pattern:
    if titel is None:
        ziel = f"{_TITEL_IN_ZEICHEN}|{_TITEL_FREI}"
    else:
        namen = "|".join(re.escape(t) for t in sorted(titel, key=len, reverse=True)) or "(?!)"
        ziel = f"„(?:{namen})“|(?:{namen})"
    return re.compile(SATZ_ANFANG + f"Bei Befund zurück zu (?:{ziel}){_GRENZE}" + SATZ_ENDE, re.M)


SCHLEIFEN_SATZ = _schleifen_muster(None)


def finde_schleifen_satz(text: str, titel: list[str] | None = None) -> re.Match | None:
    """Erster Schleifen-Satz im Text (mit oder ohne Anführungszeichen, mit oder ohne Grenze); mit titel nur zu einem
    dieser Titel."""
    return (SCHLEIFEN_SATZ if titel is None else _schleifen_muster(titel)).search(text)


def finde_startsatz(text: str, schritt: dict) -> re.Match | None:
    """Erzeugter Startsatz des Ausführenden von schritt: aufruf_kopf, dann ':' (mit Anweisung) oder '.' (ohne)."""
    kopf = aufruf_kopf(schritt)
    if kopf is None:
        return None
    return re.search(SATZ_ANFANG + re.escape(kopf) + r"(?P<zeichen>[:.])" + SATZ_ENDE, text, re.M)


def finde_mensch_satz(text: str, art: str) -> re.Match | None:
    satz = MENSCH_SATZ.get(art)
    return re.search(SATZ_ANFANG + re.escape(satz) + SATZ_ENDE, text, re.M) if satz else None


def _wortfolge(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def mehr_als_mensch_satz(anweisung: str, art: str) -> str:
    """Was die Anweisung eines Mensch-Schritts über den festen Mensch-Satz hinaus sagt: der Satz selbst fällt weg; ist
    der Rest leer oder nur ein Stück des Satzes (andere Satzzeichen, Groß/klein), bleibt nichts (WS-W6)."""
    text = anweisung.strip()
    while (treffer := finde_mensch_satz(text, art)) is not None:
        text = ersetze_satz(text, treffer, None).strip()
    rest = _wortfolge(text)
    if not rest or rest in _wortfolge(MENSCH_SATZ.get(art, "")):
        return ""
    return text


def ersetze_satz(text: str, treffer: re.Match, neu: str | None) -> str:
    """text mit neu an der Stelle des Treffers; ohne neu fällt der Satz samt Leerraum davor weg (am Zeilenanfang
    bleibt die Einrückung, der Rest der Zeile rückt nach; bleibt von der Zeile nichts, fällt sie samt Umbruch weg)."""
    vor = treffer.group("vor")
    anfang = treffer.start()
    rest = text[treffer.end():]
    if neu is not None:
        return text[:anfang] + vor + neu + rest
    if anfang == 0 or text[anfang - 1] == "\n":
        rest = rest.lstrip(" \t")
        if rest and not rest.startswith("\n"):
            return text[:anfang] + vor + rest
        if rest:
            return text[:anfang] + rest[1:]
        return text[:anfang].removesuffix("\n")
    return text[:anfang] + rest


# Gliederung eines Agent-Prompts in sechs Teile (Ticket 20; T1 Folien 25–31, Guide-Urteil). Kopf ist das Frontmatter,
# Auftrag der Text vor der ersten Überschrift; die übrigen vier Teile erkennt atlas/agent_prompt.py nur an diesen
# Überschriften (verglichen ohne Groß/klein, Satzzeichen am Rand und doppelten Leerraum). Scanner und Werkstatt lesen
# das Vokabular nur von hier.
PROMPT_TEILE = ("kopf", "auftrag", "lesen", "regeln", "schritte", "fertig")
PROMPT_UEBERSCHRIFTEN = {
    "lesen": {"de": ("lesen", "zuerst lesen", "vorher lesen", "lies zuerst", "was du liest"),
              "en": ("read", "read first", "read this first", "before you start")},
    "regeln": {"de": ("regeln", "grundregeln", "grenzen", "richtlinien"),
               "en": ("rules", "ground rules", "constraints", "guidelines")},
    "schritte": {"de": ("schritte", "ablauf", "vorgehen", "so gehst du vor", "arbeitsschritte"),
                 "en": ("steps", "workflow", "process", "procedure")},
    "fertig": {"de": ("fertig", "prüfung", "pruefung", "abschluss", "fertig wenn", "wann fertig"),
               "en": ("done", "check", "checks", "definition of done", "when done", "verification")},
}
# Überschrift, mit der die Werkstatt einen fehlenden Teil ergänzt, in der Sprache der übrigen Überschriften.
PROMPT_NEUE_UEBERSCHRIFT = {
    "lesen": {"de": "Lesen", "en": "Read"},
    "regeln": {"de": "Regeln", "en": "Rules"},
    "schritte": {"de": "Schritte", "en": "Steps"},
    "fertig": {"de": "Fertig", "en": "Done"},
}
