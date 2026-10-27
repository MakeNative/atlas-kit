"""Modul-Hooks (SC-H10 bis SC-H13): je Eintrag in modules ein Knoten mit bewacht und den Ereignissen aus den
on('<ereignis>'…)-Zeilen. Gelesen wird nur Text; nichts wird ausgeführt, keinem import wird gefolgt."""
from __future__ import annotations

import os
import posixpath
import re
import stat
from collections import Counter
from pathlib import Path

import json_spannen
from befunde import Befunde
from docstring import Docstring, erster_absatz, ohne_schlusspunkt
from ordner import Ordner
from textdatei import BOM, zerlege

ENDUNGEN = (".ts", ".mts", ".cts", ".js", ".mjs", ".cjs")
HOECHSTENS = 1024 * 1024
AUFRUF = re.compile(r"(?<![A-Za-z0-9_$.])on\((['\"])([A-Za-z0-9._-]+)\1")
OBJEKT_ALS_ZWEITES = re.compile(r"\s*,\s*\{")
LEERRAUM = re.compile(r"\s+")
TEXT_ZEICHEN = "'\"`"


def eintraege(daten: dict) -> list[str] | None:
    """modules als Liste nicht leerer Texte; None, wenn die Form nicht stimmt (SC-H5)."""
    module = daten.get("modules")
    if not isinstance(module, list) or not all(isinstance(m, str) and m.strip() for m in module):
        return None
    return module


def aufgeloest(quelle: Ordner, eintrag: str) -> str:
    """Der Eintrag relativ zum Ordner der Hook-Datei, normalisiert (./register.ts in hooks/hooks.json -> hooks/register.ts)."""
    return posixpath.normpath(posixpath.join(posixpath.dirname(quelle.hooks), eintrag))


def im_kit_ohne_symlink(kit: Path, rel: str) -> bool:
    if posixpath.isabs(rel) or rel == ".." or rel.startswith("../"):
        return False
    pfad = kit
    for teil in rel.split("/"):
        pfad = pfad / teil
        if pfad.is_symlink():
            return False
    try:
        pfad.resolve().relative_to(kit.resolve())
    except (OSError, ValueError):
        return False
    return pfad.is_file()


def moduldateien(quelle: Ordner, daten: dict) -> list[str]:
    """Die Moduldateien, die der Scanner liest (im Kit, ohne Symlink, unterstützte Endung), für hooks.skripte."""
    module = eintraege(daten) if not quelle.ist_projekt else None
    gefunden = []
    for eintrag in module or []:
        rel = aufgeloest(quelle, eintrag)
        if rel.endswith(ENDUNGEN) and im_kit_ohne_symlink(quelle.wurzel, rel):
            gefunden.append(rel)
    return gefunden


def _ohne_kommentare(zeilen: list[str]) -> list[str | None]:
    """Je Zeile der Code-Text ohne Kommentare; None für Zeilen, die nach Leerraum mit // beginnen. Ein /* bzw. // in
    einem Text ('…', "…", `…`) beginnt keinen Kommentar; ein Template-Text darf über Zeilen gehen."""
    im_block = False
    im_text: str | None = None
    code: list[str | None] = []
    for zeile in zeilen:
        if not im_block and im_text is None and zeile.lstrip().startswith("//"):
            code.append(None)
            continue
        teile: list[str] = []
        i = 0
        while i < len(zeile):
            if im_block:
                ende = zeile.find("*/", i)
                im_block, i = (True, len(zeile)) if ende < 0 else (False, ende + 2)
                continue
            zeichen = zeile[i]
            if im_text is not None:
                teile.append(zeile[i:i + 2] if zeichen == "\\" else zeichen)
                if zeichen == im_text:
                    im_text = None
                i += 2 if zeichen == "\\" else 1
                continue
            if zeile.startswith("/*", i):
                im_block, i = True, i + 2
                continue
            if zeile.startswith("//", i):
                break
            if zeichen in TEXT_ZEICHEN:
                im_text = zeichen
            teile.append(zeichen)
            i += 1
        if im_text in ("'", '"'):
            im_text = None
        code.append("".join(teile))
    return code


def _filter(code: str, ab: int) -> str | None:
    treffer = OBJEKT_ALS_ZWEITES.match(code, ab)
    if treffer is None:
        return None
    tiefe = 0
    for i in range(treffer.end() - 1, len(code)):
        if code[i] == "{":
            tiefe += 1
        elif code[i] == "}":
            tiefe -= 1
            if tiefe == 0:
                return LEERRAUM.sub(" ", code[treffer.end():i]).strip()
    return None


def ereignisse_im_modul(datei: str, zeilen: list[str]) -> list[dict]:
    """SC-H12: je Zeile höchstens ein Aufruf on('<ereignis>' …), in Dateireihenfolge, ohne Kommentare."""
    gefunden = []
    for nr, (roh, code) in enumerate(zip(zeilen, _ohne_kommentare(zeilen)), start=1):
        if code is None:
            continue
        treffer = AUFRUF.search(code)
        if treffer is None:
            continue
        gefunden.append({"ereignis": treffer.group(2), "filter": _filter(code, treffer.end()),
                         "beleg": {"datei": datei, "zeile": nr, "zitat": roh.lstrip()}})
    return gefunden


