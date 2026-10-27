"""Hook-Knoten aus hooks/hooks.json (Projekt: .claude/settings.json), bewacht und pfade aus dem
Docstring bzw. Kommentarkopf, bewacht-Kanten; Modul-Hooks liest hook_module."""
from __future__ import annotations

import json
import posixpath
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import hook_module
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
EREIGNISSE = (
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied", "Notification",
    "UserPromptSubmit", "UserPromptExpansion", "Stop", "StopFailure", "SubagentStart", "SubagentStop",
    "PreCompact", "PostCompact", "SessionStart", "SessionEnd", "Setup", "TaskCreated", "TaskCompleted",
    "TeammateIdle", "Elicitation", "ElicitationResult", "ConfigChange", "InstructionsLoaded", "CwdChanged",
    "FileChanged", "WorktreeCreate", "WorktreeRemove",
)
WERKZEUG_EREIGNISSE = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied")
AGENT_EREIGNISSE = ("SubagentStart", "SubagentStop")


# ---------------------------------------------------------------- hooks.json

@dataclass
class GeleseneHookDatei:
    daten: dict
    text: str
    hooks: dict | None
    module: list[str] | None


def hooks_struktur_ok(hooks) -> bool:
    if not isinstance(hooks, dict):
        return False
    for gruppen in hooks.values():
        if not isinstance(gruppen, list):
            return False
        for gruppe in gruppen:
            if not isinstance(gruppe, dict) or not isinstance(gruppe.get("hooks"), list):
                return False
            if not all(isinstance(h, dict) for h in gruppe["hooks"]):
                return False
    return True


def _falsche_form(befunde: Befunde, datei: str) -> None:
    befunde.melde("nicht_lesbar",
                  f"{datei} hat nicht die erwartete Form "
                  f"{{\"hooks\": {{<Ereignis>: [{{\"hooks\": [...]}}]}}}}; keine Hooks",
                  datei, None, aussage="hooks")


def lies_hook_datei(quelle: Ordner, befunde: Befunde) -> GeleseneHookDatei | None:
    """SC-H5, SC-M6: fehlt die Datei (im Projekt auch der Schlüssel hooks) -> None ohne Befund; kaputt -> None mit
    Befund nicht_lesbar. hooks bzw. modules sind None, wenn sie fehlen oder ihre Form nicht stimmt (dann mit Befund)."""
    datei = quelle.hooks
    pfad = quelle.wurzel / datei
    if not pfad.is_file():
        return None
    try:
        text = pfad.read_bytes().decode("utf-8")
        daten = json.loads(text.removeprefix("\ufeff"))
    except UnicodeDecodeError:
        befunde.melde("nicht_lesbar", f"{datei} ist nicht als UTF-8 lesbar; keine Hooks",
                      datei, None, aussage="datei")
        return None
    except json.JSONDecodeError as e:
        befunde.melde("nicht_lesbar", f"{datei} ist kein gültiges JSON ({e.msg}); keine Hooks",
                      datei, e.lineno, aussage="datei")
        return None
    except RecursionError:
        befunde.melde("nicht_lesbar", f"{datei} ist zu tief verschachtelt; keine Hooks", datei, None, aussage="datei")
        return None
    except OSError:
        return None
    if quelle.ist_projekt:
        if isinstance(daten, dict) and "hooks" not in daten:
            return None
        if not isinstance(daten, dict) or not hooks_struktur_ok(daten["hooks"]):
            _falsche_form(befunde, datei)
            return None
        return GeleseneHookDatei(daten, text, daten["hooks"], None)
    if not isinstance(daten, dict) or ("hooks" not in daten and "modules" not in daten):
        _falsche_form(befunde, datei)
        return None
    hooks = None
    if "hooks" in daten:
        if hooks_struktur_ok(daten["hooks"]):
            hooks = daten["hooks"]
        else:
            _falsche_form(befunde, datei)
    module = None
    if "modules" in daten:
        module = hook_module.eintraege(daten)
        if module is None:
            befunde.melde("nicht_lesbar",
                          f"modules in {datei} ist keine Liste von Pfaden; keine Modul-Hooks",
                          datei, None, aussage="modules")
    return GeleseneHookDatei(daten, text, hooks, module)


