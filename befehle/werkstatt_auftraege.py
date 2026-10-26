"""Warteschlange der Aufträge an die KI (specs/werkstatt.yaml WS-K1, WS-K2, WS-K4, WS-K5, WS-K8).

Die KI arbeitet in der laufenden Claude-Code-Sitzung des Nutzers; sie holt Aufträge mit warte ab. Hier stehen nur
die Zustände und ihre Übergänge, unter einer Condition, damit warte blockieren kann:

    offen → laeuft → rueckfrage ⇄ laeuft → fertig | fehlgeschlagen
    offen | laeuft | rueckfrage → abgebrochen (Seite);  laeuft | rueckfrage → fehlgeschlagen (Sitzung, Prüfung)
    entwerfen (Ticket 17): laeuft → entwurf → (Nachricht) laeuft → entwurf … → fertig (Übernahme) | abgebrochen

Dateien (Arbeitskopie, Auftragsdatei) und die Prüfung eingereichter Sätze macht werkstatt_ki.py, die Auswahl beim
Einbauen eines Gutachtens werkstatt_gutachten.py.
"""
from __future__ import annotations

import copy
import re
import secrets
import threading
import time
from typing import Callable

import ableiten
from aenderungssatz import NAME
from vokabular import SCHRITT_ID
import werkstatt_auswertung as auswertung_form
import werkstatt_gutachten as gutachten_form
from werkstatt_entwurf import teil_ids

OFFEN, LAEUFT, RUECKFRAGE = "offen", "laeuft", "rueckfrage"
FERTIG, ABGEBROCHEN, FEHLGESCHLAGEN = "fertig", "abgebrochen", "fehlgeschlagen"
ENDZUSTAENDE = (FERTIG, ABGEBROCHEN, FEHLGESCHLAGEN)
# art → Agent des Pakets (WS-K2, WS-K11, WS-K16)
ARTEN = {"aendern": "umbauer", "bewerten": "gutachter", "einbauen": "umbauer", "neu": "pipeline-planer"}
# Ticket 17: neue Pipeline mit Dialog; die Antwort ist ein Entwurf (werkstatt_entwurf.py), kein Satz.
ENTWERFEN = "entwerfen"
ARTEN[ENTWERFEN] = "pipeline-planer"
ENTWURF = "entwurf"
AUSWERTEN = "auswerten"
ARTEN[AUSWERTEN] = "auswerter"
AUSWERTUNG_GIBT_ES_NICHT = "Diese Auswertung gibt es nicht."
KEINE_FERTIGE_AUSWERTUNG = "Dieser Auftrag hat keine fertige Auswertung."
ENTWEDER_ODER = "Nenne entweder gutachten oder auswertung."


def laeuft_schon(skill: str) -> str:
    return f"Für {skill} läuft schon eine Auswertung."


def skill_aus_bezug(bezug: object) -> str | None:
    if isinstance(bezug, str) and bezug.startswith("Ablauf ") and NAME.fullmatch(bezug[len("Ablauf "):]):
        return bezug[len("Ablauf "):]
    return None
FASSUNGEN_HOECHSTENS = 10
NACHRICHTEN_HOECHSTENS = 2 * FASSUNGEN_HOECHSTENS  # auch Folgerunden ohne neue Fassung zählen
ENTWUERFE_HOECHSTENS = 10  # liegende Entwürfe; sie zählen nicht zu AKTIV_HOECHSTENS
ENTWURF_FRIST_S = 24 * 60 * 60
ZU_VIELE_FASSUNGEN = (f"Höchstens {FASSUNGEN_HOECHSTENS} Fassungen und {NACHRICHTEN_HOECHSTENS} Nachrichten je Auftrag; "
                      f"übernimm oder beginne neu.")
ENTWURF_VERFALLEN = "Der Entwurf lag 24 Stunden ohne Nachricht und ohne Übernahme; die Werkstatt hat ihn verworfen."
ZU_VIELE_ENTWUERFE = (f"Es liegen schon {ENTWUERFE_HOECHSTENS} Entwürfe in der Werkstatt; übernimm oder verwirf zuerst "
                      f"einen.")
TEXT_HOECHSTENS = 20000
ZUSATZ_HOECHSTENS = 2000
BEZUG_HOECHSTENS = 200
AKTIV_HOECHSTENS = 20
GEMERKT_HOECHSTENS = 100
ZEILEN_HOECHSTENS = 200
RUECKFRAGEN_HOECHSTENS = 3
VERSUCHE = 2
ANTWORT_IN_ZEILE = 120

AUFGEGEBEN = "Die Claude-Code-Sitzung hat den Auftrag nicht zu Ende gebracht."
ABGESCHLOSSEN = "Der Auftrag ist schon abgeschlossen."
RUECKFRAGE_SATZ = "Die KI hat eine Rückfrage"
ZU_VIELE_RUECKFRAGEN = f"Höchstens {RUECKFRAGEN_HOECHSTENS} Rückfragen je Auftrag; liefere jetzt einen Satz."
ZU_VIELE_RUECKFRAGEN_ENTWURF = f"Höchstens {RUECKFRAGEN_HOECHSTENS} Rückfragen je Fassung; liefere jetzt einen Entwurf."
ENDE = "ende"
ABGELOEST = "abgeloest"
SITZUNG_ID = re.compile(r"^s[0-9a-f]{8}$")
ZIEL_UNGUELTIG = "ziel ist ungültig."
AUFTRAG_FRIST_S = 60 * 60
ZURUECK_OFFEN = "Die Claude-Code-Sitzung hat sich nicht mehr gemeldet; der Auftrag wartet wieder auf eine Sitzung."
UEBERNOMMEN = "Eine neue Claude-Code-Sitzung hat übernommen; der Auftrag beginnt dort von vorn."
# Dieselben Anlässe bei einem Entwurf mit Fassungen: dann gilt die letzte Fassung weiter (WS-KE7).
STILL_GRUND = "Die Claude-Code-Sitzung hat sich nicht mehr gemeldet"
UEBERNOMMEN_GRUND = "Eine neue Claude-Code-Sitzung hat übernommen"


