"""Hook-Knoten aus hooks/hooks.json (Projekt: .claude/settings.json), bewacht und pfade aus dem
Docstring bzw. Kommentarkopf, bewacht-Kanten."""
from __future__ import annotations

import json
import posixpath
import re
from collections import Counter
from pathlib import Path

from befunde import Befunde
from docstring import Docstring, erster_absatz, modul_docstring, ohne_schlusspunkt
from kommentarkopf import kommentarkopf
from ordner import Ordner

SKRIPT = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"'\s]+)")
SKRIPT_IM_PROJEKT = re.compile(r"\$\{?CLAUDE_PROJECT_DIR\}?/(\S+)")
WURZEL_IM_COMMAND = {"plugin": "${CLAUDE_PLUGIN_ROOT}/", "projekt": "$CLAUDE_PROJECT_DIR/"}
ORT = {"plugin": "im Kit", "projekt": "im Projekt"}
PRAEFIX = re.compile(r"^([A-Za-z]+): ")
SATZZEICHEN = ",.;:)!?"
# Hook-Ereignisse von Claude Code; dazu kommen die Schlüssel aus hooks.json.
EREIGNISSE = {
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "Notification", "UserPromptSubmit",
    "Stop", "SubagentStart", "SubagentStop", "PreCompact", "SessionStart", "SessionEnd",
    "PermissionRequest",
}


# ---------------------------------------------------------------- hooks.json

def _struktur_ok(daten) -> bool:
    if not isinstance(daten, dict) or not isinstance(daten.get("hooks"), dict):
        return False
    for gruppen in daten["hooks"].values():
        if not isinstance(gruppen, list):
            return False
        for gruppe in gruppen:
            if not isinstance(gruppe, dict) or not isinstance(gruppe.get("hooks"), list):
                return False
            if not all(isinstance(h, dict) for h in gruppe["hooks"]):
                return False
    return True


def _lies_hooks_json(quelle: Ordner, befunde: Befunde) -> dict | None:
    """SC-H5, SC-M6: fehlt die Datei (im Projekt auch der Schlüssel hooks) -> None ohne Befund;
    kaputt -> None mit Befund nicht_lesbar."""
    datei = quelle.hooks
    pfad = quelle.wurzel / datei
    if not pfad.is_file():
        return None
    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        befunde.melde("nicht_lesbar", f"{datei} ist nicht als UTF-8 lesbar; keine Hooks",
                      datei, None)
        return None
    except json.JSONDecodeError as e:
        befunde.melde("nicht_lesbar", f"{datei} ist kein gültiges JSON ({e.msg}); keine Hooks",
                      datei, e.lineno)
        return None
    if quelle.ist_projekt and isinstance(daten, dict) and "hooks" not in daten:
        return None
    if not _struktur_ok(daten):
        befunde.melde("nicht_lesbar",
                      f"{datei} hat nicht die erwartete Form "
                      f"{{\"hooks\": {{<Ereignis>: [{{\"hooks\": [...]}}]}}}}; keine Hooks",
                      datei, None)
        return None
    return daten


def _skript(command, quelle: Ordner) -> str | None:
    if not isinstance(command, str):
        return None
    if quelle.ist_projekt:
        m = SKRIPT_IM_PROJEKT.search(command.replace('"', "").replace("'", ""))
    else:
        m = SKRIPT.search(command)
    return posixpath.normpath(m.group(1)) if m else None


def _eintraege(daten: dict, quelle: Ordner, befunde: Befunde) -> list[dict]:
    """Je command-Hook ein Eintrag {ereignis, matcher, skript, name} in Dateireihenfolge."""
    datei = quelle.hooks
    eintraege = []
    for ereignis, gruppen in daten["hooks"].items():
        for gruppe in gruppen:
            matcher = gruppe.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                befunde.melde("nicht_unterstuetzt",
                              f"matcher unter {ereignis} ist kein Text ({matcher!r}); wird null",
                              datei, None)
                matcher = None
            for h in gruppe["hooks"]:
                if h.get("type") != "command":
                    befunde.melde("nicht_unterstuetzt",
                                  f"Hook unter {ereignis} hat type {h.get('type')}; gelesen wird "
                                  f"nur type command, kein Knoten",
                                  datei, None)
                    continue
                command = h.get("command")
                skript = _skript(command, quelle)
                if skript is None:
                    befunde.melde("nicht_unterstuetzt",
                                  f"Hook unter {ereignis}: command \"{command}\" nennt kein Skript "
                                  f"unter {WURZEL_IM_COMMAND[quelle.art]}; Knoten ohne skript und bewacht",
                                  datei, None)
                name = posixpath.splitext(posixpath.basename(skript))[0] if skript else ereignis
                eintraege.append({"ereignis": ereignis, "matcher": matcher,
                                  "skript": skript, "name": name})
    return eintraege


def _ids(eintraege: list[dict]) -> list[str]:
    """SC-H3: hook:<name>; mehrfach -> @<Ereignis>; mehrfach im selben Ereignis -> -<n>."""
    je_name = Counter(e["name"] for e in eintraege)
    je_ereignis = Counter((e["name"], e["ereignis"]) for e in eintraege)
    laufend: Counter = Counter()
    ids = []
    for e in eintraege:
        schluessel = (e["name"], e["ereignis"])
        if je_name[e["name"]] == 1:
            ids.append(f"hook:{e['name']}")
        elif je_ereignis[schluessel] == 1:
            ids.append(f"hook:{e['name']}@{e['ereignis']}")
        else:
            laufend[schluessel] += 1
            ids.append(f"hook:{e['name']}@{e['ereignis']}-{laufend[schluessel]}")
    return ids


