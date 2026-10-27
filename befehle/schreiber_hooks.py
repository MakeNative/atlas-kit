"""Hook-Operationen des Schreibers (specs/werkstatt.yaml WS-L1 bis WS-L5): Leitplanke anlegen, ändern, entfernen.

Die Hook-Datei ändert der Zeilen-Editor (hookdatei.py); danach prüft der Mixin mit json.loads, dass das Ergebnis gültiges
JSON ist und alle übrigen Werte gleich sind (WS-L2). Hooks führt hier nichts aus."""
from __future__ import annotations

import copy
import hashlib
import json

import werkstatt_pfade as pfade
from hookdatei import BOM, HookDatei, HookDateiFehler, HookStelle, roh_mit_timeout
from virtuelles_kit import SchreibFehler


def nicht_lesbar(datei: str, grund: str) -> str:
    return f"{datei} lässt sich nicht lesen ({grund}); die Werkstatt schreibt dort keine Leitplanke."


def nicht_sauber(datei: str) -> str:
    return f"Die Werkstatt konnte {datei} nicht sauber ändern; nichts geschrieben."


PYTHON_STARTER = ("sh -c 'for p in python3 python \"py -3\"; do "
                  "$p -c \"import sys; sys.exit(sys.version_info < (3, 9))\" 2>/dev/null && exec $p \"$0\"; done; "
                  "echo \"Kein Python 3.9 oder neuer gefunden (python3, python, py -3).\" >&2; exit 1'")


def befehl_fuer(skript: str, projekt: bool) -> str:
    """WS-L1: ein fester Befehl je Sprache. Python startet über PYTHON_STARTER, der python3, python und py -3 der Reihe
    nach probiert (Windows: Platzhalter des Microsoft Store, nur py), Shell über sh."""
    wurzel = "$CLAUDE_PROJECT_DIR" if projekt else "${CLAUDE_PLUGIN_ROOT}"
    programm = "sh" if skript.endswith(".sh") else PYTHON_STARTER
    return f'{programm} "{wurzel}/{skript}"'


def skript_text(text: str) -> bytes:
    """LF und ein Zeilenumbruch am Ende (WS-L3)."""
    text = text.replace("\r\n", "\n")
    return (text if text.endswith("\n") else text + "\n").encode("utf-8")


def _gruppe_dazu(hooks: dict, ereignis: str, matcher: str | None, eintrag: dict) -> None:
    gruppen = hooks.setdefault(ereignis, [])
    gleiche = [g for g in gruppen if (g.get("matcher") if isinstance(g.get("matcher"), str) else None) == matcher]
    if gleiche:
        gleiche[-1]["hooks"].append(eintrag)
    else:
        gruppen.append({**({"matcher": matcher} if matcher is not None else {}), "hooks": [eintrag]})


def _ohne(daten: dict, lage: tuple[int, int, int]) -> dict:
    i, j, k = lage
    hooks = daten["hooks"]
    ereignis = list(hooks)[i]
    gruppe = hooks[ereignis][j]
    del gruppe["hooks"][k]
    if not gruppe["hooks"]:
        del hooks[ereignis][j]
    if not hooks[ereignis]:
        del hooks[ereignis]
    if not hooks:
        del daten["hooks"]
    return daten


