from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt

EINGABE_HOECHSTENS = 1024 * 1024
PROTOKOLL_HOECHSTENS = 20 * 1024 * 1024
ENDE_LESEN = 4096
SPERRE_WARTEN_S = 2.0
SPERRE_TAKT_S = 0.01
LAUF_JSON_HOECHSTENS = 256 * 1024
EREIGNISSE = "ereignisse.jsonl"
SPERRE = "ereignisse.sperre"
LAUF_JSON = "lauf.json"
ABGEBROCHEN = "abgebrochen"
ABGESCHLOSSEN = "abgeschlossen"
VERLOREN = "verloren-"
LAUF_ID = re.compile(r"^l\d{8}-\d{6}-[0-9a-f]{6}$")
STEUERZEICHEN = re.compile(r"[\x00-\x1f\x7f]")
PRE_TOOL_USE_ROH = re.compile(rb'"hook_event_name"\s*:\s*"PreToolUse"')
WERKZEUG_ROH = re.compile(rb'"tool_name"\s*:\s*"([A-Za-z0-9_.:-]{1,100})"')
TOKENS_IM_TEXT = re.compile(r"\b(?:subagent_tokens|total_tokens):\s*(\d+)")
AGENT_WERKZEUGE = ("Agent", "Task")
FRAGE_WERKZEUG = "AskUserQuestion"
SKILL_WERKZEUG = "Skill"
BLOCKIERT_SATZ = ("Dieser Lauf wurde in der Atlas Werkstatt abgebrochen. Führ keine Werkzeuge mehr aus, sag dem "
                  "Nutzer, dass der Lauf abgebrochen ist und er das Terminal-Fenster schließen kann.")
GRENZE_ANTWORT = 4000
GRENZE_BESCHREIBUNG = 200
GRENZE_KURZ = 200
GRENZE_TEXT = 300
PFAD_FELDER = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path", "Glob": "pattern", "Grep": "pattern", "WebFetch": "url",
               "WebSearch": "query", "Bash": "command"}


class Text:
    def __init__(self) -> None:
        self.gekuerzt = False

    def kurz(self, wert: object, grenze: int) -> str | None:
        if not isinstance(wert, str):
            return None
        if len(wert) > grenze:
            self.gekuerzt = True
            return wert[:grenze]
        return wert

    def einzeilig(self, wert: object, grenze: int) -> str:
        if not isinstance(wert, str):
            return ""
        return self.kurz(STEUERZEICHEN.sub(" ", wert), grenze)


def _feld(daten: dict, *namen: str) -> str | None:
    for name in namen:
        wert = daten.get(name)
        if isinstance(wert, str):
            return wert
    return None


def _objekt(wert: object) -> dict:
    return wert if isinstance(wert, dict) else {}


def _kurz_fuer(werkzeug: str | None, eingabe: dict, text: Text) -> str:
    feld = PFAD_FELDER.get(werkzeug or "")
    return text.einzeilig(eingabe.get(feld), GRENZE_KURZ) if feld else ""


def _antwort_text(antwort: object) -> str | None:
    if isinstance(antwort, str):
        return antwort
    if not isinstance(antwort, dict):
        return None
    inhalt = antwort.get("content")
    if isinstance(inhalt, str):
        return inhalt
    if isinstance(inhalt, list):
        teile = [b.get("text") for b in inhalt if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str)]
        return "\n".join(teile) if teile else None
    return None


def _ganze_zahl(wert: object) -> int | None:
    return wert if isinstance(wert, int) and not isinstance(wert, bool) and wert >= 0 else None


def _tokens(antwort: object, text: str | None) -> int | None:
    gezaehlt = _ganze_zahl(_objekt(antwort).get("totalTokens"))
    if gezaehlt is not None:
        return gezaehlt
    treffer = TOKENS_IM_TEXT.search(text or "")
    return int(treffer.group(1)) if treffer else None


