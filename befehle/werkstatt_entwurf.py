"""Pipeline-Entwurf atlas-entwurf/0.1: prüfen und in einen Änderungssatz umwandeln (specs/werkstatt.yaml WS-KE).

Der pipeline-planer antwortet auf einen Auftrag der Art entwerfen mit einem Entwurf statt eines Änderungssatzes: eine
neue Skill mit Schritten, die vorhandenen Agenten, die sie nutzt, neue Agenten und Schleifen. Schritte, neue Agenten
und Schleifen haben je eine id; die Seite wählt sie an oder ab. Aus der Auswahl entsteht hier deterministisch ein
Änderungssatz (agent_anlegen je neuem Agenten, dann skill_anlegen), den die Werkstatt wie jeden Satz der KI prüft.

Geprüft wird nur als Daten: Form, Größe, Namen, dass vorhandene Agenten existieren und neue nicht, dass es keine
Pfade gibt (kein Feld dafür, Namen ohne '/'), Schleifen nach hinten mit Grenze. Nur Standardbibliothek.
"""
from __future__ import annotations

import json
import re

import aenderungssatz as az
from ableiten import einzeilig
from vokabular import (MENSCH_SATZ, ROLLEN, SCHRITT_ID, aufruf_kopf, ersetze_satz, finde_mensch_satz, finde_schleifen_satz,
                       satz_mit_punkt, schleifen_satz)

SCHEMA = "atlas-entwurf/0.1"
GROESSE_HOECHSTENS = 64 * 1024
ZUSAMMENFASSUNG_HOECHSTENS = 600
GRUND_HOECHSTENS = 300
SCHRITTE_HOECHSTENS = 50
NEUE_HOECHSTENS = 20
VORHANDENE_HOECHSTENS = 50
SCHLEIFEN_HOECHSTENS = 20
ZU_TEILEN_HOECHSTENS = 50
BESCHREIBUNG_HOECHSTENS = 1000  # Grenzen von skill_anlegen (aenderungssatz)
ERGEBNIS_HOECHSTENS = 50
TEIL_ID = SCHRITT_ID
SKILL_EINTRAG = "_skill"  # Eintrag-id des skill_anlegen; kann keine Teil-id sein (Unterstrich)
ROLLE_WORT = {"planer": "Planer", "umsetzer": "Umsetzer", "pruefer": "Prüfer"}

ZU_GROSS = f"Der Entwurf ist größer als {GROESSE_HOECHSTENS // 1024} KiB."
# Was die Werkstatt aus der Struktur erzeugt (WS-KE3, anweisung_fuer). Schreibt der Planer genau diese Formulierung
# trotzdem, fällt sie weg, sonst stünde sie doppelt in der SKILL.md: der Aufruf-Vorsatz des eigenen Agenten bzw. der
# eigenen Parallelgruppe am Anfang und ganze Sätze „Bei Befund zurück zu …, höchstens n Runde(n).“ und der Mensch-Satz
# (erkannt wie im Schreiber, vokabular). Alles andere bleibt unangetastet, auch Skill-Aufrufe und „bei Befund“ mitten
# im Satz.


KEIN_SCHRITT = "Wähle mindestens einen Schritt; ohne Schritt entsteht keine Pipeline."


def _liste(p: az._Pruefer, ort: str, wert: object, hoechstens: int, mindestens: int = 0) -> list:
    if not isinstance(wert, list) or not mindestens <= len(wert) <= hoechstens:
        p.fehler(ort, f"erwartet eine Liste mit {mindestens} bis {hoechstens} Einträgen")
        return []
    return wert