# ---------------------------------------------------------------- Docstring

def pfade_im_docstring(doc: Docstring) -> list[tuple[str, int, str]]:
    """SC-H8: (pfad, zeile, zitat) je Pfad, erste Fundstelle, in Docstring-Reihenfolge."""
    ergebnis: list[tuple[str, int, str]] = []
    gesehen: set[str] = set()
    for nr, text in doc.zeilen:
        for wort in text.split():
            wort = wort.rstrip(SATZZEICHEN)
            erster = wort.split("/", 1)[0]
            if "/" not in wort or not erster or not (erster[0].islower() or erster[0].isdigit()):
                continue
            if wort not in gesehen:
                gesehen.add(wort)
                ergebnis.append((wort, nr, text.lstrip()))
    return ergebnis


def _docstring(kit: Path, skript: str, cache: dict, befunde: Befunde) -> Docstring | None:
    """Docstring (SC-H7) bzw. Kommentarkopf (SC-H9) je Skript einmal lesen. Fehlt der Docstring
    eines .py-Skripts: Befund pflichtfeld_fehlt; fehlt ein Kommentarkopf: kein Befund."""
    if skript not in cache:
        if not skript.endswith(".py"):
            cache[skript] = kommentarkopf(kit, skript)
            return cache[skript]
        doc = modul_docstring(kit, skript, befunde)
        if doc is not None and not doc.vorhanden:
            befunde.melde("pflichtfeld_fehlt",
                          f"{skript} hat keinen Modul-Docstring; bewacht bleibt null", skript, None)
        cache[skript] = doc
    return cache[skript]


def _skript_im_kit(kit: Path, skript: str) -> bool:
    return (not posixpath.isabs(skript) and skript != ".." and not skript.startswith("../")
            and (kit / skript).is_file())


def _hook_knoten(quelle: Ordner, e: dict, hook_id: str, ereignisse: set[str], cache: dict,
                 befunde: Befunde) -> tuple[dict, list[dict]]:
    """Ein Hook-Knoten und seine bewacht-Kanten."""
    kit = quelle.wurzel
    k = {"id": hook_id, "typ": "hook", "ereignis": e["ereignis"], "matcher": e["matcher"]}
    skript = e["skript"]
    if skript is None:                                   # SC-H2
        return {**k, "herkunft": "scan", "pfade": []}, []
    k["skript"] = skript
    if not _skript_im_kit(kit, skript):                  # SC-H6
        befunde.melde("verweis_fehlt",
                      f"Hook unter {e['ereignis']} verweist auf {skript}, das es {ORT[quelle.art]} "
                      f"nicht gibt; Knoten ohne bewacht",
                      quelle.hooks, None)
        return {**k, "herkunft": "scan", "pfade": []}, []
    doc = _docstring(kit, skript, cache, befunde)
    erste = doc.erste_zeile() if doc is not None else None
    if erste is None:                                    # SC-H7, SC-H9: kein Docstring bzw. Kopf, nicht lesbar
        return {**k, "bewacht": None, "herkunft": "scan", "pfade": []}, []

    zeile, zitat = erste
    m = PRAEFIX.match(zitat)
    if m and m.group(1) in ereignisse and m.group(1) != e["ereignis"]:
        befunde.melde("widerspruch",
                      f"Der Docstring von {skript} beginnt mit {m.group(1)}, registriert ist "
                      f"das Skript unter {e['ereignis']}",
                      skript, zeile)
    bewacht = erster_absatz(doc)
    if bewacht.startswith(f"{e['ereignis']}: "):
        bewacht = bewacht[len(e["ereignis"]) + 2:]
    pfade = pfade_im_docstring(doc)
    k.update({
        "bewacht": ohne_schlusspunkt(bewacht),
        "beleg": {"datei": skript, "zeile": zeile, "zitat": zitat},
        "herkunft": "scan",
        "pfade": [p for p, _, _ in pfade],
    })
    kanten = [{"von": hook_id, "nach": f"pfad:{p}", "typ": "bewacht", "herkunft": "scan",
               "beleg": {"datei": skript, "zeile": nr, "zitat": text}}
              for p, nr, text in pfade]
    return k, kanten


def lies_hooks(quelle: Ordner, befunde: Befunde) -> tuple[list[dict], list[dict]]:
    """Hook-Knoten in Dateireihenfolge und bewacht-Kanten."""
    daten = _lies_hooks_json(quelle, befunde)
    if daten is None:
        return [], []
    eintraege = _eintraege(daten, quelle, befunde)
    ereignisse = EREIGNISSE | set(daten["hooks"])
    cache: dict = {}
    knoten: list[dict] = []
    kanten: list[dict] = []
    for e, hook_id in zip(eintraege, _ids(eintraege)):
        k, ks = _hook_knoten(quelle, e, hook_id, ereignisse, cache, befunde)
        knoten.append(k)
        kanten.extend(ks)
    return knoten, kanten