def _werkzeug_ereignis(hook: dict, art_vor: str, text: Text) -> tuple[str, dict] | None:
    werkzeug = hook.get("tool_name") if isinstance(hook.get("tool_name"), str) else None
    eingabe = _objekt(hook.get("tool_input"))
    if art_vor == "PreToolUse":
        if werkzeug in AGENT_WERKZEUGE:
            typ = eingabe.get("subagent_type")
            return "agent_start", {"subagent_type": typ if isinstance(typ, str) else None,
                                   "beschreibung": text.kurz(eingabe.get("description"), GRENZE_BESCHREIBUNG),
                                   "hintergrund": eingabe.get("run_in_background") is True}
        if werkzeug == SKILL_WERKZEUG:
            name = eingabe.get("skill")
            return "skill", {"name": name if isinstance(name, str) else None}
        if werkzeug == FRAGE_WERKZEUG:
            fragen = eingabe.get("questions") if isinstance(eingabe.get("questions"), list) else []
            erste = _objekt(fragen[0]) if fragen else {}
            return "frage", {"text": text.kurz(erste.get("question"), GRENZE_TEXT), "fragen": len(fragen)}
        return "werkzeug", {"werkzeug": werkzeug, "kurz": _kurz_fuer(werkzeug, eingabe, text)}
    if art_vor == "PostToolUse":
        if werkzeug in AGENT_WERKZEUGE:
            antwort = hook.get("tool_response")
            ganz = _antwort_text(antwort)
            return "agent_ende", {"antwort": text.kurz(ganz, GRENZE_ANTWORT), "tokens": _tokens(antwort, ganz),
                                  "agent_id": _feld(_objekt(antwort), "agent_id", "agentId"),
                                  "dauer_ms": _ganze_zahl(_objekt(antwort).get("totalDurationMs"))}
        if werkzeug == FRAGE_WERKZEUG:
            return "frage_beantwortet", {}
        return None
    fehler = text.kurz(hook.get("error"), GRENZE_TEXT)
    if werkzeug in AGENT_WERKZEUGE:
        return "agent_fehler", {"fehler": fehler}
    return "werkzeug_fehler", {"werkzeug": werkzeug, "fehler": fehler}


def _art_und_daten(hook: dict, name: str, text: Text) -> tuple[str, dict] | None:
    if name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        return _werkzeug_ereignis(hook, name, text)
    if name == "SessionStart":
        return "sitzung_start", {"quelle": _feld(hook, "source"), "cwd": _feld(hook, "cwd")}
    if name == "SessionEnd":
        return "sitzung_ende", {"grund": _feld(hook, "reason")}
    if name == "UserPromptSubmit":
        return "eingabe", {}
    if name == "Stop":
        return "stopp", {}
    if name == "SubagentStart":
        return "subagent_start", {}
    if name == "SubagentStop":
        return "subagent_stop", {}
    if name == "PermissionRequest":
        werkzeug = _feld(hook, "tool_name")
        return "freigabe", {"werkzeug": werkzeug, "kurz": _kurz_fuer(werkzeug, _objekt(hook.get("tool_input")), text)}
    if name == "Notification":
        return "hinweis", {"text": text.kurz(hook.get("message"), GRENZE_TEXT)}
    return None


def zeitstempel(jetzt: datetime) -> str:
    return jetzt.astimezone().isoformat(timespec="milliseconds")


def ereignis(hook: dict, gekuerzt: bool, jetzt: datetime) -> dict | None:
    name = hook.get("hook_event_name")
    if not isinstance(name, str):
        return None
    text = Text()
    gebaut = _art_und_daten(hook, name, text)
    if gebaut is None:
        return None
    art, daten = gebaut
    if gekuerzt or text.gekuerzt:
        daten["gekuerzt"] = True
    return {"v": 1, "zeit": zeitstempel(jetzt), "art": art, "sitzung": _feld(hook, "session_id"),
            "agent": _feld(hook, "agent_id", "agentId"), "agent_typ": _feld(hook, "agent_type", "agentType"),
            "id": _feld(hook, "tool_use_id"), "daten": daten}


def blockiert(hook: dict, jetzt: datetime) -> dict:
    return {"v": 1, "zeit": zeitstempel(jetzt), "art": "blockiert", "sitzung": _feld(hook, "session_id"),
            "agent": _feld(hook, "agent_id", "agentId"), "agent_typ": _feld(hook, "agent_type", "agentType"),
            "id": _feld(hook, "tool_use_id"), "daten": {"werkzeug": _feld(hook, "tool_name")}}


def als_zeile(daten: dict) -> bytes:
    return (json.dumps(daten, ensure_ascii=False, separators=(", ", ": ")) + "\n").encode("utf-8")


def _kein_verweis_ordner(pfad: Path) -> bool:
    try:
        info = os.lstat(pfad)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not _ist_junction(info)


def _ist_junction(info: os.stat_result) -> bool:
    return getattr(info, "st_reparse_tag", 0) == 0xA0000003


