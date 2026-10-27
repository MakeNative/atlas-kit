"""Leitplanken in der Werkstatt (specs/werkstatt.yaml WS-L3, WS-L4, WS-L6, WS-L7): Lesepfad für GET /api/leitplanken und
/api/leitplanken/skript, die Muster der Warnung befehl, die Skripte der Vorschau und die bewacht-Pflicht nach dem
Probelauf. Der Kern ruft dieses Modul nur auf; Hooks führt hier nichts aus."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import aenderungssatz
import hooks as hooks_modul
import plattform
import werkstatt_pfade as pfade
from hookdatei import HookDatei, HookDateiFehler, HookStelle
from ordner import Ordner
from virtuelles_kit import VirtuellesKit

HOECHSTENS = 1024 * 1024
FORMULAR_ZUERST = ("PreToolUse", "PostToolUse", "SubagentStop", "SubagentStart", "SessionStart", "Stop", "UserPromptSubmit")
NUR_GELESEN = "Dieses Kit ist eine installierte Kopie und wird nur gelesen."
PFAD_NICHT = "Diesen Pfad liest die Werkstatt nicht."
MODUL_HOOK = "Modul-Hook"
OHNE_SKRIPT = "Befehl ohne Skript"
LIES = "Lies das Skript, bevor du übernimmst."
MUSTER_SATZ = "Das Skript greift aufs Netz, löscht Dateien oder startet andere Programme. Prüf, ob das gewollt ist."
OHNE_BEWACHT = ("Das Skript {skript} sagt nicht, was es bewacht. Schreib es in den Docstring (Python) bzw. den "
                "Kommentarkopf (Shell) ganz oben.")


def _wort(name: str) -> re.Pattern:
    return re.compile(rf"(?<![\w.-]){re.escape(name)}(?![\w-])")


def _genau(text: str) -> re.Pattern:
    return re.compile(re.escape(text))


MUSTER: tuple[tuple[str, re.Pattern], ...] = (
    ("curl", _wort("curl")),
    ("wget", _wort("wget")),
    ("ssh", _wort("ssh")),
    ("scp", _wort("scp")),
    ("nc", _wort("nc")),
    ("ncat", _wort("ncat")),
    ("http://", _genau("http://")),
    ("https://", _genau("https://")),
    ("urllib", _wort("urllib")),
    ("requests", _wort("requests")),
    ("socket", _wort("socket")),
    ("http.client", _genau("http.client")),
    ("rm -r", re.compile(r"(?<![\w-])rm\s+(?:\S+\s+)*?(?:-[A-Za-z]*[rR][A-Za-z]*|--recursive)(?![\w-])")),
    ("rm -f", re.compile(r"(?<![\w-])rm\s+(?:\S+\s+)*?(?:-[A-Za-z]*f[A-Za-z]*|--force)(?![\w-])")),
    ("shutil.rmtree", _genau("shutil.rmtree")),
    ("os.remove", _genau("os.remove")),
    ("unlink", re.compile(r"(?<![\w-])unlink(?![\w-])")),
    ("sudo", _wort("sudo")),
    ("chmod", _wort("chmod")),
    ("chown", _wort("chown")),
    ("eval", re.compile(r"(?<![\w-])eval(?![\w-])")),
    ("exec", re.compile(r"(?<![\w-])exec(?![\w-])")),
    ("os.exec", re.compile(r"os\.exec\w*")),
    ("os.system", _genau("os.system")),
    ("from os import", re.compile(r"(?<![\w.])from\s+os\s+import\b")),
    ("os.popen", _genau("os.popen")),
    ("os.spawn", re.compile(r"os\.spawn\w*")),
    ("subprocess", _wort("subprocess")),
    ("runpy", _wort("runpy")),
    ("__import__", _genau("__import__")),
    ("base64", _wort("base64")),
    (". / source", re.compile(r"(?:^|[;&|]\s*|(?<![\w-])(?:then|do|else)\s+)(?:\.|source)\s+\S")),
    ("`…` / $(…)", re.compile(r"`|\$\(")),
    ("-c", re.compile(r"(?<![\w-])(?:ba|z|da)?sh\s+-[A-Za-z]*c\b|(?<![\w-])python[\d.]*\s+-[A-Za-z]*c\b")),
)
FORTSETZUNG = re.compile(r"\\\r?\n")
LEERRAUM = re.compile(r"[ \t]+")
PUNKT_FORTSETZUNG = re.compile(r"\.\s+(?=\w)")


LESE_GRUND = {pfade.LeseFehler.ZU_GROSS: "größer als 1 MiB", pfade.LeseFehler.NICHT_REGULAER: "keine Datei",
              pfade.LeseFehler.NICHT_OFFEN: "lässt sich nicht öffnen", pfade.LeseFehler.FEHLT: "fehlt"}
NICHT_ZU_ZEIGEN = "{skript} kann die Werkstatt nicht zeigen: {grund}; ohne gelesenes Skript keine Leitplanke."


class LeitplankenFehler(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
        self.text = text


def nicht_lesbar(datei: str, grund: str) -> str:
    return f"{datei} lässt sich nicht lesen ({grund})"


def funde(text: str) -> list[dict]:
    """WS-L6 (3): je logischer Zeile (Shell-Fortsetzungen mit \\ verbunden, Leerraum zusammengefasst) und Muster ein Fund
    mit der ersten Zeile, in Zeilen- und Musterreihenfolge. Die Funde sind Hinweise, kein Schutz."""
    gefunden = []
    nr = 1
    for logisch in re.split(r"(?<!\\)\n", text):
        zeile = LEERRAUM.sub(" ", FORTSETZUNG.sub(" ", logisch))
        zeile = zeile + " " + PUNKT_FORTSETZUNG.sub(".", zeile)
        for name, muster in MUSTER:
            if muster.search(zeile.strip()):
                gefunden.append({"zeile": nr, "muster": name})
        nr += logisch.count("\n") + 1
    return gefunden


def _hook_datei(ordner: Ordner, kit: Path) -> tuple[HookDatei | None, str | None]:
    """Die Hook-Datei für GET /api/leitplanken: nie hinter einem Symlink, höchstens 1 MiB (wie lies_skript)."""
    pfad = kit / ordner.hooks
    if pfade.symlink_auf_dem_weg(pfad, kit) is not None:
        return None, nicht_lesbar(ordner.hooks, "Symlink")
    try:
        inhalt = pfade.lies_regulaer(pfad, HOECHSTENS)
    except pfade.LeseFehler as fehler:
        if fehler.grund == pfade.LeseFehler.FEHLT:
            return HookDatei.lies(None, ordner.art), None
        return None, nicht_lesbar(ordner.hooks, LESE_GRUND.get(fehler.grund, "lässt sich nicht öffnen"))
    try:
        return HookDatei.lies(inhalt.decode("utf-8"), ordner.art), None
    except UnicodeDecodeError:
        return None, nicht_lesbar(ordner.hooks, "nicht als UTF-8 lesbar")
    except HookDateiFehler as fehler:
        return None, nicht_lesbar(ordner.hooks, fehler.grund)


def _skripte_im_ordner(ordner: Ordner, kit: Path) -> list[str]:
    hook_ordner = kit / ordner.hook_ordner
    if pfade.symlink_auf_dem_weg(hook_ordner, kit) is not None or not hook_ordner.is_dir():
        return []
    gefunden = []
    for eintrag in hook_ordner.iterdir():
        rel = f"{ordner.hook_ordner}/{eintrag.name}"
        if pfade.ist_hook_skript(rel, ordner.hook_ordner) and not plattform.ist_verweis(eintrag) and eintrag.is_file():
            gefunden.append(rel)
    return sorted(gefunden)


def uebersicht(ordner: Ordner, kit: Path, bauplan: dict, schreibbar: bool) -> dict:
    """GET /api/leitplanken (WS-L7)."""
    datei, grund = _hook_datei(ordner, kit)
    timeouts = {s.id: s.timeout for s in datei.eintraege()} if datei is not None else {}
    hook_knoten = [k for k in bauplan.get("knoten", []) if k.get("typ") == "hook"]
    if not schreibbar:
        grund = NUR_GELESEN
    ereignisse = [*FORMULAR_ZUERST, *(e for e in hooks_modul.EREIGNISSE if e not in FORMULAR_ZUERST)]
    skripte = [{"pfad": rel, "sprache": rel.rsplit(".", 1)[-1],
                "genutzt_von": [k["id"] for k in hook_knoten if k.get("skript") == rel]}
               for rel in _skripte_im_ordner(ordner, kit)]
    liste = []
    for k in hook_knoten:
        nicht = MODUL_HOOK if k.get("art") == "modul" else OHNE_SKRIPT if not k.get("skript") else None
        eintrag = {"id": k["id"], "aenderbar": nicht is None, "grund": nicht}
        if nicht is None:
            timeout = timeouts.get(k["id"])
            eintrag["timeout"] = timeout if isinstance(timeout, int) and not isinstance(timeout, bool) else None
        liste.append(eintrag)
    return {"datei": ordner.hooks, "ordner": ordner.hook_ordner, "schreibbar": grund is None, "grund": grund,
            "ereignisse": ereignisse, "skripte": skripte, "hooks": liste}


def lies_skript(kit: Path, ordner: Ordner, pfad: object) -> dict:
    """GET /api/leitplanken/skript (WS-L7): nur Skripte der Freigabeliste im Hook-Ordner, nie hinter einem Symlink."""
    if not isinstance(pfad, str) or not pfade.ist_hook_skript(pfad, ordner.hook_ordner):
        raise LeitplankenFehler(400, PFAD_NICHT)
    ziel = kit / pfad
    if pfade.symlink_auf_dem_weg(ziel, kit) is not None:
        raise LeitplankenFehler(409, f"{pfad} liest die Werkstatt nicht: Es ist ein Symlink.")
    try:
        inhalt = pfade.lies_regulaer(ziel, HOECHSTENS)
    except pfade.LeseFehler as fehler:
        if fehler.grund == pfade.LeseFehler.FEHLT:
            raise LeitplankenFehler(404, f"{pfad} gibt es nicht.")
        grund = {pfade.LeseFehler.ZU_GROSS: "Es ist größer als 1 MiB.",
                 pfade.LeseFehler.NICHT_REGULAER: "Es ist keine Datei."}.get(fehler.grund, "Es lässt sich nicht öffnen.")
        raise LeitplankenFehler(409, f"{pfad} liest die Werkstatt nicht: {grund}")
    try:
        text = inhalt.decode("utf-8")
    except UnicodeDecodeError:
        raise LeitplankenFehler(409, f"{pfad} liest die Werkstatt nicht: Es ist nicht als UTF-8 lesbar.")
    return {"pfad": pfad, "text": text, "stand": hashlib.sha256(inhalt).hexdigest()}


def _anzeigbar(kit: VirtuellesKit, skript: str) -> tuple[str | None, str | None]:
    """(Text, None), den die Vorschau zeigt (neu bzw. geändert oder von der Platte, ohne Symlink, bis 1 MiB, UTF-8;
    R1), sonst (None, Grund). Gelesenes bindet den Stand."""
    try:
        pfade.pruefe_rel(skript)
    except pfade.PfadFehler:
        return None, "kein Pfad im Kit"
    if pfade.symlink_auf_dem_weg(kit.wurzel / skript, kit.wurzel) is not None:
        return None, "Symlink"
    im_satz, inhalt = kit.neu_inhalt(skript)
    if not im_satz:
        try:
            inhalt = pfade.lies_regulaer(kit.wurzel / skript, HOECHSTENS)
        except pfade.LeseFehler as fehler:
            return None, LESE_GRUND.get(fehler.grund, "lässt sich nicht öffnen")
        kit.merke_gelesen(skript, inhalt)
    if inhalt is None:
        return None, "fehlt"
    if len(inhalt) > HOECHSTENS:
        return None, "größer als 1 MiB"
    try:
        return inhalt.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "nicht als UTF-8 lesbar"


def _stellen_nachher(kit: VirtuellesKit) -> list[HookStelle]:
    inhalt = kit.lies(kit.ordner.hooks)
    try:
        return HookDatei.lies(None if inhalt is None else inhalt.decode("utf-8"), kit.art).eintraege()
    except (UnicodeDecodeError, HookDateiFehler):
        return []


def _befehl(eid: str, stelle: HookStelle | None, skript: str, text: str | None, neu: bool) -> tuple[dict, dict | None]:
    name = stelle.id.removeprefix("hook:") if stelle else skript.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    ereignis = stelle.ereignis if stelle else None
    matcher = stelle.matcher if stelle else None
    gefunden = funde(text) if neu and text is not None else []
    wann = f"greift bei {ereignis}" + (f" ({matcher})" if matcher else "") if ereignis else "greift bei seinem Ereignis"
    warnung = {"pfad": skript, "art": "befehl", "eintrag": eid, "name": name, "ereignis": ereignis, "matcher": matcher,
               "text": f"Leitplanke {name} startet {skript} und {wann}. {LIES}", "funde": gefunden}
    if gefunden:
        warnung["muster_satz"] = MUSTER_SATZ
    skript_eintrag = {"eintrag": eid, "pfad": skript, "text": text, "funde": gefunden} if text is not None else None
    return warnung, skript_eintrag


def _betrifft_hooks(eintrag: dict, kit: VirtuellesKit) -> bool:
    art, op = aenderungssatz.operation(eintrag)
    return art in aenderungssatz.HOOK_OPERATIONEN or (
        art == "text_ersetzen" and pfade.ist_hook_skript(op.get("datei"), kit.ordner.hook_ordner))


def vorschau_teile(satz: dict, gewaehlt: set[str], kit: VirtuellesKit) -> tuple[list[dict], list[dict], list[dict]]:
    """WS-L4, WS-L6: Warnungen befehl und mitgenutzt, die Skripte je Eintrag (R1) und Fehler für Skripte, die sich nicht
    ganz zeigen lassen (ohne gelesenes Skript kein Häkchen), nach allen gewählten Einträgen."""
    if not any(_betrifft_hooks(e, kit) for e in satz["eintraege"] if e["id"] in gewaehlt):
        return [], [], []
    fehler: list[dict] = []
    stellen = _stellen_nachher(kit)
    warnungen: list[dict] = []
    skripte: list[dict] = []
    for e in satz["eintraege"]:
        if e["id"] not in gewaehlt:
            continue
        art, op = aenderungssatz.operation(e)
        if art == "hook_anlegen":
            treffer = [s for s in stellen if (s.ereignis, s.matcher, s.skript) == (op["ereignis"], op.get("matcher"),
                                                                                 op["skript"])]
            ziele = [(treffer[0] if treffer else None, op["skript"], "text" in op)]
        elif art == "hook_aendern" and any(f in op for f in ("ereignis", "matcher", "text")):
            ref = op["hook"]
            ereignis = op.get("ereignis", ref["ereignis"])
            matcher = op["matcher"] if "matcher" in op else ref.get("matcher")
            treffer = [s for s in stellen if (s.ereignis, s.matcher, s.skript) == (ereignis, matcher, ref["skript"])]
            ziele = [(treffer[0] if treffer else None, ref["skript"], "text" in op)]
            if "text" in op:
                andere = [s.id.removeprefix("hook:") for s in stellen if s.skript == ref["skript"]
                          and (s.ereignis, s.matcher) != (ereignis, matcher)]
                if andere:
                    warnungen.append({"pfad": ref["skript"], "art": "mitgenutzt", "eintrag": e["id"],
                                      "text": f"{ref['skript']} nutzen auch {', '.join(andere)}; der neue Text gilt für "
                                              f"alle."})
        elif art == "text_ersetzen" and pfade.ist_hook_skript(op["datei"], kit.ordner.hook_ordner):
            treffer = [s for s in stellen if s.skript == op["datei"]]
            ziele = [(treffer[0] if treffer else None, op["datei"], True)]
        else:
            continue
        for stelle, skript, neu in ziele:
            text, grund = _anzeigbar(kit, skript)
            if grund is not None:
                fehler.append({"eintrag": e["id"], "text": NICHT_ZU_ZEIGEN.format(skript=skript, grund=grund)})
            warnung, skript_eintrag = _befehl(e["id"], stelle, skript, text, neu)
            warnungen.append(warnung)
            if skript_eintrag is not None:
                skripte.append(skript_eintrag)
    befehle = [w for w in warnungen if w["art"] == "befehl"]
    return befehle + [w for w in warnungen if w["art"] != "befehl"], skripte, fehler


def ohne_bewacht(satz: dict, gewaehlt: set[str], nachher: dict) -> list[dict]:
    """WS-L3: Jeder neue Hook-Knoten hat nach dem Probelauf bewacht, sonst ein Fehler je Eintrag."""
    hooks = [k for k in nachher.get("knoten", []) if k.get("typ") == "hook"]
    fehler = []
    for e in satz["eintraege"]:
        if e["id"] not in gewaehlt:
            continue
        art, op = aenderungssatz.operation(e)
        if art != "hook_anlegen":
            continue
        knoten = next((k for k in hooks if (k.get("ereignis"), k.get("matcher"), k.get("skript"))
                       == (op["ereignis"], op.get("matcher"), op["skript"])), None)
        if knoten is not None and not knoten.get("bewacht"):
            fehler.append({"eintrag": e["id"], "text": OHNE_BEWACHT.format(skript=op["skript"])})
    return fehler


def hook_skripte_betroffen(kit: VirtuellesKit, aenderungen: dict) -> set[str]:
    """Ändert sich die Hook-Datei, betrifft das auch die Skripte, die sie nennt (Präfix-Regel SC-H7, WS-L4)."""
    if kit.ordner.hooks not in aenderungen:
        return set()
    vorher = VirtuellesKit(kit.ordner, kit.kit_id, None, None)
    return {s.skript for s in [*_stellen_nachher(vorher), *_stellen_nachher(kit)] if s.skript}