class HooksMixin:
    def _hook_datei(self) -> HookDatei:
        rel = self.kit.ordner.hooks
        inhalt = self.kit.lies(rel)
        try:
            text = None if inhalt is None else inhalt.decode("utf-8")
            return HookDatei.lies(text, self.kit.art)
        except UnicodeDecodeError:
            raise SchreibFehler(nicht_lesbar(rel, "nicht als UTF-8 lesbar"))
        except HookDateiFehler as fehler:
            raise SchreibFehler(nicht_lesbar(rel, fehler.grund))

    def _hook_ordner(self) -> str:
        return self.kit.ordner.hook_ordner

    def _schreibe_hook_datei(self, datei: HookDatei, erwartet: dict) -> None:
        rel = self.kit.ordner.hooks
        neu = datei.text()
        weg = not self.kit.art == "projekt" and "hooks" not in erwartet and "modules" not in erwartet
        if neu is None:
            if not weg:
                raise SchreibFehler(nicht_sauber(rel))
            self.kit.loesche(rel)
            return
        try:
            daten = json.loads(neu.removeprefix(BOM))
        except (json.JSONDecodeError, RecursionError):
            raise SchreibFehler(nicht_sauber(rel))
        if weg or daten != erwartet or list(daten) != list(erwartet):
            raise SchreibFehler(nicht_sauber(rel))
        self.kit.schreibe(rel, neu.encode("utf-8"))

    def _eine_stelle(self, datei: HookDatei, ref: dict) -> HookStelle:
        stellen = datei.finde(ref["ereignis"], ref.get("matcher"), ref["skript"])
        if not stellen:
            raise SchreibFehler(f"Die Leitplanke {ref['skript']} unter {ref['ereignis']} gibt es nicht.")
        if len(stellen) > 1:
            raise SchreibFehler(f"Die Leitplanke {ref['skript']} unter {ref['ereignis']} ist mehrfach eingetragen; die "
                                f"Werkstatt weiß nicht, welche gemeint ist.")
        return stellen[0]

    def _skript_schreiben(self, skript: str, text: str) -> None:
        self.kit.schreibe(skript, skript_text(text))

    def hook_anlegen(self, op: dict, _eid: str) -> None:
        datei = self._hook_datei()
        ereignis, matcher, skript = op["ereignis"], op.get("matcher"), op["skript"]
        if "text" in op:
            if self.kit.existiert(skript):
                raise SchreibFehler(f"{skript} gibt es schon. Wähl einen anderen Namen oder „Vorhandenes Skript“.")
            self._skript_schreiben(skript, op["text"])
        else:
            try:
                self.kit.pruefe_ziel(skript)
            except SchreibFehler:
                raise SchreibFehler(f"{skript} gibt es nicht.")
            inhalt = self.kit.lies(skript)
            if inhalt is None:
                raise SchreibFehler(f"{skript} gibt es nicht.")
        doppelt = datei.finde(ereignis, matcher, skript)
        if doppelt:
            raise SchreibFehler(f"Diese Leitplanke gibt es schon: {doppelt[0].id.removeprefix('hook:')} unter {ereignis}.")
        eintrag = {"type": "command", "command": befehl_fuer(skript, self.kit.art == "projekt")}
        if op.get("timeout") is not None:
            eintrag["timeout"] = op["timeout"]
        erwartet = copy.deepcopy(datei.daten) if datei.daten is not None else {}
        _gruppe_dazu(erwartet.setdefault("hooks", {}), ereignis, matcher, eintrag)
        datei.einfuegen(ereignis, matcher, json.dumps(eintrag, ensure_ascii=False, indent=datei.einheit))
        self._schreibe_hook_datei(datei, erwartet)

    def hook_aendern(self, op: dict, _eid: str) -> None:
        datei = self._hook_datei()
        stelle = self._eine_stelle(datei, op["hook"])
        if not any(feld in op for feld in ("ereignis", "matcher", "text", "timeout")):
            raise SchreibFehler("Es fehlt, was sich ändern soll.")
        if "text" in op:
            self._skript_ersetzen(stelle.skript, op["text"], op.get("text_stand"))
        ereignis = op.get("ereignis", stelle.ereignis)
        matcher = op["matcher"] if "matcher" in op else stelle.matcher
        verschoben = ereignis != stelle.ereignis or matcher != stelle.matcher
        if not verschoben and "timeout" not in op:
            return
        erwartet = copy.deepcopy(datei.daten)
        i, j, k = stelle.lage
        alt = list(erwartet["hooks"].values())[i][j]["hooks"][k]
        neu_eintrag = {**alt}
        if "timeout" in op:
            neu_eintrag.pop("timeout", None)
            if op["timeout"] is not None:
                neu_eintrag["timeout"] = op["timeout"]
        if not verschoben:
            list(erwartet["hooks"].values())[i][j]["hooks"][k] = neu_eintrag
            datei.setze_timeout(stelle, op["timeout"])
            self._schreibe_hook_datei(datei, erwartet)
            return
        roh, einrueckung = datei.roh_eintrag(stelle)
        if "timeout" in op:
            roh = roh_mit_timeout(roh, datei.einheit, op["timeout"])
        _gruppe_dazu(erwartet["hooks"], ereignis, matcher, neu_eintrag)
        erwartet = _ohne(erwartet, stelle.lage)
        datei.einfuegen(ereignis, matcher, roh, einrueckung)
        datei.entfernen(stelle)
        self._schreibe_hook_datei(datei, erwartet)

    def _skript_ersetzen(self, skript: str | None, text: str, stand: str | None) -> None:
        ordner = self._hook_ordner()
        if skript is None or not pfade.ist_hook_skript(skript, ordner):
            raise SchreibFehler(f"{skript} liegt außerhalb von {ordner}/; den Text ändert die Werkstatt nicht.")
        self.kit.pruefe_ziel(skript)
        vorher = self.kit.von_platte(skript)
        if vorher is None:
            raise SchreibFehler(f"{skript} gibt es nicht.")
        if stand is not None and hashlib.sha256(vorher).hexdigest() != stand:
            raise SchreibFehler(f"{skript} hat sich seit dem Laden geändert. Öffne die Leitplanke neu.")
        self._skript_schreiben(skript, text)

    def hook_entfernen(self, op: dict, eid: str) -> None:
        datei = self._hook_datei()
        stelle = self._eine_stelle(datei, op["hook"])
        erwartet = _ohne(copy.deepcopy(datei.daten), stelle.lage)
        datei.entfernen(stelle)
        self._schreibe_hook_datei(datei, erwartet)
        if stelle.skript and pfade.ist_hook_skript(stelle.skript, self._hook_ordner()):
            self.buch.hook_skripte_frei[stelle.skript] = eid

    def hook_skripte_aufraeumen(self) -> list[tuple[str, str]]:
        """WS-L5 nach dem letzten gewählten Eintrag: Skripte entfernter Leitplanken, die kein Hook der Hook-Datei mehr
        nutzt, fallen weg; (Eintrag, Skript) je gelöschtem Skript."""
        if not self.buch.hook_skripte_frei:
            return []
        try:
            genutzt = {s.skript for s in self._hook_datei().eintraege()}
        except SchreibFehler:
            return []
        geloescht = []
        for skript, eid in self.buch.hook_skripte_frei.items():
            if skript in genutzt or not self.kit.existiert(skript):
                continue
            try:
                self.kit.loesche(skript)
            except SchreibFehler:
                continue
            geloescht.append((eid, skript))
        return geloescht
