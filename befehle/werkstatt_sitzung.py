"""bin/atlas-kit werkstatt warte|fortschritt|frage|ergebnis|abbruch (specs/werkstatt.yaml WS-K4 bis WS-K9).

Die Befehle der Werkstatt-Sitzung: Die Skill /atlas:werkstatt ruft sie aus der laufenden Claude-Code-Sitzung auf,
mit dem Sitzungs-Schlüssel aus der Zustand-Datei <datenordner>/werkstatt/<kennung>.json. Sie sprechen nur mit dem
Server auf 127.0.0.1 und schreiben nichts.

Steuerzeilen ('@atlas …') schreibt nur dieses Modul, und nur aus geprüften Werten: die Kennung nach ihrem Muster,
der Agent aus der festen Liste, Pfade nur, wenn sie im Auftragsordner dieses Auftrags liegen. Alles andere geht über
sage, das Steuerzeichen maskiert und '@atlas' einrückt. Der Text des Auftrags kommt nie in den Chat. Die erste
Prüfmeldung bzw. der Grund eines Fehlschlags kann Namen, Titel oder Befundtexte aus dem eingereichten Satz bzw. dem
Kit enthalten; sie kommen nur maskiert, einzeilig und auf 300 Zeichen gekürzt in den Chat.

Die Sitzung bekommt beim Start eine Sitzungs-ID (`<kennung>.s<8 Hex>` in '@atlas werkstatt:'); warte trägt sie
mit. Startet eine neue Sitzung, übernimmt sie, und warte der alten endet mit 'abgelöst'.
Nur Standardbibliothek.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import ableiten
import einrichtung
import plattform
import scanner_lauf
import texte

BEFEHLE = ("warte", "fortschritt", "frage", "ergebnis", "abbruch")
STEUER = ableiten.STEUER
# <st_dev>-<st_ino> hexadezimal; st_ino hat auf ReFS (Windows Dev Drive) bis zu 128 Bit.
KENNUNG = re.compile(r"^[0-9a-f]{1,16}-[0-9a-f]{1,32}$")
WERKSTATT = re.compile(r"^([0-9a-f]{1,16}-[0-9a-f]{1,32})\.(s[0-9a-f]{8})$")
AUFTRAG_ID = re.compile(r"^a[0-9]{1,6}-[0-9a-f]{6}$")
BEZUG = re.compile(r"^([0-9a-f]{1,16}-[0-9a-f]{1,32})\.(a[0-9]{1,6}-[0-9a-f]{6})$")
ANTWORT_DATEI = re.compile(r"^antwort-[0-9]{1,4}\.txt$")
TOKENS = re.compile(r"^[0-9]{1,9}$")
AGENTEN = ("umbauer", "gutachter", "pipeline-planer", "auswerter")
ART_TEXT = {"aendern": "ändern", "bewerten": "bewerten", "einbauen": "Vorschläge einbauen", "neu": "neue Pipeline"}
ART_TEXT["entwerfen"] = "neue Pipeline entwerfen"  # Ticket 17
ART_TEXT["auswerten"] = "Läufe auswerten"
ZEILE_HOECHSTENS = 300
WARTE_GESAMT_S = 540.0
STUECK_S = 20.0
# Eine Grenze für alle Befehle: Jeder Weg der Sitzung braucht beim Server höchstens PRUEFUNG_HOECHSTENS_S für Sperre
# und Einlesen; die Befehle warten je Anfrage 60 s länger (warte zusätzlich sein Stück), ein Befehl insgesamt
# höchstens BEFEHL_HOECHSTENS_S, unter dem Timeout von 600000 ms, den die Skill für warte und ergebnis nennt. Eine
# Zeitüberschreitung ist nie das Ende der Werkstatt und gibt keinen Auftrag auf.
ANTWORT_WARTEN_S = scanner_lauf.PRUEFUNG_HOECHSTENS_S + 60.0
BEFEHL_HOECHSTENS_S = 570.0

BEENDET = "Die Werkstatt ist beendet."
ABGEBROCHEN = "Der Auftrag wurde in der Seite abgebrochen."
ABGELOEST = "Eine andere Claude-Code-Sitzung hat die Werkstatt übernommen."


class Zeitueberschreitung(Exception):
    """Der Server hat innerhalb der Wartezeit nicht geantwortet (er lebt aber, die Verbindung stand)."""


def nicht_geprueft(aid: str, sekunden: float) -> str:
    return f"Die Werkstatt hat die Antwort zu Auftrag {aid} nicht innerhalb von {sekunden:g} s geprüft. Den Stand zeigt die Seite."


def keine_antwort(aid: str, sekunden: float) -> str:
    return f"Die Werkstatt hat zu Auftrag {aid} nicht innerhalb von {sekunden:g} s geantwortet. Den Stand zeigt die Seite."


def keine_werkstatt(kennung: str) -> str:
    return f"{kennung} ist keine Werkstatt."


def kein_auftrag(bezug: str) -> str:
    return f"{bezug} ist kein Auftrag der Werkstatt."


def gutachten_satz(aid: str, vorschlaege: object, verworfen: object) -> str:
    """WS-K15: nur Zahlen, keine Texte aus dem Gutachten."""
    n = vorschlaege if isinstance(vorschlaege, int) and not isinstance(vorschlaege, bool) else 0
    m = verworfen if isinstance(verworfen, int) and not isinstance(verworfen, bool) else 0
    satz = f"Gutachten zu Auftrag {aid} liegt in der Werkstatt: {n} {'Vorschlag' if n == 1 else 'Vorschläge'}"
    return f"{satz}, {m} verworfen." if m else f"{satz}."


def auswertung_satz(aid: str, zahlen: object) -> str:
    zahlen = zahlen if isinstance(zahlen, dict) else {}

    def zahl(name: str) -> int:
        wert = zahlen.get(name)
        return wert if isinstance(wert, int) and not isinstance(wert, bool) else 0
    satz = (f"Auswertung zu Auftrag {aid} liegt in der Werkstatt: {zahl('bewertungen')} Bewertungen, "
            f"{zahl('vorschlaege')} Vorschläge, {zahl('gedaechtnis')} Memory-Einträge")
    return f"{satz}, {zahl('verworfen')} verworfen." if zahl("verworfen") else f"{satz}."


def eigener_pfad(pfad: object, aid: str, art: str, ergebnis: object) -> bool:
    """Ein Pfad für eine Steuerzeile: absolut, aufgelöst, genau in <ergebnis>/werkstatt/auftraege/<aid>/ dieser
    Werkstatt (ergebnis aus ihrer Zustand-Datei), bekannter Dateiname."""
    if not isinstance(pfad, str) or STEUER.strip() in pfad or any(ableiten.ist_steuerzeichen(z) for z in pfad):
        return False
    if not isinstance(ergebnis, str) or not os.path.isabs(ergebnis):
        return False
    p = Path(pfad)
    if not p.is_absolute() or ".." in p.parts or len(p.parts) < 5:
        return False
    if p.parent.name != aid or p.parent.parent.name != "auftraege" or p.parent.parent.parent.name != "werkstatt":
        return False
    if os.path.realpath(p.parent) != str(p.parent):
        return False
    if p.parent != Path(os.path.realpath(ergebnis)) / "werkstatt" / "auftraege" / aid:
        return False
    return p.name == "auftrag.json" if art == "auftrag.json" else bool(ANTWORT_DATEI.fullmatch(p.name))


def lies_zustand(datei: Path, mit_pid: bool = False) -> dict | None:
    """Zustand-Datei eines Servers (WS-B5): port und schluessel, mit mit_pid auch pid; sonst None."""
    try:
        daten = json.loads(datei.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(daten, dict) or not isinstance(daten.get("port"), int) \
            or not isinstance(daten.get("schluessel"), str) or (mit_pid and not isinstance(daten.get("pid"), int)):
        return None
    return daten


def lebt(pid: object) -> bool:
    """Läuft ein Prozess mit dieser pid? Ohne Prozess bekommt der Port aus der Zustand-Datei keinen Schlüssel."""
    return plattform.lebt(pid)


def anfrage(zustand: dict, methode: str, pfad: str, daten: object = None, warten: float = ANTWORT_WARTEN_S,
            recht: str = "schluessel", zeit_melden: bool = False) -> tuple[int, object] | None:
    """Anfrage an den Server auf 127.0.0.1 mit dem Recht recht aus der Zustand-Datei: (Status, JSON) oder None, wenn
    er nicht antwortet; POST schickt daten als JSON mit Origin. Mit zeit_melden wirft eine Zeitüberschreitung
    Zeitueberschreitung statt None."""
    if not isinstance(zustand.get(recht), str):
        return None
    ursprung = f"http://127.0.0.1:{zustand['port']}"
    koepfe = {"X-Atlas-Werkstatt": zustand[recht]}
    inhalt = None
    if methode == "POST":
        koepfe.update({"Origin": ursprung, "Content-Type": "application/json"})
        inhalt = json.dumps(daten if daten is not None else {}).encode("utf-8")
    anfrage_ = urllib.request.Request(f"{ursprung}{pfad}", data=inhalt, headers=koepfe, method=methode)
    oeffner = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with oeffner.open(anfrage_, timeout=warten) as antwort:
            return antwort.status, json.loads(antwort.read().decode("utf-8"))
    except urllib.error.HTTPError as fehler:
        try:
            rumpf = json.loads(fehler.read().decode("utf-8"))
        except (OSError, ValueError):
            rumpf = {}
        return fehler.code, rumpf if isinstance(rumpf, dict) else {}
    except (OSError, ValueError) as fehler:
        grund = fehler.reason if isinstance(fehler, urllib.error.URLError) else fehler
        if zeit_melden and isinstance(grund, (TimeoutError, socket.timeout)):
            raise Zeitueberschreitung() from fehler
        return None


class Sitzung:
    def __init__(self, aufruf, sage, steuere):
        self.aufruf = aufruf
        self.sage = sage
        self.steuere = steuere
        self.ordner = einrichtung.datenordner(aufruf.paket, aufruf.daten) / "werkstatt"

    # ------------------------------------------------------------ Server

    def zustand(self, kennung: str) -> dict | None:
        """Die Zustand-Datei eines lebenden Servers; ist sein Prozess weg, None (der Port könnte jetzt einem anderen
        Programm gehören, das den Schlüssel nicht bekommen soll)."""
        zustand = lies_zustand(self.ordner / f"{kennung}.json", mit_pid=True)
        return zustand if zustand is not None and lebt(zustand["pid"]) else None

    @staticmethod
    def post(zustand: dict, pfad: str, daten: dict, warten: float | None = None) -> tuple[int, dict] | None:
        """(Status, JSON) oder None, wenn der Server nicht antwortet (Ende); eine Zeitüberschreitung (der Server lebt,
        ist aber beschäftigt) wirft Zeitueberschreitung. Gewartet wird ANTWORT_WARTEN_S, wenn warten fehlt."""
        return anfrage(zustand, "POST", pfad, daten, warten=ANTWORT_WARTEN_S if warten is None else warten,
                       zeit_melden=True)

    # ------------------------------------------------------------ Ausgaben

    def ende(self) -> int:
        self.sage(BEENDET)
        self.sage(texte.WERKSTATT_SITZUNG_ENDE)
        self.steuere([f"{STEUER}ende"])
        return 0

    def weiter_warten(self) -> int:
        self.steuere([f"{STEUER}warte"])
        return 0

    def runde(self, kennung: str, zustand: dict, auftrag: object) -> bool:
        """Die vier Steuerzeilen einer Runde, nur aus geprüften Werten (WS-K4)."""
        if not isinstance(auftrag, dict):
            return False
        aid, agent, dateien = auftrag.get("id"), auftrag.get("agent"), auftrag.get("dateien")
        if not isinstance(aid, str) or not AUFTRAG_ID.fullmatch(aid) or agent not in AGENTEN \
                or not isinstance(dateien, dict):
            return False
        datei, antwort = dateien.get("auftrag"), dateien.get("antwort")
        ergebnis = zustand.get("ergebnis")
        if not eigener_pfad(datei, aid, "auftrag.json", ergebnis) or not eigener_pfad(antwort, aid, "antwort", ergebnis):
            return False
        self.steuere([f"{STEUER}auftrag: {datei}", f"{STEUER}agent: atlas:{agent}", f"{STEUER}antwort: {antwort}",
                      f"{STEUER}id: {kennung}.{aid}"])
        return True

    def abgeschlossen(self, aid: str, antwort: dict) -> int:
        if antwort.get("zustand") == "abgebrochen":
            self.sage(ABGEBROCHEN)
        elif antwort.get("zustand") == "rueckfrage":
            self.sage(f"Auftrag {aid} wartet auf die Antwort des Nutzers.")
        elif antwort.get("zustand") == "offen":
            self.sage(f"Auftrag {aid} läuft noch nicht.")
        elif antwort.get("zustand") == "entwurf":
            self.sage(f"Der Entwurf zu Auftrag {aid} liegt beim Nutzer.")
        else:
            self.sage(f"Auftrag {aid} ist schon abgeschlossen.")
        return self.weiter_warten()

    # ------------------------------------------------------------ Befehle

    def warte(self, argumente: list[str]) -> int:
        angabe = argumente[0] if argumente else ""
        treffer = WERKSTATT.fullmatch(angabe)
        if treffer is None:
            self.sage(keine_werkstatt(angabe))
            return 1
        kennung, sitzung = treffer.groups()
        gesamt = float(os.environ.get("ATLAS_KIT_WARTE_S", WARTE_GESAMT_S) or WARTE_GESAMT_S)
        beginn = time.monotonic()
        ende, schluss = beginn + gesamt, beginn + BEFEHL_HOECHSTENS_S
        while True:
            zustand = self.zustand(kennung)
            if zustand is None:
                return self.ende()
            rest = ende - time.monotonic()
            if rest <= 0:
                return self.weiter_warten()
            stueck = min(STUECK_S, rest)
            try:
                antwort = self.post(zustand, "/api/auftraege/warte", {"sekunden": stueck, "sitzung": sitzung},
                                    warten=max(1.0, min(stueck + ANTWORT_WARTEN_S, schluss - time.monotonic())))
            except Zeitueberschreitung:
                # Der Server legt gerade eine Kopie an oder ist beschäftigt; ein Auftrag kommt beim nächsten warte.
                return self.weiter_warten()
            if antwort is not None and antwort[0] == 200 and antwort[1].get("abgeloest"):
                self.sage(ABGELOEST)
                self.sage(texte.WERKSTATT_SITZUNG_ENDE)
                self.steuere([f"{STEUER}ende"])
                return 0
            if antwort is None or antwort[0] != 200 or antwort[1].get("ende"):
                return self.ende()
            auftrag = antwort[1].get("auftrag")
            if auftrag is None:
                continue
            aid = str(auftrag.get("id", ""))
            if not AUFTRAG_ID.fullmatch(aid):
                return self.ende()
            if auftrag.get("anlass") == "antwort":
                self.sage(f"Antwort auf die Rückfrage zu Auftrag {aid} ist da.")
            elif auftrag.get("anlass") == "versuch":
                self.sage(f"Die Werkstatt hat die Antwort zu Auftrag {aid} nicht angenommen; der Agent bekommt einen "
                          f"zweiten Versuch.")
            elif auftrag.get("anlass") == "nachricht":
                self.sage(f"Nachricht zum Entwurf von Auftrag {aid} ist da; der Planer macht eine neue Fassung.")
            else:
                self.sage(f"Auftrag {aid} aus der Werkstatt: {ART_TEXT.get(str(auftrag.get('art')), '?')}.")
            if not self.runde(kennung, zustand, auftrag):
                return self.ende()
            return 0

    def _bezug(self, argumente: list[str]) -> tuple[str, str, dict] | None:
        bezug = argumente[0] if argumente else ""
        treffer = BEZUG.fullmatch(bezug)
        if treffer is None:
            self.sage(kein_auftrag(bezug))
            return None
        kennung, aid = treffer.groups()
        zustand = self.zustand(kennung)
        return kennung, aid, zustand if zustand is not None else {}

    def _antwort_auswerten(self, kennung: str, aid: str, antwort: tuple[int, dict] | None, erfolg) -> int:
        if antwort is None:
            return self.ende()
        status, daten = antwort
        if status == 409:
            return self.abgeschlossen(aid, daten)
        if status == 404:
            self.sage(f"Auftrag {aid} gibt es in der Werkstatt nicht.")
            return self.weiter_warten()
        if status != 200:
            self.sage(f"Die Werkstatt hat abgelehnt: {daten.get('fehler', status)}")
            return self.weiter_warten()
        return erfolg(daten)

    def fortschritt(self, argumente: list[str]) -> int:
        gefunden = self._bezug(argumente)
        if gefunden is None:
            return 1
        kennung, aid, zustand = gefunden
        if not zustand:
            return self.ende()
        zeile = ableiten.einzeilig(argumente[1] if len(argumente) > 1 else "", ZEILE_HOECHSTENS)
        if not zeile:
            return 0
        try:
            antwort = self.post(zustand, "/api/auftraege/fortschritt", {"id": aid, "zeile": zeile})
        except Zeitueberschreitung:
            return 0  # die Fortschrittszeile fehlt dann nur; der Agent kann starten
        return self._antwort_auswerten(kennung, aid, antwort, lambda _daten: 0)

    def ergebnis(self, argumente: list[str], nur_frage: bool = False) -> int:
        gefunden = self._bezug(argumente)
        if gefunden is None:
            return 1
        kennung, aid, zustand = gefunden
        if not zustand:
            return self.ende()

        def erfolg(daten: dict) -> int:
            art = daten.get("zustand")
            if art == "fertig" and "auswertung" in daten:
                self.sage(auswertung_satz(aid, daten.get("auswertung")))
            elif art == "fertig" and "vorschlaege" in daten:
                self.sage(gutachten_satz(aid, daten.get("vorschlaege"), daten.get("verworfen")))
            elif art == "fertig":
                self.sage(f"Vorschlag zu Auftrag {aid} liegt in der Werkstatt zur Vorschau.")
            elif art == "rueckfrage":
                self.sage(f"Rückfrage zu Auftrag {aid} steht in der Werkstatt; die Antwort kommt über warte.")
            elif art == "entwurf" and daten.get("grund"):
                self.sage(f"Die Runde zu Auftrag {aid} brachte keine neue Fassung: {daten['grund']}")
            elif art == "entwurf":
                fassung = daten.get("fassung")
                nr = f" (Fassung {fassung})" if isinstance(fassung, int) and not isinstance(fassung, bool) else ""
                self.sage(f"Entwurf zu Auftrag {aid} liegt in der Werkstatt{nr}; nachbessern oder übernehmen geht "
                          f"in der Seite.")
            elif art == "laeuft":
                meldungen = daten.get("meldungen") or ["?"]
                self.sage(f"Die Werkstatt hat die Antwort zu Auftrag {aid} nicht angenommen: {meldungen[0]}")
                self.sage("Der Agent bekommt einen zweiten Versuch.")
                if self.runde(kennung, zustand, daten.get("auftrag")):
                    return 0
                return self.ende()
            else:
                self.sage(f"Auftrag {aid} ist fehlgeschlagen: {daten.get('grund', '?')}")
            return self.weiter_warten()

        daten = {"id": aid, "nur_frage": nur_frage}
        tokens = argumente[1].strip() if len(argumente) > 1 else ""
        if TOKENS.fullmatch(tokens) and int(tokens) > 0:
            daten["tokens"] = int(tokens)
        try:
            antwort = self.post(zustand, "/api/auftraege/ergebnis", daten)
        except Zeitueberschreitung:
            # Der Server lebt und prüft noch: das ist kein Ende der Werkstatt, und der Auftrag bleibt (WS-K4).
            self.sage(nicht_geprueft(aid, ANTWORT_WARTEN_S))
            return self.weiter_warten()
        return self._antwort_auswerten(kennung, aid, antwort, erfolg)

    def abbruch(self, argumente: list[str]) -> int:
        gefunden = self._bezug(argumente)
        if gefunden is None:
            return 1
        kennung, aid, zustand = gefunden
        if not zustand:
            return self.ende()
        # Den Grund liest der Server aus der Antwortdatei, nie aus einem Shell-Argument.
        daten = {"id": aid, "zustand": "fehlgeschlagen"}

        def erfolg(daten_: dict) -> int:
            auftrag = daten_.get("auftrag") if isinstance(daten_.get("auftrag"), dict) else {}
            if auftrag.get("zustand") == "entwurf":  # WS-KE7: Folgerunde fällt auf die letzte Fassung zurück
                self.sage(f"Die Runde zu Auftrag {aid} brachte keine neue Fassung: {auftrag.get('grund', '?')}")
            else:
                self.sage(f"Auftrag {aid} als fehlgeschlagen gemeldet.")
            return self.weiter_warten()

        try:
            antwort = self.post(zustand, "/api/auftraege/zustand", daten)
        except Zeitueberschreitung:
            self.sage(keine_antwort(aid, ANTWORT_WARTEN_S))
            return self.weiter_warten()
        return self._antwort_auswerten(kennung, aid, antwort, erfolg)


def fuehre_aus(aufruf, sage, steuere, argumente: list[str]) -> int:
    sitzung = Sitzung(aufruf, sage, steuere)
    befehl, rest = argumente[0], argumente[1:]
    if befehl == "warte":
        return sitzung.warte(rest)
    if befehl == "fortschritt":
        return sitzung.fortschritt(rest)
    if befehl == "ergebnis":
        return sitzung.ergebnis(rest)
    if befehl == "frage":
        return sitzung.ergebnis(rest, nur_frage=True)
    return sitzung.abbruch(rest)