def _form(entwurf: object) -> list[str]:
    p = az._Pruefer()
    e = p.felder("Entwurf", entwurf, ("schema", "zusammenfassung", "skill", "schritte"),
                 ("vorhandene", "neue_agenten", "schleifen"))
    if e is None:
        return p.meldungen
    if e.get("schema") != SCHEMA:
        p.fehler("Entwurf.schema", f"erwartet {SCHEMA}")
    p.zeile("Entwurf.zusammenfassung", e.get("zusammenfassung"), ZUSAMMENFASSUNG_HOECHSTENS)
    skill = p.felder("skill", e.get("skill"), ("name", "beschreibung", "nutzen"), ("eingabe", "ergebnis", "zu_teilen"))
    if skill is not None:
        p.muster("skill.name", skill.get("name"), az.NAME, "Name")
        p.zeile("skill.beschreibung", skill.get("beschreibung"), BESCHREIBUNG_HOECHSTENS)
        p.zeile("skill.nutzen", skill.get("nutzen"), 300)
        for feld in ("eingabe", "ergebnis"):
            if feld in skill:
                p.liste_von_zeilen(f"skill.{feld}", skill[feld])
        # Was nur mit bestimmten Teilen stimmt, steht hier; die Übernahme nimmt es nur mit, wenn alle diese Teile
        # gewählt sind (Ticket 18, Probelauf: abgewählte Ladezeit-Prüfung stand weiter in Beschreibung und Ergebnis).
        zusatz_laenge = 0
        ergebnisse = len(skill["ergebnis"]) if isinstance(skill.get("ergebnis"), list) else 0
        for i, z in enumerate(_liste(p, "skill.zu_teilen", skill.get("zu_teilen", []), ZU_TEILEN_HOECHSTENS)):
            ort = f"skill.zu_teilen[{i + 1}]"
            o = p.felder(ort, z, ("teile",), ("beschreibung", "ergebnis"))
            if o is None:
                continue
            for j, t in enumerate(_liste(p, f"{ort}.teile", o.get("teile"), 20, 1)):
                p.muster(f"{ort}.teile[{j + 1}]", t, TEIL_ID, "Teil-id")
            if "beschreibung" not in o and "ergebnis" not in o:
                p.fehler(ort, "braucht beschreibung oder ergebnis")
            if "beschreibung" in o and p.zeile(f"{ort}.beschreibung", o["beschreibung"], 300):
                zusatz_laenge += len(o["beschreibung"]) + 1
            if "ergebnis" in o and p.liste_von_zeilen(f"{ort}.ergebnis", o["ergebnis"]):
                ergebnisse += len(o["ergebnis"])
        # Gemessen wie skill_angaben zusammensetzt (mit dem Punkt, den die Grundbeschreibung dort bekommen kann),
        # damit eine angenommene Auswahl nie an den Grenzen von skill_anlegen scheitert.
        if isinstance(skill.get("beschreibung"), str) and zusatz_laenge \
                and len(satz_mit_punkt(skill["beschreibung"])) + zusatz_laenge > BESCHREIBUNG_HOECHSTENS:
            p.fehler("skill.zu_teilen", f"Beschreibung und alle Zusätze zusammen höchstens {BESCHREIBUNG_HOECHSTENS} "
                                        f"Zeichen")
        if ergebnisse > ERGEBNIS_HOECHSTENS:
            p.fehler("skill.zu_teilen", f"ergebnis und alle Zusätze zusammen höchstens {ERGEBNIS_HOECHSTENS} Einträge")
    for i, v in enumerate(_liste(p, "vorhandene", e.get("vorhandene", []), VORHANDENE_HOECHSTENS)):
        ort = f"vorhandene[{i + 1}]"
        o = p.felder(ort, v, ("name", "grund"), ())
        if o is not None:
            p.muster(f"{ort}.name", o.get("name"), az.NAME, "Name")
            p.zeile(f"{ort}.grund", o.get("grund"), GRUND_HOECHSTENS)
    for i, a in enumerate(_liste(p, "neue_agenten", e.get("neue_agenten", []), NEUE_HOECHSTENS)):
        ort = f"neue_agenten[{i + 1}]"
        o = p.felder(ort, a, ("id", "name", "beschreibung", "rolle", "tools", "text"), ("grund",))
        if o is None:
            continue
        p.muster(f"{ort}.id", o.get("id"), TEIL_ID, "Teil-id")
        p.muster(f"{ort}.name", o.get("name"), az.NAME, "Name")
        p.auswahl(f"{ort}.rolle", o.get("rolle"), ROLLEN)
        if not isinstance(o.get("tools"), list) or not o.get("tools"):
            p.fehler(f"{ort}.tools", "erwartet eine Liste mit mindestens einem Werkzeug")
        az._agent_felder(p, ort, {k: o[k] for k in ("beschreibung", "tools", "text") if k in o}, anlegen=True)
        if isinstance(o.get("text"), str) and not o["text"].strip():
            p.fehler(f"{ort}.text", "darf nicht leer sein")
        if "grund" in o:
            p.zeile(f"{ort}.grund", o["grund"], GRUND_HOECHSTENS)
    for i, s in enumerate(_liste(p, "schritte", e.get("schritte"), SCHRITTE_HOECHSTENS, 1)):
        ort = f"schritte[{i + 1}]"
        p.schritt(ort, s, mit_anweisung=True, mit_schleife=False)
        if isinstance(s, dict) and "werkzeug" in s:
            p.fehler(f"{ort}.werkzeug", "ist im Entwurf nicht erlaubt (keine Pfade)")
        if isinstance(s, dict) and isinstance(s.get("anweisung"), str) and not s["anweisung"].strip():
            p.fehler(f"{ort}.anweisung", "darf nicht leer sein")
    for i, s in enumerate(_liste(p, "schleifen", e.get("schleifen", []), SCHLEIFEN_HOECHSTENS)):
        ort = f"schleifen[{i + 1}]"
        o = p.felder(ort, s, ("id", "von", "zurueck", "max"), ("grund",))
        if o is None:
            continue
        p.muster(f"{ort}.id", o.get("id"), TEIL_ID, "Teil-id")
        p.muster(f"{ort}.von", o.get("von"), SCHRITT_ID, "Schritt-id")
        p.muster(f"{ort}.zurueck", o.get("zurueck"), SCHRITT_ID, "Schritt-id")
        if o.get("max") is None:
            p.fehler(f"{ort}.max", "Pflichtfeld max fehlt (jede Schleife braucht eine Grenze)")
        p.grenze(f"{ort}.max", o.get("max"))
        if "grund" in o:
            p.zeile(f"{ort}.grund", o["grund"], GRUND_HOECHSTENS)
    return p.meldungen