def pruefe_lauf_ordner(roh: str) -> Path | None:
    if not os.path.isabs(roh):
        return None
    ordner = Path(roh)
    laeufe = ordner.parent
    werkstatt = laeufe.parent
    if not LAUF_ID.fullmatch(ordner.name) or laeufe.name != "laeufe" or werkstatt.name != "werkstatt":
        return None
    if not all(_kein_verweis_ordner(p) for p in (werkstatt, laeufe, ordner)):
        return None
    datei = ordner / LAUF_JSON
    try:
        info = os.lstat(datei)
        if not stat.S_ISREG(info.st_mode) or info.st_size > LAUF_JSON_HOECHSTENS:
            return None
        lauf = json.loads(datei.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(lauf, dict) or lauf.get("id") != ordner.name:
        return None
    return ordner


def lies_eingabe(eingabe: BinaryIO) -> tuple[dict | None, bool, bytes]:
    roh = eingabe.read(EINGABE_HOECHSTENS + 1)
    gekuerzt = len(roh) > EINGABE_HOECHSTENS
    roh = roh[:EINGABE_HOECHSTENS]
    try:
        text = roh.decode("utf-8", errors="ignore" if gekuerzt else "strict")
        daten, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except ValueError:
        return None, gekuerzt, roh
    return (daten if isinstance(daten, dict) else None), gekuerzt, roh


def _unlesbar_als_hook(roh: bytes) -> dict | None:
    if not PRE_TOOL_USE_ROH.search(roh):
        return None
    werkzeug = WERKZEUG_ROH.search(roh)
    name = werkzeug.group(1).decode("utf-8", errors="replace") if werkzeug else None
    return {"hook_event_name": "PreToolUse", "tool_name": name}


def _sperre_versuchen(fd: int) -> bool:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _sperre_loesen(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _verloren(lauf_ordner: Path) -> None:
    name = f"{VERLOREN}{datetime.now().strftime('%Y%m%dT%H%M%S')}-{os.urandom(3).hex()}"
    try:
        os.close(os.open(lauf_ordner / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600))
    except OSError:
        pass


def _schon_voll(datei: Path, groesse: int) -> bool:
    try:
        with open(datei, "rb") as f:
            f.seek(max(0, groesse - ENDE_LESEN))
            ende = f.read(ENDE_LESEN)
    except OSError:
        return False
    return b'"art": "voll"' in ende


def _anhaengen(datei: Path, zeile: bytes) -> None:
    fd = os.open(datei, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, zeile)
    finally:
        os.close(fd)


def _schreibe_gesperrt(lauf_ordner: Path, zeile: bytes, voll_zeile: bytes | None, immer: bool) -> None:
    datei = lauf_ordner / EREIGNISSE
    try:
        groesse = os.lstat(datei).st_size
    except FileNotFoundError:
        groesse = 0
    nahe_voll = groesse > PROTOKOLL_HOECHSTENS - EINGABE_HOECHSTENS and _schon_voll(datei, groesse)
    if groesse + len(zeile) <= PROTOKOLL_HOECHSTENS and not nahe_voll:
        _anhaengen(datei, zeile)
        return
    if voll_zeile is not None and not nahe_voll:
        _anhaengen(datei, voll_zeile)
    if immer:
        _anhaengen(datei, zeile)


def schreibe(lauf_ordner: Path, zeile: bytes, warten: float = SPERRE_WARTEN_S, voll_zeile: bytes | None = None,
             immer: bool = False) -> bool:
    try:
        fd = os.open(lauf_ordner / SPERRE, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0), 0o600)
    except OSError:
        _verloren(lauf_ordner)
        return False
    try:
        ende = time.monotonic() + warten
        while not _sperre_versuchen(fd):
            if time.monotonic() >= ende:
                _verloren(lauf_ordner)
                return False
            time.sleep(SPERRE_TAKT_S)
        try:
            _schreibe_gesperrt(lauf_ordner, zeile, voll_zeile, immer)
            return True
        except OSError:
            return False
        finally:
            _sperre_loesen(fd)
    finally:
        os.close(fd)


def main(argv: list[str], eingabe: BinaryIO) -> int:
    if len(argv) != 2:
        return 0
    lauf_ordner = pruefe_lauf_ordner(argv[1])
    if lauf_ordner is None or (lauf_ordner / ABGESCHLOSSEN).exists():
        return 0
    hook, gekuerzt, roh = lies_eingabe(eingabe)
    abgebrochen = (lauf_ordner / ABGEBROCHEN).exists()
    if hook is None and abgebrochen:
        hook = _unlesbar_als_hook(roh)
    if hook is None:
        return 0
    jetzt = datetime.now()
    voll = als_zeile({"v": 1, "zeit": zeitstempel(jetzt), "art": "voll", "sitzung": _feld(hook, "session_id"),
                      "agent": None, "agent_typ": None, "id": None, "daten": {}})
    if hook.get("hook_event_name") == "PreToolUse" and abgebrochen:
        schreibe(lauf_ordner, als_zeile(blockiert(hook, jetzt)), voll_zeile=voll)
        sys.stderr.write(BLOCKIERT_SATZ + "\n")
        sys.stderr.flush()
        return 2
    gebaut = ereignis(hook, gekuerzt, jetzt)
    if gebaut is None:
        return 0
    schreibe(lauf_ordner, als_zeile(gebaut), voll_zeile=voll, immer=gebaut["art"] == "sitzung_ende")
    return 0


def _einstieg() -> int:
    try:
        return main(sys.argv, sys.stdin.buffer)
    except Exception:  # noqa: BLE001
        return 0


if __name__ == "__main__":
    sys.exit(_einstieg())