def _kopfkommentar(datei: str, zeilen: list[str]) -> tuple[str, dict] | None:
    """Erster Absatz eines Block-Kommentars am Dateianfang (davor nur Leerzeilen) mit Beleg auf seine erste Textzeile."""
    start = next((i for i, z in enumerate(zeilen) if z.strip()), None)
    if start is None or not zeilen[start].lstrip().startswith("/*"):
        return None
    inhalt: list[tuple[int, str]] = []
    for i in range(start, len(zeilen)):
        text = zeilen[i]
        if i == start:
            text = text.lstrip()[2:]
        ende = text.find("*/")
        teil = text if ende < 0 else text[:ende]
        teil = teil.strip()
        while teil.startswith("*"):
            teil = teil[1:].strip()
        inhalt.append((i + 1, teil))
        if ende >= 0:
            break
    doc = Docstring(inhalt)
    erste = doc.erste_zeile()
    if erste is None:
        return None
    bewacht = erster_absatz(doc)
    return ohne_schlusspunkt(bewacht), {"datei": datei, "zeile": erste[0], "zitat": erste[1]}


def _aus_description(daten: dict, hook_text: str, hook_datei: str) -> tuple[str, dict] | None:
    beschreibung = daten.get("description")
    if not isinstance(beschreibung, str) or not beschreibung.strip():
        return None
    anfang = 1 if hook_text.startswith(BOM) else 0
    try:
        oben = json_spannen.zerlege(hook_text, anfang)
    except RecursionError:
        return None
    paare = [p for p in oben.paare if p.schluessel == "description"]
    if len(paare) != 1 or paare[0].wert.art != "text":
        return None
    paar = paare[0]
    zitat = hook_text[paar.wert.anfang + 1:paar.wert.ende - 1]
    bewacht = ohne_schlusspunkt(LEERRAUM.sub(" ", beschreibung).strip())
    return bewacht, {"datei": hook_datei, "zeile": json_spannen.zeile(hook_text, paar.wert.anfang), "zitat": zitat}


def _lies_modul(kit: Path, rel: str) -> list[str] | None:
    pfad = kit / rel
    try:
        if os.lstat(pfad).st_size > HOECHSTENS:
            return None
        fd = os.open(pfad, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as datei:
        if not stat.S_ISREG(os.fstat(datei.fileno()).st_mode):
            return None
        inhalt = datei.read(HOECHSTENS + 1)
    if len(inhalt) > HOECHSTENS:
        return None
    try:
        return zerlege(inhalt.decode("utf-8").removeprefix(BOM))
    except UnicodeDecodeError:
        return None


def _ids(namen: list[str], belegte_ids: list[str]) -> list[str]:
    """hook:<name>; trägt ein anderer Hook-Knoten die id schon: @modul, bei weiterer Gleichheit @modul-<n> (A3)."""
    belegt = set(belegte_ids)
    erste_frei: set[str] = set()
    kollidiert: list[bool] = []
    for name in namen:
        frei = f"hook:{name}" not in belegt and name not in erste_frei
        if frei:
            erste_frei.add(name)
        kollidiert.append(not frei)
    je_name = Counter(n for n, k in zip(namen, kollidiert) if k)
    laufend: Counter = Counter()
    ids = []
    for name, k in zip(namen, kollidiert):
        if not k:
            ids.append(f"hook:{name}")
        elif je_name[name] == 1:
            ids.append(f"hook:{name}@modul")
        else:
            laufend[name] += 1
            ids.append(f"hook:{name}@modul-{laufend[name]}")
    return ids


def modul_knoten(quelle: Ordner, daten: dict, hook_text: str, belegte_ids: list[str],
                 befunde: Befunde) -> list[dict]:
    """SC-H10 bis SC-H13. daten ist die gelesene Hook-Datei, hook_text ihr Text; modules hat schon die Form nach SC-H5."""
    module = eintraege(daten) or []
    kit = quelle.wurzel
    hook_datei = quelle.hooks
    description = _aus_description(daten, hook_text, hook_datei) if len(module) == 1 else None
    namen = [posixpath.splitext(posixpath.basename(aufgeloest(quelle, m)))[0] for m in module]
    knoten = []
    for eintrag, name, hook_id in zip(module, namen, _ids(namen, belegte_ids)):
        rel = aufgeloest(quelle, eintrag)
        k: dict = {"id": hook_id, "typ": "hook", "art": "modul", "ereignis": None, "matcher": None, "modul": rel}
        bewacht = description
        ereignisse: list[dict] = []
        if not im_kit_ohne_symlink(kit, rel):
            befunde.melde("verweis_fehlt",
                          f"modules nennt {eintrag}, das es im Kit nicht gibt (oder hinter einem Symlink bzw. "
                          f"außerhalb liegt); keine Ereignisse",
                          hook_datei, None, aussage="modules", ziel_art="modul", ziel=eintrag)
        elif not rel.endswith(ENDUNGEN):
            befunde.melde("nicht_unterstuetzt",
                          f"modules nennt {eintrag}; gelesen werden nur {', '.join(ENDUNGEN)}; keine Ereignisse",
                          hook_datei, None, aussage="modules", ziel_art="modul", ziel=eintrag)
        else:
            zeilen = _lies_modul(kit, rel)
            if zeilen is None:
                befunde.melde("nicht_lesbar",
                              f"{rel} ist nicht als UTF-8 lesbar oder größer als 1 MiB; keine Ereignisse",
                              rel, None, aussage="datei")
            else:
                ereignisse = ereignisse_im_modul(rel, zeilen)
                if bewacht is None:
                    bewacht = _kopfkommentar(rel, zeilen)
        k["bewacht"] = bewacht[0] if bewacht is not None else None
        if bewacht is not None:
            k["beleg"] = bewacht[1]
        k.update({"ereignisse": ereignisse, "herkunft": "scan", "pfade": []})
        knoten.append(k)
    return knoten