def _namen(schritt: dict) -> list[str]:
    return ([schritt["agent"]] if schritt.get("agent") else []) + list(schritt.get("parallel") or [])


def pruefe(entwurf: object, agenten: set[str], skills: set[str]) -> list[str]:
    """Meldungen zum Entwurf; leer, wenn er gültig ist. agenten und skills sind die Namen im Kit."""
    try:
        groesse = len(json.dumps(entwurf, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return ["Der Entwurf ist kein JSON-Objekt."]
    if groesse > GROESSE_HOECHSTENS:
        return [ZU_GROSS]
    meldungen = _form(entwurf)
    if meldungen:
        return meldungen
    e: dict = entwurf  # type: ignore[assignment]
    name = e["skill"]["name"]
    if name in skills:
        meldungen.append(f"Die Skill {name} gibt es im Kit schon; wähle einen neuen Namen.")
    schritte, neue = e["schritte"], e.get("neue_agenten", [])
    ids = [s["id"] for s in schritte] + [a["id"] for a in neue] + [s["id"] for s in e.get("schleifen", [])]
    for doppelt in sorted({i for i in ids if ids.count(i) > 1}):
        meldungen.append(f"Die id {doppelt} kommt im Entwurf mehrfach vor; jede id gilt für genau einen Teil.")
    neue_namen = [a["name"] for a in neue]
    for a in neue:
        if a["name"] in agenten:
            meldungen.append(f"Den Agenten {a['name']} gibt es im Kit schon; nimm ihn unter vorhandene oder wähle "
                             f"einen neuen Namen.")
        elif neue_namen.count(a["name"]) > 1:
            meldungen.append(f"Der neue Agent {a['name']} steht mehrfach im Entwurf.")
    vorhandene = [v["name"] for v in e.get("vorhandene", [])]
    for v in sorted(set(vorhandene)):
        if v not in agenten:
            meldungen.append(f"Den vorhandenen Agenten {v} gibt es im Kit nicht.")
        elif vorhandene.count(v) > 1:
            meldungen.append(f"Der vorhandene Agent {v} steht mehrfach im Entwurf.")
    genutzt: set[str] = set()
    for s in schritte:
        for n in _namen(s):
            genutzt.add(n)
            if n in neue_namen:
                continue
            if n not in agenten:
                meldungen.append(f"Schritt „{s['titel']}“ nennt den Agenten {n}, den es im Kit nicht gibt und der "
                                 f"nicht neu angelegt wird.")
            elif n not in vorhandene:
                meldungen.append(f"Schritt „{s['titel']}“ nutzt den vorhandenen Agenten {n}; nenne ihn unter "
                                 f"vorhandene mit Grund.")
        if s.get("skill") and (s["skill"] == name or s["skill"] not in skills):
            meldungen.append(f"Schritt „{s['titel']}“ startet die Skill {s['skill']}, die es im Kit nicht gibt.")
    for v in sorted(set(vorhandene) - genutzt):
        if v in agenten:
            meldungen.append(f"Der vorhandene Agent {v} wird von keinem Schritt genutzt.")
    for z in e["skill"].get("zu_teilen", []):
        for t in z["teile"]:
            if t not in ids:
                meldungen.append(f"skill.zu_teilen nennt den Teil {t}, den es im Entwurf nicht gibt.")
    reihe = [s["id"] for s in schritte]
    von_gesehen: set[str] = set()
    for sl in e.get("schleifen", []):
        if sl["von"] not in reihe or sl["zurueck"] not in reihe:
            meldungen.append(f"Die Schleife {sl['id']} nennt einen Schritt, den es im Entwurf nicht gibt.")
        elif reihe.index(sl["zurueck"]) >= reihe.index(sl["von"]):
            meldungen.append(f"Die Schleife {sl['id']} führt nicht zu einem früheren Schritt zurück.")
        elif sl["von"] in von_gesehen:
            meldungen.append(f"Von Schritt {sl['von']} geht mehr als eine Schleife aus.")
        von_gesehen.add(sl["von"])
    return meldungen


def _aufruf(schritt: dict) -> str | None:
    kopf = aufruf_kopf(schritt)
    return f"{kopf}: " if kopf else None


def bereinige(anweisung: str, schritt: dict, titel: list[str]) -> str:
    """Entfernt aus einer Anweisung genau die Teile, die die Werkstatt selbst erzeugt (WS-KE3)."""
    text = anweisung.strip()
    vorsatz = _aufruf(schritt)
    if vorsatz and text.startswith(vorsatz):
        text = text[len(vorsatz):]
    while (treffer := finde_schleifen_satz(text, titel)) is not None:
        text = ersetze_satz(text, treffer, None)
    while schritt.get("mensch") and (treffer := finde_mensch_satz(text, schritt["mensch"])) is not None:
        text = ersetze_satz(text, treffer, None)
    return text.strip()


def normalisiere(entwurf: dict) -> tuple[dict, list[str]]:
    """Der geprüfte Entwurf mit bereinigten Anweisungen; Meldung, wenn eine Anweisung danach leer ist."""
    neu = json.loads(json.dumps(entwurf))
    meldungen = []
    for s in neu["schritte"]:
        s["anweisung"] = bereinige(s["anweisung"], s, [x["titel"] for x in neu["schritte"]])
        if not s["anweisung"]:
            meldungen.append(f"Schritt „{s['titel']}“: Die Anweisung beschreibt nur Aufruf, Schleife oder Mensch-Satz; "
                             f"beschreib die Arbeit des Schritts, den Rest ergänzt die Werkstatt.")
    return neu, meldungen


def anweisung_fuer(schritt: dict, schleife: dict | None, titel: dict[str, str]) -> str:
    """Anweisung in der SKILL.md wie die Leinwand (anweisungFuer): Aufruf, Arbeit, Mensch-Satz, Schleifen-Satz."""
    tun = satz_mit_punkt(schritt["anweisung"].strip())
    text = f"{_aufruf(schritt) or ''}{tun}"
    if schritt.get("mensch"):
        text += f" {MENSCH_SATZ[schritt['mensch']]}"
    if schleife:
        text += f" {schleifen_satz(titel[schleife['zurueck']], schleife['max'])}"
    return text


def _erwaehnt(text: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", text) is not None


def teil_ids(entwurf: dict) -> list[str]:
    """Alle wählbaren Teile in der Reihenfolge des Entwurfs: neue Agenten, Schritte, Schleifen (auch teile je Fassung
    in der öffentlichen Form, WS-KE4, für die Markierung nach WS-KE11)."""
    return ([a["id"] for a in entwurf.get("neue_agenten", [])] + [s["id"] for s in entwurf["schritte"]]
            + [s["id"] for s in entwurf.get("schleifen", [])])


def _kurz(text: str, hoechstens: int = 300) -> str:
    return text if len(text) <= hoechstens else text[:hoechstens - 1] + "…"


def skill_angaben(entwurf: dict, gewaehlt: set[str]) -> dict:
    """Beschreibung und Ergebnis der neuen Skill aus den gewählten Teilen: die Grundangaben und jeder Zusatz aus
    zu_teilen, dessen Teile alle gewählt sind."""
    skill = entwurf["skill"]
    zusaetze = [z for z in skill.get("zu_teilen", []) if all(t in gewaehlt for t in z["teile"])]
    saetze = [z["beschreibung"] for z in zusaetze if z.get("beschreibung")]
    beschreibung = " ".join([satz_mit_punkt(skill["beschreibung"]), *saetze]) if saetze else skill["beschreibung"]
    ergebnis = [*skill.get("ergebnis", []), *(e for z in zusaetze for e in z.get("ergebnis", []))]
    return {"beschreibung": beschreibung, "nutzen": skill["nutzen"], "eingabe": list(skill.get("eingabe", [])),
            "ergebnis": ergebnis}


def _texte_mit_abgewaehltem(entwurf: dict, gewaehlt: set[str], angaben: dict) -> list[str]:
    """Nennt ein behaltener Text der Skill (Beschreibung, Nutzen, Ergebnis) einen abgewählten Teil ganzwortig: den Namen
    eines abgewählten neuen Agenten, eines Agenten, den nur abgewählte Schritte nutzen, oder den Titel eines
    abgewählten Schritts?"""
    schritte = entwurf["schritte"]
    gewaehlte_namen = {n for s in schritte if s["id"] in gewaehlt for n in _namen(s)}
    namen = {a["name"] for a in entwurf.get("neue_agenten", []) if a["id"] not in gewaehlt}
    namen |= {n for s in schritte if s["id"] not in gewaehlt for n in _namen(s)} - gewaehlte_namen
    titel = [s["titel"] for s in schritte if s["id"] not in gewaehlt]
    texte = [("Die Beschreibung", angaben["beschreibung"]), ("Der Nutzen", angaben["nutzen"]),
             *((f"Das Ergebnis „{_kurz(e, 60)}“", e) for e in angaben["ergebnis"])]
    meldungen = []
    for wo, text in texte:
        for x in sorted(namen):
            if _erwaehnt(text, x):
                meldungen.append(f"{wo} der neuen Skill erwähnt den abgewählten Teil {x}; wähle ihn mit an oder bitte "
                                 f"den Planer im Chat, die Angaben umzuschreiben.")
        for x in titel:
            if re.search(rf"(?<![\w-]){re.escape(x)}(?![\w-])", text, re.I):
                meldungen.append(f"{wo} der neuen Skill erwähnt den abgewählten Schritt „{x}“; wähle ihn mit an oder "
                                 f"bitte den Planer im Chat, die Angaben umzuschreiben.")
    return meldungen


ANDERE_HOECHSTENS = 3


def memory_beschreibung(agent: str, andere: list[str]) -> str:
    satz = f"Memory für {agent} einschalten (Projekt)."
    if not andere:
        return satz
    namen = sorted(andere)
    liste = ", ".join(namen) if len(namen) <= ANDERE_HOECHSTENS else \
        f"{', '.join(namen[:ANDERE_HOECHSTENS])} und {len(namen) - ANDERE_HOECHSTENS} weiteren"
    return f"{satz} {agent} arbeitet auch in {liste}; das Memory gilt dort ebenso."


def zu_satz(entwurf: dict, teile: list[str],
            memory: dict[str, tuple[str | None, list[str]]] | None = None) -> tuple[dict | None, list[str]]:
    """Änderungssatz aus einem geprüften Entwurf und der Auswahl der Teile; (None, Meldungen), wenn die Auswahl nicht
    zusammenpasst (fehlende Abhängigkeiten werden alle genannt)."""
    bekannt = teil_ids(entwurf)
    unbekannt = [t for t in teile if t not in bekannt]
    meldungen = ([f"Die Auswahl nennt Teile, die es im Entwurf nicht gibt: {', '.join(sorted(set(unbekannt)))}."]
                 if unbekannt else [])
    gewaehlt = set(teile)
    neue = {a["name"]: a for a in entwurf.get("neue_agenten", [])}
    schritte = [s for s in entwurf["schritte"] if s["id"] in gewaehlt]
    titel = {s["id"]: s["titel"] for s in entwurf["schritte"]}
    if not schritte:
        meldungen.append(KEIN_SCHRITT)
    for s in schritte:
        for n in _namen(s):
            if n in neue and neue[n]["id"] not in gewaehlt:
                meldungen.append(f"Schritt „{s['titel']}“ braucht den neuen Agenten {n}; wähle ihn mit an oder den "
                                 f"Schritt ab.")
        for n, a in neue.items():
            if a["id"] not in gewaehlt and n not in _namen(s) and _erwaehnt(s["anweisung"], n):
                meldungen.append(f"Schritt „{s['titel']}“ erwähnt den abgewählten Agenten {n}; wähle ihn mit an oder "
                                 f"den Schritt ab.")
    schleifen = {}
    for sl in entwurf.get("schleifen", []):
        if sl["id"] not in gewaehlt:
            # Eine in eigenen Worten beschriebene Schleife bliebe nach dem Abwählen im Text stehen
            von = next((s for s in schritte if s["id"] == sl["von"]), None)
            if von and sl["zurueck"] in gewaehlt and _erwaehnt(von["anweisung"], titel[sl["zurueck"]]) \
                    and re.search(r"zurück", von["anweisung"], re.I):
                meldungen.append(f"Schritt „{von['titel']}“ beschreibt die abgewählte Schleife zurück zu "
                                 f"„{titel[sl['zurueck']]}“; wähle sie mit an oder den Schritt ab.")
            continue
        fehlen = [titel[x] for x in (sl["von"], sl["zurueck"]) if x not in gewaehlt]
        if fehlen:
            meldungen.append(f"Die Schleife von „{titel[sl['von']]}“ zurück zu „{titel[sl['zurueck']]}“ braucht beide "
                             f"Schritte; „{'“ und „'.join(fehlen)}“ ist abgewählt.")
            continue
        schleifen[sl["von"]] = {"zurueck": sl["zurueck"], "max": sl["max"]}
    angaben = skill_angaben(entwurf, gewaehlt)
    meldungen += _texte_mit_abgewaehltem(entwurf, gewaehlt, angaben)
    if meldungen:
        return None, meldungen
    eintraege = []
    for a in entwurf.get("neue_agenten", []):
        if a["id"] not in gewaehlt:
            continue
        eintraege.append({"id": a["id"],
                          # die Beschreibung des Agenten darf mehrzeilig sein, die des Eintrags ist eine Zeile
                          "beschreibung": einzeilig(f"Neuer Agent {a['name']} ({ROLLE_WORT[a['rolle']]}): "
                                                    f"{a['beschreibung']}"),
                          "agent_anlegen": {"name": a["name"], "beschreibung": a["beschreibung"], "rolle": a["rolle"],
                                            "tools": list(a["tools"]), "text": a["text"],
                                            **({"memory": "project"} if memory is not None else {})}})
    if memory is not None:
        vorhandene: list[str] = []
        for s in schritte:
            for n in _namen(s):
                if n not in neue and n not in vorhandene and n in memory and memory[n][0] is None:
                    vorhandene.append(n)
        for i, n in enumerate(vorhandene, 1):
            eintraege.append({"id": f"_m{i}", "beschreibung": memory_beschreibung(n, memory[n][1]),
                              "agent_aendern": {"name": n, "memory": "project"}})
    skill = entwurf["skill"]
    neue_schritte = []
    for s in schritte:
        schritt = {"id": s["id"], "titel": s["titel"]}
        for feld in ("agent", "parallel", "skill", "mensch"):
            if s.get(feld) is not None:
                schritt[feld] = s[feld]
        if s["id"] in schleifen:
            schritt["bei_befund"] = schleifen[s["id"]]
        schritt["anweisung"] = anweisung_fuer(s, schleifen.get(s["id"]), titel)
        neue_schritte.append(schritt)
    anlegen = {"name": skill["name"], "beschreibung": angaben["beschreibung"], "nutzen": angaben["nutzen"],
               **{f: angaben[f] for f in ("eingabe", "ergebnis") if angaben[f]}, "schritte": neue_schritte}
    n = len(neue_schritte)
    eintraege.append({"id": SKILL_EINTRAG,
                      "beschreibung": f"Neue Skill {skill['name']} mit {n} {'Schritt' if n == 1 else 'Schritten'}.",
                      "skill_anlegen": anlegen})
    return {"schema": az.SCHEMA, "titel": _kurz(f"Neue Pipeline {skill['name']}", 120), "herkunft": "ki",
            "eintraege": eintraege}, []
