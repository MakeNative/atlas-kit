"""Gliederung eines Agent-Prompts in sechs Teile (Ticket 20, specs/scanner.yaml SC-PR).

Kopf (Frontmatter), Auftrag (Text vor der ersten Abschnitts-Überschrift), Lesen, Regeln, Schritte und Fertig. Die vier
letzten erkennt die Gliederung nur an Überschriften aus vokabular.PROMPT_UEBERSCHRIFTEN; andere Abschnitte bleiben
unter ihrem eigenen Namen in der Reihenfolge der Datei. Nichts wird erfunden: Fertig gibt es nur mit Überschrift,
sonst höchstens den abgeleiteten Hinweis auf den letzten Schritt mit seiner Zeile. Scanner (Agent-Knoten) und
Werkstatt (GET /api/agent) rufen beide gliedere(); nur Standardbibliothek.
"""
from __future__ import annotations

import datetime
import re

from vokabular import PROMPT_NEUE_UEBERSCHRIFT, PROMPT_TEILE, PROMPT_UEBERSCHRIFTEN

ZAUN = re.compile(r"^\s*(`{3,}|~{3,})")
ZAUN_ZU = re.compile(r"^\s*(`{3,}|~{3,})\s*$")  # schließt nur ohne Info-String (CommonMark, Review r20-6)
# Ohne konkurrierende Quantoren (Review r20-17): der Text wird danach mit ueberschrift_text gekürzt. Gleich in
# web/lib/markdown.ts.
UEBERSCHRIFT = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*))?$")


def ueberschrift_text(roh: str | None) -> str:
    """Text einer Überschrift ohne Leerraum am Rand und ohne schließende '#'-Folge (die nach Leerraum steht)."""
    text = (roh or "").rstrip(" \t")
    ohne = text.rstrip("#")
    if ohne != text and (not ohne or ohne[-1] in " \t"):
        text = ohne.rstrip(" \t")
    return text.strip()
NUMMER = re.compile(r"^(\s*)\d+[.)][ \t]+\S")
DEUTSCH = re.compile(r"[äöüß]|\b(der|die|das|und|du|dein|nicht|mit|für|ist|im|den|dem|ein|eine|zu|von|wenn|oder|auch)\b")
ENGLISCH = re.compile(r"\b(the|and|you|your|of|to|is|with|for|this|that|when|or)\b")
RANG = {teil: i for i, teil in enumerate(PROMPT_TEILE)}
KOPF_ZUERST = ("name", "description", "tools", "disallowedTools", "model", "memory", "skills")