def skript_aus_command(command, projekt: bool) -> str | None:
    """Das Skript, das ein command startet, relativ zur Wurzel (SC-H2); None ohne."""
    if not isinstance(command, str):
        return None
    if projekt:
        m = SKRIPT_IM_PROJEKT.search(command.replace('"', "").replace("'", ""))
    else:
        m = SKRIPT.search(command)
    return posixpath.normpath(m.group(1)) if m else None


def _eintraege(hooks: dict, quelle: Ordner, befunde: Befunde) -> list[dict]:
    """Je command-Hook ein Eintrag {ereignis, matcher, skript, name} in Dateireihenfolge."""
    datei = quelle.hooks
    eintraege = []
    for ereignis, gruppen in hooks.items():
        for gruppe in gruppen:
            matcher = gruppe.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                befunde.melde("nicht_unterstuetzt",
                              f"matcher unter {ereignis} ist kein Text ({matcher!r}); wird null",
                              datei, None, aussage=f"hooks.{ereignis}.matcher")
                matcher = None
            for h in gruppe["hooks"]:
                if h.get("type") != "command":
                    befunde.melde("nicht_unterstuetzt",
                                  f"Hook unter {ereignis} hat type {h.get('type')}; gelesen wird "
                                  f"nur type command, kein Knoten",
                                  datei, None, aussage=f"hooks.{ereignis}.type", ziel_art="type",
                                  ziel=str(h.get("type")))
                    continue
                command = h.get("command")
                skript = skript_aus_command(command, quelle.ist_projekt)
                if skript is None:
                    befunde.melde("nicht_unterstuetzt",
                                  f"Hook unter {ereignis}: command \"{command}\" nennt kein Skript "
                                  f"unter {WURZEL_IM_COMMAND[quelle.art]}; Knoten ohne skript und bewacht",
                                  datei, None, aussage=f"hooks.{ereignis}.command", ziel_art="command",
                                  ziel=str(command))
                name = posixpath.splitext(posixpath.basename(skript))[0] if skript else ereignis
                eintraege.append({"ereignis": ereignis, "matcher": matcher,
                                  "skript": skript, "name": name})
    return eintraege


def command_ids(eintraege: list[dict]) -> list[str]:
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
                          f"{skript} hat keinen Modul-Docstring; bewacht bleibt null", skript, None,
                          aussage="docstring")
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
                      quelle.hooks, None, aussage=f"hooks.{e['ereignis']}.command", ziel_art="skript", ziel=skript)
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
                      skript, zeile, aussage="docstring", ziel_art="ereignis", ziel=e["ereignis"])
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
    """Hook-Knoten in Dateireihenfolge (erst die Command-Hooks, dann die Modul-Hooks) und bewacht-Kanten."""
    gelesen = lies_hook_datei(quelle, befunde)
    if gelesen is None:
        return [], []
    eintraege = _eintraege(gelesen.hooks, quelle, befunde) if gelesen.hooks is not None else []
    ereignisse = set(EREIGNISSE) | set(gelesen.hooks or {})
    cache: dict = {}
    knoten: list[dict] = []
    kanten: list[dict] = []
    for e, hook_id in zip(eintraege, command_ids(eintraege)):
        k, ks = _hook_knoten(quelle, e, hook_id, ereignisse, cache, befunde)
        knoten.append(k)
        kanten.extend(ks)
    if gelesen.module is not None:
        knoten += hook_module.modul_knoten(quelle, gelesen.daten, gelesen.text, [k["id"] for k in knoten], befunde)
    return knoten, kanten


def skripte(quelle: Ordner) -> list[str]:
    """Hook-Skripte und Moduldateien, die der Scanner liest (im Kit vorhanden), relativ zur Wurzel und sortiert.

    Öffentlich für die Werkstatt: Ihre Probe-Kopie enthält genau das, was der Scanner liest
    (Ordner.dateien() und diese Dateien). Befunde entstehen hier keine.
    """
    gelesen = lies_hook_datei(quelle, Befunde())
    if gelesen is None:
        return []
    gefunden = {e["skript"] for e in (_eintraege(gelesen.hooks, quelle, Befunde()) if gelesen.hooks is not None else [])
                if e.get("skript") and _skript_im_kit(quelle.wurzel, e["skript"])}
    if gelesen.module is not None:
        gefunden |= set(hook_module.moduldateien(quelle, gelesen.daten))
    return sorted(gefunden)