def zweimal_abgelehnt(meldung: str) -> str:
    return f"Die Werkstatt hat die Antwort zweimal nicht angenommen: {meldung}"


def pruefe_ziel(ziel: object) -> dict | None:
    """WS-M2: fehlt (None) oder {skill?, schritt? (nur mit skill), agent?}, mindestens eins; Namen wie im
    Änderungssatz (WS-F3), nur die Form."""
    if ziel is None:
        return None
    muster = {"skill": NAME, "schritt": SCHRITT_ID, "agent": NAME}
    if (not isinstance(ziel, dict) or not ziel or set(ziel) - set(muster)
            or not all(isinstance(w, str) and muster[k].fullmatch(w) for k, w in ziel.items())
            or ("schritt" in ziel and "skill" not in ziel)):
        raise AuftragFehler(400, ZIEL_UNGUELTIG)
    return {k: ziel[k] for k in muster if k in ziel}


class AuftragFehler(Exception):
    """status für HTTP; zustand nennt den Zustand eines abgeschlossenen Auftrags (für die Sätze der Sitzung)."""

    def __init__(self, status: int, text: str, zustand: str | None = None):
        super().__init__(text)
        self.status = status
        self.text = text
        self.zustand = zustand


def _eine_zeile(text: str) -> bool:
    return "\n" not in text and "\r" not in text