def _norm(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip("*_`").strip()
    return text.rstrip(":.").strip().lower()


_TEIL_ZU = {_norm(w): (teil, sprache) for teil, je in PROMPT_UEBERSCHRIFTEN.items()
            for sprache, woerter in je.items() for w in woerter}


def teil_von(titel: str) -> tuple[str, str] | None:
    """(teil, sprache) einer Überschrift aus dem Vokabular, sonst None."""
    return _TEIL_ZU.get(_norm(titel))


def _ueberschriften(zeilen: list[str], ab: int) -> tuple[list[tuple[int, int, str]], set[int], int | None]:
    """(Zeile, Ebene, Text) jeder Überschrift mit Text außerhalb von Code-Zäunen, die Zeilen in Zäunen und die Zeile
    eines Zauns, der bis zum Ende offen bleibt (sonst None)."""
    koepfe, im_code = [], set()
    zaun = None
    zaun_ab = None
    for nr in range(ab, len(zeilen) + 1):
        z = zeilen[nr - 1]
        if zaun is not None:
            im_code.add(nr)
            m = ZAUN_ZU.match(z)
            if m and m.group(1)[0] == zaun[0] and len(m.group(1)) >= len(zaun):
                zaun = None
            continue
        m = ZAUN.match(z)
        if m:
            zaun = m.group(1)
            zaun_ab = nr
            im_code.add(nr)
            continue
        h = UEBERSCHRIFT.match(z)
        text = ueberschrift_text(h.group(2)) if h else ""
        if h and text.strip("#").strip():
            koepfe.append((nr, len(h.group(1)), text))
    return koepfe, im_code, (zaun_ab if zaun is not None else None)


def _sprache(teile_titel: list[str], probe: list[str]) -> str:
    zaehler = {"de": 0, "en": 0}
    for titel in teile_titel:
        treffer = teil_von(titel)
        if treffer:
            zaehler[treffer[1]] += 1
    if zaehler["de"] or zaehler["en"]:
        return "en" if zaehler["en"] > zaehler["de"] else "de"
    text = " ".join(probe).lower()
    en, de = len(ENGLISCH.findall(text)), len(DEUTSCH.findall(text))
    if en > de:
        return "en"
    return "de" if de or not re.search(r"[a-z]", text) else "en"


def _fertig_hinweis(zeilen: list[str], teil: dict, im_code: set[int]) -> dict | None:
    """Letzter nummerierter Schritt (geringste Einrückung) im Abschnitt Schritte, vor dessen erster Unterüberschrift
    (eine nummerierte Liste unter '### Beispiel' ist kein Schritt, Review r20-9)."""
    treffer = []
    for nr in range(teil["zeile"] + 1, teil["bis"] + 1):
        if nr in im_code:
            continue
        if UEBERSCHRIFT.match(zeilen[nr - 1]):
            break
        m = NUMMER.match(zeilen[nr - 1])
        if m:
            treffer.append((len(m.group(1).expandtabs(4)), nr))
    if not treffer:
        return None
    tiefe = min(t for t, _ in treffer)
    nr = [n for t, n in treffer if t == tiefe][-1]
    return {"zeile": nr, "zitat": zeilen[nr - 1].strip()}


def gliedere(zeilen: list[str], ab: int = 1) -> dict:
    """Gliederung der Zeilen ab Zeile ab (1-basiert, erste Zeile unter dem Frontmatter).

    teile: je Abschnitt {teil (auftrag, lesen, regeln, schritte, fertig oder None = eigener Abschnitt), titel (Text der
    Überschrift, beim Auftrag None), zeile, bis} in Dateireihenfolge; ein Abschnitt mit Überschrift beginnt mit ihr und
    reicht bis vor die nächste Abschnitts-Überschrift. fehlt: je fehlendem Teil {teil, vor (Index in teile, vor dem er
    stünde; None = am Ende), ueberschrift (mit der die Werkstatt ihn ergänzt; beim Auftrag None)}. fertig_hinweis
    {zeile, zitat} nur ohne Fertig, aber mit nummeriertem Schritt.
    """
    koepfe, im_code, offener_zaun = _ueberschriften(zeilen, ab)
    erste = next((nr for nr in range(ab, len(zeilen) + 1) if zeilen[nr - 1].strip()), None)
    # Ein Titel über allem (# Name vor tieferen Abschnitten, einzig auf seiner Ebene) gehört zum Auftrag und zählt
    # weder für die Abschnitts-Ebene noch als Abschnitt (Review r20-1).
    titel = koepfe[0] if koepfe and koepfe[0][0] == erste else None
    if (titel and not teil_von(titel[2]) and sum(1 for k in koepfe if k[1] <= titel[1]) == 1
            and any(k[1] > titel[1] for k in koepfe)):
        koepfe = koepfe[1:]
    erkannt = [k for k in koepfe if teil_von(k[2])]
    ebene = min(k[1] for k in erkannt) if erkannt else (min(k[1] for k in koepfe) if koepfe else 2)
    trenner = [k for k in koepfe if k[1] <= ebene]
    # Ein erkannter Teil, der nur als Unterüberschrift dasteht (### Done unter ## Steps), ist trotzdem dieser Teil:
    # seine erste Fundstelle beginnt einen Abschnitt (Review r20-8).
    oben = {teil_von(k[2])[0] for k in trenner if teil_von(k[2])}
    for k in koepfe:
        treffer = teil_von(k[2])
        if k[1] > ebene and treffer and treffer[0] not in oben:
            oben.add(treffer[0])
            trenner.append(k)
    trenner.sort()
    ende = len(zeilen)
    teile: list[dict] = []
    vor_erstem = trenner[0][0] if trenner else ende + 1
    if erste is not None and erste < vor_erstem:
        teile.append({"teil": "auftrag", "titel": None, "zeile": erste, "bis": vor_erstem - 1})
    for i, (nr, _, titel) in enumerate(trenner):
        bis = trenner[i + 1][0] - 1 if i + 1 < len(trenner) else ende
        treffer = teil_von(titel)
        teile.append({"teil": treffer[0] if treffer else None, "titel": titel, "zeile": nr, "bis": bis})

    probe = [k[2] for k in trenner]
    if teile and teile[0]["teil"] == "auftrag":
        probe += zeilen[teile[0]["zeile"] - 1:teile[0]["bis"]]
    sprache = _sprache([k[2] for k in trenner], probe)
    vorhanden = {t["teil"] for t in teile}
    fehlt = []
    for teil in PROMPT_TEILE[1:]:
        if teil in vorhanden:
            continue
        if teil == "auftrag":
            vor = 0 if teile else None
        else:
            vor = next((i for i, t in enumerate(teile) if t["teil"] and RANG[t["teil"]] > RANG[teil]), None)
        ueberschrift = None if teil == "auftrag" else f"{'#' * ebene} {PROMPT_NEUE_UEBERSCHRIFT[teil][sprache]}"
        fehlt.append({"teil": teil, "vor": vor, "ueberschrift": ueberschrift})
    ergebnis = {"teile": teile, "fehlt": fehlt, "ebene": ebene}
    if offener_zaun is not None:
        # Verschachtelt geschriebene Zäune gleicher Länge zerfallen nach CommonMark; was nach dem letzten offenen Zaun
        # steht, ist Code bis zum Ende (Review r20-14). Die Seite sagt das an.
        ergebnis["offener_zaun"] = offener_zaun
    schritte = [t for t in teile if t["teil"] == "schritte"]
    if "fertig" not in vorhanden and schritte:
        hinweis = _fertig_hinweis(zeilen, schritte[-1], im_code)
        if hinweis is not None:
            ergebnis["fertig_hinweis"] = hinweis
    return ergebnis


def _innen(wert: object) -> str:
    if isinstance(wert, dict):
        return "{" + "; ".join(f"{k}: {_innen(v)}" for k, v in wert.items()) + "}"
    if isinstance(wert, list):
        return "[" + ", ".join(_innen(w) for w in wert) + "]"
    return kopf_wert(wert)


def kopf_wert(wert: object) -> str:
    """Wert eines Frontmatter-Felds als eine Zeile für die Kopf-Tabelle: Liste 'a, b', Abbildung 'k: v; k2: [a, b]'."""
    if wert is None:
        return ""
    if isinstance(wert, bool):
        return "true" if wert else "false"
    if isinstance(wert, (datetime.date, datetime.datetime)):
        return wert.isoformat()
    if isinstance(wert, list):
        return ", ".join(_innen(w) for w in wert)
    if isinstance(wert, dict):
        return "; ".join(f"{k}: {_innen(v)}" for k, v in wert.items())
    return " ".join(str(wert).split())  # eine Zeile, auch bei Block-Skalaren (Review r20-11)


def kopf(werte: dict, zeile_von) -> list[dict]:
    """Zeilen der Kopf-Tabelle: name, description, tools, disallowedTools, model, memory, skills, dann die übrigen
    Felder in der Reihenfolge der Datei. zeile_von(schluessel) nennt die Zeile des Felds."""
    folge = [s for s in KOPF_ZUERST if s in werte] + [s for s in werte if s not in KOPF_ZUERST]
    return [{"feld": str(s), "wert": kopf_wert(werte[s]), "zeile": zeile_von(s)} for s in folge]
