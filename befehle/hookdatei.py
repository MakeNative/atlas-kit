"""Zeilen-Editor der Hook-Datei (specs/werkstatt.yaml WS-L2): Er ändert nur die Spannen unter dem Schlüssel hooks; alles
andere bleibt Byte für Byte. Neue Zeilen übernehmen Einrückung und Zeilenende der Nachbarn."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import hooks as hooks_modul
import json_spannen as js

BOM = "﻿"
EINHEIT = "  "
PLATZHALTER = "__atlas_eintrag__"


def _ohne_doppelte(paare: list[tuple[str, object]]) -> dict:
    schluessel = [k for k, _ in paare]
    doppelt = next((k for k in schluessel if schluessel.count(k) > 1), None)
    if doppelt is not None:
        raise HookDateiFehler(f"doppelter Schlüssel {doppelt}")
    return dict(paare)


class HookDateiFehler(Exception):
    def __init__(self, grund: str):
        super().__init__(grund)
        self.grund = grund


@dataclass
class HookStelle:
    ereignis: str
    matcher: str | None
    command: object
    skript: str | None
    timeout: object
    id: str
    lage: tuple[int, int, int]


def _spanne(kind: js.Wert | js.Paar) -> tuple[int, int]:
    return (kind.anfang, kind.wert.ende) if isinstance(kind, js.Paar) else (kind.anfang, kind.ende)


def _kinder(behaelter: js.Wert) -> list:
    return behaelter.paare if behaelter.art == "objekt" else behaelter.elemente


def _zeilen_anfang(text: str, offset: int) -> int:
    return text.rfind("\n", 0, offset) + 1


def einrueckung_vor(text: str, offset: int) -> str | None:
    """Der Leerraum vor offset in seiner Zeile; None, wenn davor in der Zeile noch etwas anderes steht."""
    vor = text[_zeilen_anfang(text, offset):offset]
    return vor if not vor.strip() else None


def eingerueckt(roh: str, von: str, nach: str, nl: str) -> str:
    """Folgezeilen eines mehrzeiligen Werts von der Einrückung von auf nach umsetzen."""
    zeilen = roh.split("\n")
    aus = [zeilen[0]]
    for zeile in zeilen[1:]:
        aus.append(nach + zeile[len(von):] if zeile.startswith(von) else zeile)
    return "\n".join(z.removesuffix("\r") for z in aus).replace("\n", nl)


class JsonText:
    """Ein JSON-Text mit seinem Baum; jede Änderung schneidet nur die betroffene Spanne aus bzw. fügt sie ein."""

    def __init__(self, text: str, einheit: str | None = None):
        self.text = text
        self.nl = "\r\n" if "\r\n" in text else "\n"
        self.anfang = 1 if text.startswith(BOM) else 0
        self.baum = js.zerlege(text, self.anfang)
        self.einheit = einheit if einheit is not None else self._einheit()

    def _einheit(self) -> str:
        for kind in _kinder(self.baum):
            vor = einrueckung_vor(self.text, _spanne(kind)[0])
            if vor:
                return vor
        return EINHEIT

    def _neu(self, text: str) -> None:
        self.text = text
        self.baum = js.zerlege(text, self.anfang)

    def finde(self, weg: tuple) -> js.Wert:
        wert = self.baum
        for schritt in weg:
            wert = wert.paar(schritt).wert if isinstance(schritt, str) else wert.elemente[schritt]
        return wert

    def anhaengen(self, weg: tuple, baue: Callable[[str | None], str]) -> None:
        """Hängt ans Ende des Behälters unter weg an; baue(einrückung) liefert den Text des Kinds (Folgezeilen mit dieser
        Einrückung), baue(None) einen einzeiligen."""
        behaelter = self.finde(weg)
        kinder = _kinder(behaelter)
        text = self.text
        if kinder:
            anfang, ende = _spanne(kinder[-1])
            vor = einrueckung_vor(text, anfang)
            neu = f",{self.nl}{vor}{baue(vor)}" if vor is not None else f", {baue(None)}"
            self._neu(text[:ende] + neu + text[ende:])
            return
        basis = einrueckung_vor(text, behaelter.anfang)
        if basis is None:
            basis = text[_zeilen_anfang(text, behaelter.anfang):]
            basis = basis[:len(basis) - len(basis.lstrip())]
        innen = basis + self.einheit
        neu = f"{self.nl}{innen}{baue(innen)}{self.nl}{basis}"
        self._neu(text[:behaelter.anfang + 1] + neu + text[behaelter.ende - 1:])

    def entferne(self, weg: tuple, index: int) -> None:
        kinder = _kinder(self.finde(weg))
        text = self.text
        anfang, ende = _spanne(kinder[index])
        if len(kinder) == 1:
            behaelter = self.finde(weg)
            self._neu(text[:behaelter.anfang + 1] + text[behaelter.ende - 1:])
        elif index == len(kinder) - 1:
            self._neu(text[:_spanne(kinder[index - 1])[1]] + text[ende:])
        else:
            self._neu(text[:anfang] + text[_spanne(kinder[index + 1])[0]:])

    def ersetze(self, weg: tuple, neu: str) -> None:
        wert = self.finde(weg)
        self._neu(self.text[:wert.anfang] + neu + self.text[wert.ende:])

    def roh(self, weg: tuple) -> tuple[str, str]:
        """Text eines Werts und die Einrückung seiner ersten Zeile."""
        wert = self.finde(weg)
        vor = einrueckung_vor(self.text, wert.anfang)
        return self.text[wert.anfang:wert.ende], vor if vor is not None else ""


def als_json(wert: object, einheit: str, einrueckung: str | None, nl: str) -> str:
    if einrueckung is None:
        return json.dumps(wert, ensure_ascii=False)
    return eingerueckt(json.dumps(wert, ensure_ascii=False, indent=einheit), "", einrueckung, nl)


class HookDatei:
    def __init__(self, json_text: JsonText | None, daten: dict | None, projekt: bool):
        self.projekt = projekt
        self.daten = daten
        self.json = json_text

    @classmethod
    def lies(cls, text: str | None, art: str) -> HookDatei:
        """None = die Datei fehlt. Wirft HookDateiFehler, wenn sie kein JSON-Objekt ist oder hooks eine unerwartete Form
        hat (SC-H5)."""
        projekt = art == "projekt"
        if text is None:
            return cls(None, None, projekt)
        try:
            daten = json.loads(text.removeprefix(BOM), object_pairs_hook=_ohne_doppelte)
        except json.JSONDecodeError as fehler:
            raise HookDateiFehler(f"kein gültiges JSON: {fehler.msg} in Zeile {fehler.lineno}")
        except RecursionError:
            raise HookDateiFehler("zu tief verschachtelt")
        if not isinstance(daten, dict):
            raise HookDateiFehler("kein JSON-Objekt")
        if "hooks" in daten and not hooks_modul.hooks_struktur_ok(daten["hooks"]):
            raise HookDateiFehler("hooks hat nicht die erwartete Form")
        try:
            json_text = JsonText(text)
        except RecursionError:
            raise HookDateiFehler("zu tief verschachtelt")
        return cls(json_text, daten, projekt)

    @property
    def nl(self) -> str:
        return self.json.nl if self.json is not None else "\n"

    @property
    def einheit(self) -> str:
        return self.json.einheit if self.json is not None else EINHEIT

    def _hooks(self) -> js.Wert | None:
        if self.json is None:
            return None
        paar = self.json.baum.paar("hooks")
        return paar.wert if paar is not None else None

    def eintraege(self) -> list[HookStelle]:
        hooks = self._hooks()
        if hooks is None:
            return []
        roh: list[dict] = []
        text = self.json.text
        for i, ereignis_paar in enumerate(hooks.paare):
            for j, gruppe in enumerate(ereignis_paar.wert.elemente):
                gruppe_daten = json.loads(text[gruppe.anfang:gruppe.ende])
                matcher = gruppe_daten.get("matcher") if isinstance(gruppe_daten.get("matcher"), str) else None
                for k, eintrag in enumerate(gruppe_daten["hooks"]):
                    if eintrag.get("type") != "command":
                        continue
                    skript = hooks_modul.skript_aus_command(eintrag.get("command"), self.projekt)
                    name = skript.rsplit("/", 1)[-1].rsplit(".", 1)[0] if skript else ereignis_paar.schluessel
                    roh.append({"ereignis": ereignis_paar.schluessel, "matcher": matcher, "command": eintrag.get("command"),
                                "skript": skript, "timeout": eintrag.get("timeout"), "name": name, "lage": (i, j, k)})
        ids = hooks_modul.command_ids(roh)
        return [HookStelle(e["ereignis"], e["matcher"], e["command"], e["skript"], e["timeout"], hook_id, e["lage"])
                for e, hook_id in zip(roh, ids)]

    def finde(self, ereignis: str, matcher: str | None, skript: str) -> list[HookStelle]:
        return [s for s in self.eintraege() if s.ereignis == ereignis and s.matcher == matcher and s.skript == skript]

    def _weg(self, stelle: HookStelle) -> tuple:
        i, j, k = stelle.lage
        ereignis = self._hooks().paare[i].schluessel
        return ("hooks", ereignis, j, "hooks", k)

    def roh_eintrag(self, stelle: HookStelle) -> tuple[str, str]:
        return self.json.roh(self._weg(stelle))

    def einfuegen(self, ereignis: str, matcher: str | None, roh_eintrag: str, einrueckung: str = "") -> None:
        """Platz nach WS-L2: letzte Gruppe desselben Ereignisses mit gleichem matcher, sonst neue Gruppe am Ende des
        Ereignisses, sonst neues Ereignis am Ende von hooks, sonst hooks als letzter Schlüssel, sonst eine neue Datei."""
        nl = self.nl

        def eintrag(innen: str | None) -> str:
            if innen is None:
                return " ".join(z.strip() for z in roh_eintrag.splitlines())
            return eingerueckt(roh_eintrag, einrueckung, innen, nl)

        def mit_eintrag(wert: object, innen: str | None) -> str:
            gerahmt = als_json(wert, self.einheit, innen, nl)
            zeile = next(z for z in gerahmt.split(nl) if PLATZHALTER in z)
            platz = zeile[:len(zeile) - len(zeile.lstrip())] if innen is not None else None
            return gerahmt.replace(json.dumps(PLATZHALTER), eintrag(platz))

        gruppe = {**({"matcher": matcher} if matcher is not None else {}), "hooks": [PLATZHALTER]}
        if self.json is None:
            self.json = JsonText(mit_eintrag({"hooks": {ereignis: [gruppe]}}, "") + "\n", EINHEIT)
            return
        hooks = self._hooks()
        if hooks is None:
            self.json.anhaengen((), lambda innen: f'"hooks": {mit_eintrag({ereignis: [gruppe]}, innen)}')
            return
        ereignis_paar = hooks.paar(ereignis)
        if ereignis_paar is None:
            self.json.anhaengen(("hooks",), lambda innen: f"{json.dumps(ereignis, ensure_ascii=False)}: "
                                                          f"{mit_eintrag([gruppe], innen)}")
            return
        gleiche = [j for j, s in enumerate(ereignis_paar.wert.elemente)
                   if self._matcher_von(s) == matcher]
        if gleiche:
            self.json.anhaengen(("hooks", ereignis, gleiche[-1], "hooks"), eintrag)
            return
        self.json.anhaengen(("hooks", ereignis), lambda innen: mit_eintrag(gruppe, innen))

    def _matcher_von(self, gruppe: js.Wert) -> str | None:
        paar = gruppe.paar("matcher")
        if paar is None:
            return None
        wert = json.loads(self.json.text[paar.wert.anfang:paar.wert.ende])
        return wert if isinstance(wert, str) else None

    def entfernen(self, stelle: HookStelle) -> str:
        """Entfernt den Eintrag; leer gewordene Gruppen, Ereignisse und ein leeres hooks fallen weg. Gibt den Rohtext des
        Eintrags zurück (command wörtlich)."""
        roh, _ = self.roh_eintrag(stelle)
        i, j, k = stelle.lage
        hooks = self._hooks()
        ereignis = hooks.paare[i].schluessel
        gruppen = hooks.paare[i].wert.elemente
        if len(gruppen[j].paar("hooks").wert.elemente) > 1:
            self.json.entferne(("hooks", ereignis, j, "hooks"), k)
        elif len(gruppen) > 1:
            self.json.entferne(("hooks", ereignis), j)
        elif len(hooks.paare) > 1:
            self.json.entferne(("hooks",), i)
        else:
            oben = self.json.baum.paare
            self.json.entferne((), next(n for n, p in enumerate(oben) if p.schluessel == "hooks"))
        return roh

    def setze_timeout(self, stelle: HookStelle, timeout: int | None) -> None:
        weg = self._weg(stelle)
        eintrag = self.json.finde(weg)
        paar = eintrag.paar("timeout")
        if paar is not None and timeout is not None:
            self.json.ersetze((*weg, "timeout"), str(timeout))
        elif paar is not None:
            self.json.entferne(weg, eintrag.paare.index(paar))
        elif timeout is not None:
            self.json.anhaengen(weg, lambda _innen: f'"timeout": {timeout}')

    def text(self) -> str | None:
        """None = die Datei fällt weg (im Kit ohne hooks und modules)."""
        if self.json is None:
            return None
        if not self.projekt and self.json.baum.paar("hooks") is None and self.json.baum.paar("modules") is None:
            return None
        return self.json.text


def roh_mit_timeout(roh: str, einheit: str, timeout: int | None) -> str:
    """Der Rohtext eines Eintrags mit geändertem timeout (null entfernt das Feld); alles andere bleibt wörtlich."""
    teil = JsonText(roh, einheit)
    paar = teil.baum.paar("timeout")
    if paar is not None and timeout is not None:
        teil.ersetze(("timeout",), str(timeout))
    elif paar is not None:
        teil.entferne((), teil.baum.paare.index(paar))
    elif timeout is not None:
        teil.anhaengen((), lambda _innen: f'"timeout": {timeout}')
    return teil.text