class Auftraege:
    def __init__(self, uhr: Callable[[], float] = time.monotonic) -> None:
        self._liste: list[dict] = []
        self._nummer = 0
        self._kennung = secrets.token_hex(3)
        self._uhr = uhr
        self.bedingung = threading.Condition()
        self.beendet = False
        # Sitzungs-Lease (WS-K4): genau eine aktive Sitzung; jede warte-Anfrage zählt die Generation
        # hoch, ältere Wartende nehmen danach keinen Auftrag mehr an.
        self.aktive_sitzung: str | None = None
        self.abgeloest: set[str] = set()
        self.generation = 0
        # Ruft der Server bei jeder Änderung (Ereignis 'auftrag', WS-K1); läuft unter der Sperre, darf nicht blockieren.
        self.bei_aenderung: Callable[[str], None] | None = None

    # ------------------------------------------------------------ intern

    @staticmethod
    def _jetzt() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _geaendert(self, auftrag: dict) -> None:
        self.bedingung.notify_all()
        if self.bei_aenderung is not None:
            self.bei_aenderung(auftrag["id"])

    def _finde(self, aid: object) -> dict:
        for auftrag in self._liste:
            if auftrag["id"] == aid:
                return auftrag
        raise AuftragFehler(404, "Diesen Auftrag gibt es nicht.")

    def _zeile(self, auftrag: dict, text: str, detail: str | None = None) -> None:
        """Eine Fortschrittszeile; detail ist der technische Grund einer Ablehnung, den die Seite aufklappbar zeigt."""
        if len(auftrag["zeilen"]) < ZEILEN_HOECHSTENS:
            zeile = {"zeit": self._jetzt(), "text": text}
            if detail and detail != text:
                zeile["detail"] = detail[:600]
            auftrag["zeilen"].append(zeile)

    def _beende(self, auftrag: dict, zustand: str, grund: str | None = None) -> None:
        if zustand == FEHLGESCHLAGEN and auftrag["fassungen"]:
            self._zurueck_zum_entwurf(auftrag, grund or AUFGEGEBEN)
            return
        auftrag["zustand"] = zustand
        auftrag["grund"] = grund
        auftrag["antwort"] = None
        if auftrag["abgeholt"] is not None:
            auftrag["dauer_s"] = int(round(self._uhr() - auftrag["abgeholt"]))
        self._geaendert(auftrag)

    @staticmethod
    def _nicht_abgeschlossen(auftrag: dict) -> None:
        if auftrag["zustand"] in ENDZUSTAENDE:
            raise AuftragFehler(409, ABGESCHLOSSEN, auftrag["zustand"])

    def _laufend(self, aid: object) -> dict:
        auftrag = self._finde(aid)
        self._nicht_abgeschlossen(auftrag)
        if auftrag["zustand"] != LAEUFT:
            raise AuftragFehler(409, "Der Auftrag läuft gerade nicht.", auftrag["zustand"])
        return auftrag

    @staticmethod
    def oeffentlich(auftrag: dict) -> dict:
        """Was die Seite sieht (WS-K2): keine Pfade, keine Antwortdatei."""
        frage = auftrag["frage"] if auftrag["zustand"] == RUECKFRAGE and auftrag["antwort"] is None else None
        return {"id": auftrag["id"], "art": auftrag["art"], "text": auftrag["text"], "bezug": auftrag["bezug"],
                "zustand": auftrag["zustand"], "zeit": auftrag["zeit"], "satz": auftrag["satz"],
                "eintraege": auftrag["eintraege"], "zeilen": copy.deepcopy(auftrag["zeilen"]),
                "frage": copy.deepcopy(frage), "dauer_s": auftrag["dauer_s"], "tokens": auftrag["tokens"],
                "grund": auftrag["grund"], "gutachten": Auftraege._gutachten_kurz(auftrag["gutachten"]),
                "einbauen": copy.deepcopy(auftrag["einbauen"]),
                "auswertung": Auftraege._auswertung_kurz(auftrag["auswertung"]), "laeufe": auftrag["laeufe"],
                **Auftraege._entwurf_oeffentlich(auftrag)}

    @staticmethod
    def _auswertung_kurz(auswertung: dict | None) -> dict | None:
        if auswertung is None:
            return None
        return {"bewertungen": len(auswertung["bewertungen"]), "vorschlaege": len(auswertung["vorschlaege"]),
                "gedaechtnis": len(auswertung["gedaechtnis"]), "verworfen": auswertung["verworfen"]}

    @staticmethod
    def _gutachten_kurz(gutachten: dict | None) -> dict | None:
        """In der Liste nur Zahlen; das ganze Gutachten liefert gutachten() (WS-K15)."""
        if gutachten is None:
            return None
        return {"vorschlaege": len(gutachten["vorschlaege"]), "verworfen": gutachten["verworfen"]}

    # ------------------------------------------------------------ Seite

    def voll(self) -> bool:
        with self.bedingung:
            return sum(1 for a in self._liste if a["zustand"] not in (*ENDZUSTAENDE, ENTWURF)) >= AKTIV_HOECHSTENS

    def stelle_ein(self, art: object, text: object, bezug: object = None, gutachten: object = None,
                   auswahl: object = None, *, ziel: object = None, auswertung: object = None) -> dict:
        if not isinstance(art, str) or art not in ARTEN:
            raise AuftragFehler(400, "art ist ungültig.")
        if art == "einbauen":
            if text is not None and (not isinstance(text, str) or len(text) > ZUSATZ_HOECHSTENS):
                raise AuftragFehler(400, f"text hat beim Einbauen höchstens {ZUSATZ_HOECHSTENS} Zeichen.")
        elif not isinstance(text, str) or not text.strip() or len(text) > TEXT_HOECHSTENS:
            raise AuftragFehler(400, f"text fehlt oder hat mehr als {TEXT_HOECHSTENS} Zeichen.")
        if art != "einbauen" and (gutachten is not None or auswahl is not None or auswertung is not None):
            raise AuftragFehler(400, "gutachten und auswahl gibt es nur beim Einbauen.")
        if art == "einbauen" and (gutachten is None) == (auswertung is None):
            raise AuftragFehler(400, ENTWEDER_ODER)
        if bezug is not None and (not isinstance(bezug, str) or not bezug.strip() or len(bezug) > BEZUG_HOECHSTENS
                                  or not _eine_zeile(bezug)):
            raise AuftragFehler(400, f"bezug ist eine Zeile mit höchstens {BEZUG_HOECHSTENS} Zeichen.")
        ziel = pruefe_ziel(ziel)
        with self.bedingung:
            if sum(1 for a in self._liste if a["zustand"] not in (*ENDZUSTAENDE, ENTWURF)) >= AKTIV_HOECHSTENS:
                raise AuftragFehler(409, "Es warten schon zu viele Aufträge.")
            if art == ENTWERFEN and sum(1 for a in self._liste if a["zustand"] == ENTWURF) >= ENTWUERFE_HOECHSTENS:
                raise AuftragFehler(409, ZU_VIELE_ENTWUERFE)
            if art == AUSWERTEN and self._offene_auswertung(bezug) is not None:
                raise AuftragFehler(409, laeuft_schon(skill_aus_bezug(bezug) or str(bezug)))
            einbauen = vorschlaege = None
            zusatz = text.strip() if isinstance(text, str) and text.strip() else None
            if art == "einbauen" and auswertung is not None:
                vorschlaege = self._aus_auswertung(auswertung, auswahl)
                einbauen = {"gutachten": None, "auswertung": auswertung, "auswahl": [v["id"] for v in vorschlaege]}
                text = auswertung_form.auftragstext(vorschlaege, zusatz)
            elif art == "einbauen":
                vorschlaege = self._gewaehlte(gutachten, auswahl)
                einbauen = {"gutachten": gutachten, "auswahl": [v["id"] for v in vorschlaege]}
                text = gutachten_form.auftragstext(vorschlaege, text.strip() if isinstance(text, str) else None)
            self._nummer += 1
            auftrag = {"id": f"a{self._nummer}-{self._kennung}", "art": art, "text": text, "bezug": bezug,
                       "zustand": OFFEN, "zeit": self._jetzt(), "satz": None, "eintraege": None, "zeilen": [],
                       "frage": None, "antwort": None, "verlauf": [], "rueckfragen": 0, "runde": 0, "versuch": 0,
                       "pruefung": [], "abgeholt": None, "dauer_s": None, "tokens": None, "grund": None,
                       "gutachten": None, "einbauen": einbauen, "vorschlaege": vorschlaege,
                       "sitzung": None, "kontakt": None, "ziel": ziel}
            auftrag.update(fassungen=[], entwurf=None, nachricht=None, fassung_beginn=None, tokens_beginn=0,
                           frage_seit=None, wartezeit=0.0,
                           entwurf_seit=None)
            # Arbeit des Servers für die Sitzung (Anlegen der Kopie, Prüfung einer Antwort): solange sie läuft, gilt
            # der Auftrag beim nächsten warte nicht als aufgegeben, sondern als verwaist und wird danach nachgereicht.
            auftrag.update(in_arbeit=0, verwaist=False, nachreichen=None)
            auftrag.update(auswertung=None, auswertung_intern=None, laeufe=None)
            self._liste.append(auftrag)
            fertige = [a for a in self._liste if a["zustand"] in ENDZUSTAENDE]
            for alt in fertige[:max(0, len(self._liste) - GEMERKT_HOECHSTENS)]:
                self._liste.remove(alt)
            self._geaendert(auftrag)
            return self.oeffentlich(auftrag)

    def _gewaehlte(self, gutachten: object, auswahl: object) -> list[dict]:
        """WS-K16: Kopie der gewählten Vorschläge eines fertigen Gutachtens (unter der Sperre)."""
        if not isinstance(gutachten, str):
            raise AuftragFehler(400, "gutachten nennt den Auftrag mit dem Gutachten.")
        quelle = self._finde(gutachten)
        if quelle["art"] != "bewerten":
            raise AuftragFehler(404, "Dieses Gutachten gibt es nicht.")
        if quelle["zustand"] != FERTIG or quelle["gutachten"] is None:
            raise AuftragFehler(409, "Dieser Auftrag hat kein fertiges Gutachten.", quelle["zustand"])
        try:
            return copy.deepcopy(gutachten_form.pruefe_auswahl(quelle["gutachten"], auswahl))
        except ValueError as fehler:
            raise AuftragFehler(400, str(fehler))

    def _offene_auswertung(self, bezug: object) -> str | None:
        return next((a["id"] for a in self._liste if a["art"] == AUSWERTEN and a["bezug"] == bezug
                     and a["zustand"] not in ENDZUSTAENDE), None)

    def offene_auswertung(self, skill: str) -> str | None:
        with self.bedingung:
            return self._offene_auswertung(f"Ablauf {skill}")

    def _fertige_auswertung(self, aid: object) -> dict:
        try:
            auftrag = self._finde(aid)
        except AuftragFehler:
            raise AuftragFehler(404, AUSWERTUNG_GIBT_ES_NICHT)
        if auftrag["art"] != AUSWERTEN:
            raise AuftragFehler(404, AUSWERTUNG_GIBT_ES_NICHT)
        if auftrag["zustand"] != FERTIG or auftrag["auswertung"] is None:
            raise AuftragFehler(409, KEINE_FERTIGE_AUSWERTUNG, auftrag["zustand"])
        return auftrag

    def _aus_auswertung(self, auswertung: object, auswahl: object) -> list[dict]:
        quelle = self._fertige_auswertung(auswertung)
        try:
            return auswertung_form.pruefe_auswahl(quelle["auswertung"], auswahl)
        except ValueError as fehler:
            raise AuftragFehler(400, str(fehler))

    def auswertung(self, aid: object) -> dict:
        with self.bedingung:
            return copy.deepcopy(self._fertige_auswertung(aid)["auswertung"])

    def auswertung_mit_intern(self, aid: object) -> tuple[dict, dict]:
        with self.bedingung:
            auftrag = self._fertige_auswertung(aid)
            return copy.deepcopy(auftrag["auswertung"]), copy.deepcopy(auftrag["auswertung_intern"])

    def setze_laeufe(self, aid: object, anzahl: int) -> None:
        with self.bedingung:
            auftrag = self._finde(aid)
            auftrag["laeufe"] = anzahl
            self._geaendert(auftrag)

    def fertig_auswertung(self, aid: object, auswertung: dict, intern: dict, zeile: str) -> dict:
        with self.bedingung:
            auftrag = self._laufend(aid)
            auftrag.update(auswertung=copy.deepcopy(auswertung), auswertung_intern=copy.deepcopy(intern), pruefung=[])
            self._zeile(auftrag, zeile)
            self._beende(auftrag, FERTIG)
            return self.oeffentlich(auftrag)

    def gutachten(self, aid: object) -> dict:
        with self.bedingung:
            auftrag = self._finde(aid)
            if auftrag["gutachten"] is None:
                raise AuftragFehler(409, "Dieser Auftrag hat kein fertiges Gutachten.", auftrag["zustand"])
            return copy.deepcopy(auftrag["gutachten"])

    def liste(self) -> list[dict]:
        with self.bedingung:
            return [self.oeffentlich(a) for a in self._liste]

    def laeufe(self) -> dict[str, dict]:
        """WS-K12: Dauer und Tokens je Vorschlag der KI, ohne die öffentliche Form aller Aufträge zu bauen."""
        with self.bedingung:
            return {a["satz"]: {"dauer_s": a["dauer_s"], "tokens": a["tokens"]} for a in self._liste if a["satz"]}

    def brich_ab(self, aid: object) -> dict:
        with self.bedingung:
            auftrag = self._finde(aid)
            self._nicht_abgeschlossen(auftrag)
            self._beende(auftrag, ABGEBROCHEN)
            return self.oeffentlich(auftrag)

    def beantworte(self, aid: object, antwort: str) -> dict:
        with self.bedingung:
            auftrag = self._finde(aid)
            self._nicht_abgeschlossen(auftrag)
            if auftrag["zustand"] != RUECKFRAGE or auftrag["antwort"] is not None:
                raise AuftragFehler(409, "Dieser Auftrag wartet auf keine Antwort.", auftrag["zustand"])
            auftrag["antwort"] = antwort
            einzeilig = ableiten.einzeilig(antwort, ANTWORT_IN_ZEILE)
            self._zeile(auftrag, f"Antwort: {einzeilig}")
            self._geaendert(auftrag)
            return self.oeffentlich(auftrag)

    # ------------------------------------------------------------ Sitzung

    def warte(self, sitzung: object, sekunden: float) -> tuple[str, dict] | str | None:
        """WS-K4, WS-KE5: ('antwort' | 'nachricht' | 'neu' | 'versuch', Auftrag), ENDE, ABGELOEST oder None nach Ablauf.

        Eine neue Sitzungs-ID übernimmt die Werkstatt; die alte gilt danach dauerhaft als abgelöst. Jede warte-Anfrage
        erhöht die Generation; ältere Wartende kehren beim Aufwachen ohne Auftrag zurück. Laufende Aufträge der eigenen
        Sitzung gelten beim Eintritt als aufgegeben, nicht bei jedem Aufwachen – außer der Server arbeitet gerade für
        die Sitzung daran (in_arbeit): dann hat sie nur nicht auf seine Antwort gewartet; der Auftrag ist verwaist und
        kommt, wenn die Arbeit fertig ist und er noch läuft, nachgereicht (Kopie mit 'nachgereicht': Anlass). Jeder
        ausgegebene Auftrag ist in_arbeit, bis der Aufrufer ende_arbeit meldet.
        """
        if not isinstance(sitzung, str) or not SITZUNG_ID.fullmatch(sitzung):
            raise AuftragFehler(400, "sitzung fehlt oder ist ungültig.")
        ende = self._uhr() + sekunden
        with self.bedingung:
            if sitzung in self.abgeloest:
                return ABGELOEST
            if sitzung != self.aktive_sitzung:
                alte = self.aktive_sitzung
                if alte is not None:
                    self.abgeloest.add(alte)
                    # Laufende Aufträge der abgelösten Sitzung beginnen bei der neuen von vorn
                    # (Arbeitskopie neu); Rückfragen bleiben und gehen mit der Antwort an die neue Sitzung.
                    for auftrag in self._liste:
                        if auftrag["zustand"] == LAEUFT and auftrag["sitzung"] == alte:
                            self._zurueck_offen(auftrag, UEBERNOMMEN, UEBERNOMMEN_GRUND)
                self.aktive_sitzung = sitzung
            self.generation += 1
            meine = self.generation
            self.bedingung.notify_all()
            for auftrag in self._liste:
                if auftrag["zustand"] == LAEUFT and auftrag["sitzung"] == sitzung:
                    if auftrag["in_arbeit"] or auftrag["nachreichen"]:
                        auftrag["verwaist"] = True
                    else:
                        self._beende(auftrag, FEHLGESCHLAGEN, AUFGEGEBEN)
            while True:
                if self.beendet:
                    return ENDE
                if self.generation != meine:
                    return ABGELOEST if sitzung != self.aktive_sitzung else None
                for auftrag in self._liste:
                    if auftrag["zustand"] == LAEUFT and auftrag["sitzung"] == sitzung and auftrag["nachreichen"] \
                            and not auftrag["in_arbeit"]:
                        anlass = auftrag["nachreichen"]
                        auftrag.update(nachreichen=None, verwaist=False, kontakt=self._uhr(), in_arbeit=1)
                        return anlass, {**copy.deepcopy(auftrag), "nachgereicht": True}
                for auftrag in self._liste:
                    if auftrag["zustand"] == RUECKFRAGE and auftrag["antwort"] is not None:
                        auftrag["verlauf"].append({"frage": auftrag["frage"], "antwort": auftrag["antwort"]})
                        if auftrag["frage_seit"] is not None:  # Zeit beim Nutzer zählt nicht als Rechenzeit
                            auftrag["wartezeit"] += self._uhr() - auftrag["frage_seit"]
                        auftrag.update(zustand=LAEUFT, frage=None, antwort=None, pruefung=[], versuch=1,
                                       runde=auftrag["runde"] + 1, sitzung=sitzung, kontakt=self._uhr(),
                                       in_arbeit=auftrag["in_arbeit"] + 1)
                        self._geaendert(auftrag)
                        return "antwort", copy.deepcopy(auftrag)
                    if auftrag["zustand"] == ENTWURF and auftrag["nachricht"] is not None:
                        return "nachricht", self._nimm_nachricht(auftrag, sitzung)
                if not any(a["zustand"] in (LAEUFT, RUECKFRAGE) for a in self._liste):
                    for auftrag in self._liste:
                        if auftrag["zustand"] == OFFEN:
                            jetzt = self._uhr()
                            auftrag.update(zustand=LAEUFT, runde=1, versuch=1, sitzung=sitzung, kontakt=jetzt,
                                           abgeholt=auftrag["abgeholt"] if auftrag["abgeholt"] is not None else jetzt,
                                           in_arbeit=auftrag["in_arbeit"] + 1)
                            self._geaendert(auftrag)
                            return "neu", copy.deepcopy(auftrag)
                rest = ende - self._uhr()
                if rest <= 0:
                    return None
                self.bedingung.wait(rest)

    def ende_arbeit(self, aid: object, anlass: str) -> bool:
        """Die Arbeit des Servers für die Sitzung an diesem Auftrag ist fertig. True, wenn die Sitzung inzwischen neu
        wartet (verwaist): dann bekommt sie den Auftrag, falls er noch läuft, mit anlass über warte nachgereicht, und
        der Aufrufer gibt ihn nicht mehr aus."""
        with self.bedingung:
            try:
                auftrag = self._finde(aid)
            except AuftragFehler:
                return False
            auftrag["in_arbeit"] = max(0, auftrag["in_arbeit"] - 1)
            verwaist = auftrag["verwaist"]
            if not auftrag["in_arbeit"]:
                if verwaist and auftrag["zustand"] == LAEUFT:
                    auftrag["nachreichen"] = anlass
                auftrag["verwaist"] = False
            self.bedingung.notify_all()
            return verwaist

    def _zurueck_offen(self, auftrag: dict, zeile: str, grund: str) -> None:
        """Ein laufender Auftrag ohne Sitzung beginnt von vorn; hat er schon Fassungen (entwerfen), gilt wie bei einer
        gescheiterten Folgerunde die letzte Fassung weiter (WS-KE7), mit grund in der Zeile."""
        auftrag.update(verwaist=False, nachreichen=None)
        if auftrag["fassungen"]:
            self._zurueck_zum_entwurf(auftrag, grund)
            return
        auftrag.update(zustand=OFFEN, runde=0, versuch=0, pruefung=[], sitzung=None, kontakt=None)
        self._zeile(auftrag, zeile)
        self._geaendert(auftrag)

    def pruefe_fristen(self, frist: float = AUFTRAG_FRIST_S) -> list[str]:
        """Ein laufender Auftrag, zu dem die Sitzung sich länger als frist nicht gemeldet hat, wird
        wieder offen (statt ewig 'laeuft'). Rückfragen warten auf den Nutzer und haben keine Frist."""
        zurueck = []
        with self.bedingung:
            jetzt = self._uhr()
            for auftrag in self._liste:
                if auftrag["zustand"] == LAEUFT and auftrag["kontakt"] is not None and jetzt - auftrag["kontakt"] > frist:
                    self._zurueck_offen(auftrag, ZURUECK_OFFEN, STILL_GRUND)
                    zurueck.append(auftrag["id"])
                elif auftrag["zustand"] == ENTWURF and auftrag["nachricht"] is None \
                        and jetzt - auftrag["entwurf_seit"] > ENTWURF_FRIST_S:
                    self._beende(auftrag, ABGEBROCHEN, ENTWURF_VERFALLEN)  # WS-KE4: liegende Entwürfe verfallen
                    zurueck.append(auftrag["id"])
        return zurueck

    def beende_alle(self) -> None:
        with self.bedingung:
            self.beendet = True
            self.bedingung.notify_all()

    def hole(self, aid: object) -> dict:
        with self.bedingung:
            return copy.deepcopy(self._finde(aid))

    def laufend(self, aid: object, arbeit: bool = False) -> dict:
        """Der laufende Auftrag; mit arbeit beginnt die Arbeit des Servers daran (danach ende_arbeit)."""
        with self.bedingung:
            auftrag = self._laufend(aid)
            auftrag["kontakt"] = self._uhr()
            if arbeit:
                auftrag["in_arbeit"] += 1
            return copy.deepcopy(auftrag)

    def zeile(self, aid: object, text: str, detail: str | None = None) -> None:
        with self.bedingung:
            auftrag = self._finde(aid)
            if auftrag["zustand"] in ENDZUSTAENDE:
                return
            self._zeile(auftrag, text, detail)
            self._geaendert(auftrag)

    def zaehle_tokens(self, aid: object, tokens: object) -> None:
        """WS-K12: Tokens des Agenten, wie die Sitzung sie aus dessen Ergebnis meldet; je Runde aufaddiert."""
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 < tokens < 10 ** 9:
            return
        with self.bedingung:
            auftrag = self._laufend(aid)
            auftrag["tokens"] = (auftrag["tokens"] or 0) + tokens

    def fortschritt(self, aid: object, text: str) -> None:
        with self.bedingung:
            auftrag = self._laufend(aid)
            auftrag["kontakt"] = self._uhr()
            self._zeile(auftrag, text)
            self._geaendert(auftrag)

    def frage(self, aid: object, frage: dict) -> bool:
        """laeuft → rueckfrage; False bei zu vielen Rückfragen (dann entscheidet die Prüfung über den Versuch)."""
        with self.bedingung:
            auftrag = self._laufend(aid)
            if auftrag["rueckfragen"] >= RUECKFRAGEN_HOECHSTENS:
                return False
            auftrag.update(zustand=RUECKFRAGE, frage=copy.deepcopy(frage), antwort=None,
                           rueckfragen=auftrag["rueckfragen"] + 1, frage_seit=self._uhr())
            self._zeile(auftrag, RUECKFRAGE_SATZ)
            self._geaendert(auftrag)
            return True

    def naechster_versuch(self, aid: object, meldungen: list[str], alltag: str | None = None) -> dict | None:
        """WS-K7: erster Versuch abgelehnt → neue Runde (Kopie des Auftrags); zweiter → fehlgeschlagen (None).
        Die Zeile nennt den Grund in Alltagssprache (alltag), den technischen als detail; die KI bekommt ihn in pruefung."""
        with self.bedingung:
            auftrag = self._laufend(aid)
            if auftrag["versuch"] < VERSUCHE:
                auftrag.update(versuch=auftrag["versuch"] + 1, runde=auftrag["runde"] + 1, pruefung=list(meldungen),
                               kontakt=self._uhr())
                grund = alltag or meldungen[0]
                grund = grund if grund.rstrip().endswith((".", "!", "?")) else f"{grund.rstrip()}."
                self._zeile(auftrag, f"Nicht angenommen: {grund} Die KI bekommt einen zweiten Versuch."[:300],
                            meldungen[0] if grund.rstrip(".") != meldungen[0].rstrip(".") else None)
                self._geaendert(auftrag)
                return copy.deepcopy(auftrag)
            self._beende(auftrag, FEHLGESCHLAGEN, zweimal_abgelehnt(meldungen[0]))
            return None

    def fertig(self, aid: object, vorschlag: str, eintraege: int, zeile: str) -> dict:
        with self.bedingung:
            auftrag = self._laufend(aid)
            auftrag.update(satz=vorschlag, eintraege=eintraege, pruefung=[])
            self._zeile(auftrag, zeile)
            self._beende(auftrag, FERTIG)
            return self.oeffentlich(auftrag)

    def fertig_gutachten(self, aid: object, gutachten: dict, zeile: str) -> dict:
        """WS-K15: ein Gutachten statt eines Vorschlags; satz bleibt null."""
        with self.bedingung:
            auftrag = self._laufend(aid)
            auftrag.update(gutachten=copy.deepcopy(gutachten), pruefung=[])
            self._zeile(auftrag, zeile)
            self._beende(auftrag, FERTIG)
            return self.oeffentlich(auftrag)

    def scheitere(self, aid: object, grund: str) -> dict:
        with self.bedingung:
            auftrag = self._finde(aid)
            self._nicht_abgeschlossen(auftrag)
            if auftrag["zustand"] == OFFEN:
                raise AuftragFehler(409, "Der Auftrag läuft noch nicht.", OFFEN)
            if auftrag["zustand"] == ENTWURF:
                raise AuftragFehler(409, "Der Auftrag läuft gerade nicht.", ENTWURF)
            self._beende(auftrag, FEHLGESCHLAGEN, grund)
            return self.oeffentlich(auftrag)

    def zustaende(self) -> dict[str, str]:
        with self.bedingung:
            return {a["id"]: a["zustand"] for a in self._liste}

    def in_arbeit(self) -> set[str]:
        """Aufträge, an denen der Server gerade für die Sitzung arbeitet (ausgegeben, bis ende_arbeit)."""
        with self.bedingung:
            return {a["id"] for a in self._liste if a["in_arbeit"]}

    # ------------------------------------------------------------ Entwürfe (Ticket 17, WS-KE)

    @staticmethod
    def _entwurf_oeffentlich(auftrag: dict) -> dict:
        """Fassungen ohne Entwurf, den letzten Entwurf ganz und das Gespräch in Reihenfolge (für den Chat der Seite);
        bei anderen Arten leer (WS-KE4). nachricht_wartet: eine Nachricht ist geschickt, aber noch nicht abgeholt."""
        if auftrag["art"] != ENTWERFEN:
            return {"fassungen": [], "entwurf": None, "gespraech": [], "nachricht_wartet": False}
        fassungen = [{**{k: f[k] for k in ("nr", "nachricht", "zusammenfassung", "zeit", "dauer_s", "tokens")},
                      "teile": teil_ids(f["entwurf"])} for f in auftrag["fassungen"]]
        gespraech: list[dict] = []
        nach_index: dict[int, list[dict]] = {}
        for f in auftrag["fassungen"]:
            nach_index.setdefault(f["verlauf_index"], []).append(f)
        for i, eintrag in enumerate([*auftrag["verlauf"], None]):
            for f in nach_index.get(i, []):
                gespraech.append({"wer": "ki", "text": f["zusammenfassung"], "fassung": f["nr"],
                                  "dauer_s": f["dauer_s"], "tokens": f["tokens"]})
            if eintrag is None:
                break
            if "nachricht" in eintrag:
                gespraech.append({"wer": "du", "text": eintrag["nachricht"]})
            elif "keine_fassung" in eintrag:
                gespraech.append({"wer": "ki", "text": eintrag["keine_fassung"], "keine_fassung": True})
            else:
                gespraech.append({"wer": "ki", "text": eintrag["frage"]["text"], "frage": True})
                gespraech.append({"wer": "du", "text": eintrag["antwort"]})
        if auftrag["nachricht"] is not None:
            gespraech.append({"wer": "du", "text": auftrag["nachricht"]})
        return {"fassungen": fassungen, "entwurf": copy.deepcopy(auftrag["entwurf"]), "gespraech": gespraech,
                "nachricht_wartet": auftrag["nachricht"] is not None}

    def _nimm_nachricht(self, auftrag: dict, sitzung: str) -> dict:
        """entwurf mit Nachricht → laeuft, neue Runde; die Nachricht steht danach im verlauf (WS-KE5)."""
        auftrag["verlauf"].append({"nachricht": auftrag["nachricht"], "fassung": len(auftrag["fassungen"])})
        jetzt = self._uhr()
        auftrag.update(zustand=LAEUFT, nachricht=None, grund=None, pruefung=[], versuch=1, rueckfragen=0,
                       runde=auftrag["runde"] + 1, sitzung=sitzung, kontakt=jetzt, fassung_beginn=jetzt,
                       tokens_beginn=auftrag["tokens"] or 0, wartezeit=0.0, frage_seit=None,
                       in_arbeit=auftrag["in_arbeit"] + 1)
        self._geaendert(auftrag)
        return copy.deepcopy(auftrag)

    def _zurueck_zum_entwurf(self, auftrag: dict, grund: str) -> None:
        """WS-KE7: Scheitert eine Folgerunde, gilt die letzte Fassung weiter."""
        nr = len(auftrag["fassungen"])
        # Die gescheiterte Runde bleibt im Gespräch sichtbar, auch nach der nächsten Nachricht (WS-KE13).
        auftrag["verlauf"].append({"keine_fassung": grund, "fassung": nr})
        auftrag.update(zustand=ENTWURF, grund=grund, frage=None, antwort=None, pruefung=[], sitzung=None,
                       kontakt=None, entwurf_seit=self._uhr())
        self._zeile(auftrag, f"Keine neue Fassung: {grund}; Fassung {nr} gilt weiter."[:300])
        self._geaendert(auftrag)

    def entwurf_fertig(self, aid: object, entwurf: dict) -> int:
        """laeuft → entwurf mit einer neuen Fassung; gibt ihre Nummer zurück."""
        with self.bedingung:
            auftrag = self._laufend(aid)
            jetzt = self._uhr()
            beginn = auftrag["fassung_beginn"] if auftrag["fassung_beginn"] is not None else auftrag["abgeholt"]
            tokens = (auftrag["tokens"] or 0) - auftrag["tokens_beginn"]
            nr = len(auftrag["fassungen"]) + 1
            nachricht = next((v["nachricht"] for v in reversed(auftrag["verlauf"])
                              if "nachricht" in v and v["fassung"] == nr - 1), None)
            auftrag["fassungen"].append({
                "nr": nr, "nachricht": nachricht, "zusammenfassung": entwurf["zusammenfassung"], "zeit": self._jetzt(),
                "dauer_s": int(round(jetzt - beginn - auftrag["wartezeit"])) if beginn is not None else None,
                "tokens": tokens or None,
                "verlauf_index": len(auftrag["verlauf"]), "entwurf": copy.deepcopy(entwurf)})
            auftrag.update(zustand=ENTWURF, entwurf=copy.deepcopy(entwurf), grund=None, pruefung=[], sitzung=None,
                           kontakt=None, entwurf_seit=jetzt)
            self._zeile(auftrag, f"Fassung {nr} liegt in der Werkstatt")
            self._geaendert(auftrag)
            return nr

    def nachricht(self, aid: object, text: str) -> dict:
        """WS-KE5: Nachricht des Nutzers zur letzten Fassung; warte gibt den Auftrag danach als neue Runde aus."""
        with self.bedingung:
            auftrag = self._finde(aid)
            self._nicht_abgeschlossen(auftrag)
            if auftrag["zustand"] != ENTWURF or auftrag["nachricht"] is not None:
                raise AuftragFehler(409, "Dieser Auftrag wartet auf keine Nachricht.", auftrag["zustand"])
            if len(auftrag["fassungen"]) >= FASSUNGEN_HOECHSTENS \
                    or sum(1 for v in auftrag["verlauf"] if "nachricht" in v) >= NACHRICHTEN_HOECHSTENS:
                raise AuftragFehler(409, ZU_VIELE_FASSUNGEN, ENTWURF)
            auftrag["nachricht"] = text
            einzeilig = ableiten.einzeilig(text, ANTWORT_IN_ZEILE)
            self._zeile(auftrag, f"Nachricht: {einzeilig}")
            self._geaendert(auftrag)
            return self.oeffentlich(auftrag)

    def zur_uebernahme(self, aid: object, fassung: object) -> dict:
        """Der letzte Entwurf, wenn der Auftrag im Zustand entwurf ist und fassung die letzte Fassung nennt."""
        with self.bedingung:
            auftrag = self._finde(aid)
            self._nicht_abgeschlossen(auftrag)
            if auftrag["zustand"] != ENTWURF:
                raise AuftragFehler(409, "Der Planer arbeitet gerade an diesem Auftrag; übernehmen geht danach.",
                                    auftrag["zustand"])
            if isinstance(fassung, bool) or fassung != len(auftrag["fassungen"]):
                raise AuftragFehler(409, f"Fassung {fassung} ist nicht mehr die letzte; übernehmen geht nur mit "
                                         f"Fassung {len(auftrag['fassungen'])}.", ENTWURF)
            return copy.deepcopy(auftrag["entwurf"])

    def uebernommen(self, aid: object, fassung: int, vorschlag: str, eintraege: int, zeile: str) -> dict:
        """entwurf → fertig mit dem Vorschlag aus der Übernahme (WS-KE6)."""
        with self.bedingung:
            auftrag = self._finde(aid)
            self._nicht_abgeschlossen(auftrag)
            if auftrag["zustand"] != ENTWURF or fassung != len(auftrag["fassungen"]):
                raise AuftragFehler(409, "Der Entwurf hat sich inzwischen geändert.", auftrag["zustand"])
            auftrag.update(satz=vorschlag, eintraege=eintraege, nachricht=None)
            self._zeile(auftrag, zeile)
            self._beende(auftrag, FERTIG)
            # WS-KE6: gezählt wird die Rechenzeit der Runden, nicht die Zeit beim Nutzer
            auftrag["dauer_s"] = sum(f["dauer_s"] or 0 for f in auftrag["fassungen"])
            return self.oeffentlich(auftrag)
