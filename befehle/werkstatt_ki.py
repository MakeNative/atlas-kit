"""KI-Aufträge der Werkstatt: Arbeitskopie, Auftragsdatei, Antwort lesen und prüfen (specs/werkstatt.yaml WS-K3,
WS-K4, WS-K6, WS-K7, WS-K15, WS-K16).

Die KI arbeitet in der Claude-Code-Sitzung des Nutzers, nie hier. Der Server legt je Auftrag eine Arbeitskopie an
(genau das, was der Scanner liest) und eine Auftragsdatei, die der Agent liest. Die Antwort des Agenten schreibt die
Sitzung in eine Datei im Auftragsordner; der Server liest sie selbst (kein Symlink, höchstens 1 MiB) und prüft sie nur
als Daten: Form, Freigabeliste, Probelauf mit Scan, Belege neuer Schritte. Erst dann wird sie ein Vorschlag, und
angewandt wird sie nur aus der Seite. Beim Bewerten ist die Antwort ein Gutachten: jeder Vorschlag braucht einen Beleg in
der Arbeitskopie (so, wie der Server sie angelegt hat), und seine Skizze besteht dieselbe Prüfung wie ein Satz, sonst fällt nur er weg.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import ableiten
import aenderungssatz
import plattform
import scanner_lauf
import werkstatt_ablage as ablage_modul
import werkstatt_auswertung as auswertung_form
import werkstatt_entwurf
import werkstatt_gutachten as gutachten_form
import werkstatt_kennzahlen
import werkstatt_pfade as pfade
from werkstatt_auftraege import (ABGELOEST, ARTEN, AUFTRAG_FRIST_S, ENDE, ENDZUSTAENDE, LAEUFT, OFFEN, RUECKFRAGE,
                                 ZU_VIELE_RUECKFRAGEN, AuftragFehler, Auftraege)
from werkstatt_auftraege import AUSWERTEN, ENTWERFEN, ENTWURF, ZU_VIELE_RUECKFRAGEN_ENTWURF, skill_aus_bezug
from werkstatt_kern import KI as HERKUNFT_KI
from werkstatt_kern import Werkstatt, WerkstattFehler, _rest, zeit_um_beim_anlegen

SCHEMA = "atlas-werkstatt-auftrag/0.1"
ANTWORT_HOECHSTENS = 1024 * 1024
FRAGE_HOECHSTENS = 500
OPTIONEN_HOECHSTENS = 6
OPTION_HOECHSTENS = 200
MELDUNG_HOECHSTENS = 300
ORDNER_AUFTRAEGE = "auftraege"
ORDNER_KOPIE = "kopie"

KOPIE_SATZ = "Kopie des Kits angelegt, deine Dateien bleiben unberührt"
KOPIE_NEU = "Das Kit hat sich seit der Rückfrage geändert: Kopie neu angelegt"
PRUEFT = "Prüft den Vorschlag: Form, Probelauf in einer Kopie, Hinweise, Belege"
GEPRUEFT = "Vorschlag geprüft: kein neuer Hinweis, jeder neue Schritt hat einen Beleg"
PRUEFT_GUTACHTEN = "Prüft das Gutachten: Belege in der Kopie, jede Skizze im Probelauf"
KEINE_FRAGE = "Erwartet war eine Rückfrage (frage)."
KEINE_AENDERUNG = "Der Satz ändert keine Datei."
ZU_GROSS = "Die Antwort ist größer als 1 MiB."
FEHLT = "Die Antwortdatei fehlt."
NICHT_REGULAER = "Die Antwortdatei ist keine reguläre Datei."
OHNE_GRUND = "Die Claude-Code-Sitzung hat den Auftrag abgebrochen."
PRUEFT_ENTWURF = "Prüft den Entwurf: Form, vorhandene Agenten, Probelauf in einer Kopie, Hinweise, Belege"
AUSWAHL_PASST_NICHT = "Die Auswahl passt nicht zusammen."
AUSWAHL_ABGELEHNT = "Die Werkstatt hat die Auswahl nicht angenommen."
# Schlüssel der Antwort je Art (WS-K6, WS-K15, WS-KE2); frage geht immer
ERGEBNIS_SCHLUESSEL = {"bewerten": "gutachten", ENTWERFEN: "entwurf", AUSWERTEN: "auswertung"}
PRUEFT_AUSWERTUNG = "Prüft die Auswertung: Belege gegen die Läufe, jede Skizze im Probelauf"
MEMORY_LOGS_PFLICHT = "memory und logs müssen wahr oder falsch sein."
ARBEITSDATEN_FEHLEN = "Die Arbeitsdaten dieser Auswertung fehlen; stell den Auftrag noch einmal."


def ergebnis_schluessel(art: str) -> str:
    return ERGEBNIS_SCHLUESSEL.get(art, "satz")


def kein_block(art: str) -> str:
    return f"Die Antwort enthält keinen JSON-Block mit genau einem Schlüssel {ergebnis_schluessel(art)} oder frage."


def nicht_kopiert(dateien: int, ordner: int) -> str:
    teile = ([("1 Datei" if dateien == 1 else f"{dateien} Dateien")] if dateien else []) \
        + ([f"{ordner} Ordner"] if ordner else [])
    return (f"{' und '.join(teile)} nicht in der Kopie: Symlink, zu groß, nicht lesbar oder über der Grenze "
            f"(steht in der Auftragsdatei)")


def gezaehlt(uebersprungen: list[dict]) -> tuple[int, int]:
    """(Dateien, Ordner) unter uebersprungen nach dem Typ, den der Eintrag beim Vermerken bekam; ein Sammel-Eintrag
    zählt mit seiner anzahl."""
    ordner = sum(1 for u in uebersprungen if u["typ"] == pfade.ORDNER)
    return sum(u.get("anzahl", 1) for u in uebersprungen if u["typ"] != pfade.ORDNER), ordner


def auswertung_geprueft(zahlen: dict) -> str:
    satz = (f"Auswertung geprüft: {zahlen['bewertungen']} Bewertungen, {zahlen['vorschlaege']} Vorschläge, "
            f"{zahlen['gedaechtnis']} Memory-Einträge")
    return f"{satz}, {zahlen['verworfen']} verworfen" if zahlen["verworfen"] else satz


def kopiert(laeufe: int, dateien: int) -> str:
    return f"Kopiert {laeufe} Läufe und {dateien} Memory-Dateien"


def gutachten_geprueft(gueltig: int, verworfen: int) -> str:
    satz = f"Gutachten geprüft: {gueltig} Vorschläge mit Beleg"
    return f"{satz}, {verworfen} verworfen" if verworfen else satz



HINWEIS_FUER_MENSCHEN = "Einfügen geht hier mit KI: „Mit KI einfügen“ am Kasten oder Pfeil."
HINWEIS_FUER_DIE_KI = ("Füge den Schritt mit nur_ableitung (und belege) ein und schreib den Text an der Stelle mit "
                       "text_ersetzen um.")


def fuer_ki(text: str) -> str:
    """Ablehnungsgründe gehen an die KI zurück; ein Hinweis, der für Menschen in der Seite gedacht ist, wird für sie
    umgeschrieben (manueller Test 24.10.2026)."""
    return text.replace(HINWEIS_FUER_MENSCHEN, HINWEIS_FUER_DIE_KI)

ALLTAG_FORM = "Die Antwort der KI war nicht so aufgebaut, wie die Werkstatt sie braucht."
ALLTAG_VORSCHLAG_FORM = "Der Vorschlag war nicht so aufgebaut, wie die Werkstatt ihn braucht."
ALLTAG_EINTRAG = "Eine der Änderungen ließ sich so nicht anwenden."
ALLTAG_HINWEIS = "Die Änderung hätte neue Hinweise zum Bauplan ergeben."
ALLTAG_BELEG = "Der Beleg passt nicht zum Text der Datei."
ALLTAG_OHNE_BELEG = "Ein neuer Schritt hätte keine Belegstelle im Text."
ALLTAG_NICHTS = "Die Änderung hätte keine Datei geändert."
# Meldungen über einzelne Felder (Gutachten, Frage, Entwurf): „feld: …“, „feld.unterfeld …“, „erwartet …“, „id … doppelt“
_FELD_MELDUNG = re.compile(r"^(?:[a-z_]+(?:\.[a-z_]+)*(?::| steht | kommt )|erwartet |id \S+ kommt doppelt)")


def in_alltag(meldung: str) -> str:
    """Ablehnungsgrund für die Fortschrittszeile in der Seite (Feinschliff 24.10.2026): technische Gründe (Form,
    Einträge, Felder, Belege) in Alltagssprache; was schon Alltagssprache ist, bleibt. Die KI bekommt weiter den
    technischen Grund, die Seite zeigt ihn aufklappbar."""
    if meldung.startswith("Form: ") or meldung.startswith("Die Antwort enthält keinen JSON-Block"):
        return ALLTAG_FORM
    if meldung.startswith("Eintrag "):
        return ALLTAG_EINTRAG
    if meldung.startswith("Neuer Hinweis in "):
        return ALLTAG_HINWEIS
    if meldung.startswith("beleg"):
        return ALLTAG_BELEG
    if _FELD_MELDUNG.match(meldung):
        return ALLTAG_VORSCHLAG_FORM
    if meldung.startswith("Schritt „") and meldung.endswith(" hat keinen Beleg."):
        return ALLTAG_OHNE_BELEG
    if meldung == KEINE_AENDERUNG:
        return ALLTAG_NICHTS
    return meldung


TEXT_HOECHSTENS = 2000  # Antwort auf eine Rückfrage, Nachricht im Gespräch über einen Entwurf


def text_fehler(name: str) -> str:
    return f"{name} hat 1 bis {TEXT_HOECHSTENS} Zeichen."


def pruefe_text(text: object, name: str) -> str:
    """Text des Nutzers an die KI: 1 bis TEXT_HOECHSTENS Zeichen, keine Steuerzeichen außer Zeilenumbruch und Tab."""
    if (not isinstance(text, str) or not text.strip() or len(text) > TEXT_HOECHSTENS
            or any(ableiten.ist_steuerzeichen(z) for z in text if z not in "\n\t")):
        raise AuftragFehler(400, text_fehler(name))
    return text


class AntwortFehler(Exception):
    pass


einzeilig = ableiten.einzeilig  # Meldungen höchstens MELDUNG_HOECHSTENS Zeichen


def schritte_ohne_beleg(vorher: dict, nachher: dict) -> list[str]:
    """WS-K7: Jeder Schritt im Bauplan danach, den es vorher nicht gab, braucht beleg mit datei, zeile und zitat."""
    alte = {k.get("id") for k in vorher.get("knoten", []) if isinstance(k, dict) and k.get("typ") == "schritt"}
    meldungen = []
    for knoten in nachher.get("knoten", []):
        if not isinstance(knoten, dict) or knoten.get("typ") != "schritt" or knoten.get("id") in alte:
            continue
        beleg = knoten.get("beleg")
        if (isinstance(beleg, dict) and isinstance(beleg.get("datei"), str) and isinstance(beleg.get("zeile"), int)
                and isinstance(beleg.get("zitat"), str) and beleg["zitat"].strip()):
            continue
        skill = str(knoten.get("skill", "")).removeprefix("skill:")
        meldungen.append(f"Schritt „{knoten.get('titel')}“ in {skill} hat keinen Beleg.")
    return meldungen


def lies_antwort(pfad: Path) -> bytes:
    """Liest die Antwortdatei ohne einem Symlink zu folgen, nur als reguläre Datei, höchstens 1 MiB."""
    try:
        return pfade.lies_regulaer(pfad, ANTWORT_HOECHSTENS)
    except pfade.LeseFehler as fehler:
        raise AntwortFehler({pfade.LeseFehler.FEHLT: FEHLT, pfade.LeseFehler.ZU_GROSS: ZU_GROSS}.get(fehler.grund,
                                                                                                    NICHT_REGULAER))


def pruefe_frage(frage: object) -> list[str]:
    if not isinstance(frage, dict) or set(frage) - {"text", "optionen"}:
        return ["frage: erwartet ein Objekt mit text und optionen."]
    meldungen = []
    text = frage.get("text")
    if (not isinstance(text, str) or not text.strip() or len(text) > FRAGE_HOECHSTENS
            or any(ableiten.ist_steuerzeichen(z) for z in text)):
        meldungen.append(f"frage.text: eine Zeile mit 1 bis {FRAGE_HOECHSTENS} Zeichen.")
    optionen = frage.get("optionen", [])
    if (not isinstance(optionen, list) or len(optionen) > OPTIONEN_HOECHSTENS
            or not all(isinstance(o, str) and o.strip() and len(o) <= OPTION_HOECHSTENS
                       and not any(ableiten.ist_steuerzeichen(z) for z in o) for o in optionen)):
        meldungen.append(f"frage.optionen: höchstens {OPTIONEN_HOECHSTENS} Texte, je eine Zeile mit 1 bis "
                         f"{OPTION_HOECHSTENS} Zeichen.")
    return meldungen


class KI:
    def __init__(self, werkstatt: Werkstatt):
        self.werkstatt = werkstatt
        self.auftraege: Auftraege = werkstatt.auftraege
        self.basis = Path(os.path.realpath(werkstatt.ergebnis)) / ablage_modul.ORDNER
        self._mit_ordnern: set[str] = set()
        self._uebersprungen: dict[str, list[dict]] = {}  # je Auftrag: nicht in die Arbeitskopie übernommen
        self._kit_stand: dict[str, tuple] = {}  # je Auftrag: Signatur des Kits beim Anlegen der Arbeitskopie
        self._kopie_stand: dict[str, dict[str, str]] = {}  # je Auftrag: sha256 je Datei der Arbeitskopie beim Anlegen
        self._auswerten_felder: dict[str, dict] = {}
        self._sperre = threading.Lock()
        self.auswerten = None

    # ------------------------------------------------------------ Ordner

    def _ordner(self, aid: str) -> tuple[Path, Path]:
        return self.basis / ORDNER_AUFTRAEGE / aid, self.basis / ORDNER_KOPIE / aid

    def _auftrag_datei(self, aid: str) -> Path:
        return self._ordner(aid)[0] / "auftrag.json"

    def _antwort_datei(self, aid: str, runde: int) -> Path:
        return self._ordner(aid)[0] / f"antwort-{runde}.txt"

    def _auftragsordner(self, aid: str, art: str = ORDNER_AUFTRAEGE) -> Path:
        """Die einzige Stelle, die Ordner eines Auftrags anlegt (sicherer Weg, kein Symlink); sie merkt den Auftrag
        vorher für raeume_auf, so bleibt auch ein halb angelegter Ordner nicht liegen."""
        with self._sperre:
            self._mit_ordnern.add(aid)
        return pfade.sicherer_ordner(ablage_modul.werkstatt_ordner(Path(os.path.realpath(self.werkstatt.ergebnis))),
                                     art, aid)

    def _lege_an(self, auftrag: dict, ende: float) -> None:
        """Arbeitskopie, Bauplan und Auftragsdatei (WS-K3); Sperre und Einlesen höchstens bis ende."""
        aid = auftrag["id"]
        with self._sperre:
            eigene = aid in self._mit_ordnern  # Ordner, die dieser Server schon angelegt hat (Übernahme)
        for vorhanden in self._ordner(aid):
            if os.path.lexists(vorhanden):
                if not eigene or plattform.ist_verweis(vorhanden) or not vorhanden.is_dir():
                    raise pfade.PfadFehler(f"{vorhanden} gibt es schon; die Werkstatt legt dort nichts ab.")
                shutil.rmtree(vorhanden)  # alte Arbeitskopie dieses Servers (Übernahme)
        ordner = self._auftragsordner(aid)
        kopie = self._auftragsordner(aid, ORDNER_KOPIE)
        for pfad in (self.basis, ordner.parent, ordner, kopie.parent, kopie):
            os.chmod(pfad, 0o700)
        stand = self.werkstatt.signatur()  # vor dem Kopieren: eine Änderung währenddessen gilt als neuer Stand
        with self._sperre:
            self._kit_stand[aid] = stand
        skill_ordner = self.werkstatt.ordner.skill_ordner()  # einmal: für die Dateiliste und die Begleitdateien
        uebersprungen = pfade.kopiere_quellen(self.werkstatt.kit, self.werkstatt.quellen(skill_ordner), kopie,
                                              fuer_ki=True)
        pfade.kopiere_begleitdateien(self.werkstatt.kit, skill_ordner, kopie, uebersprungen)
        kopie_stand = gutachten_form.stand_der_kopie(kopie)
        with self._sperre:
            self._uebersprungen[aid] = uebersprungen
            self._kopie_stand[aid] = kopie_stand
        try:
            bauplan = self.werkstatt.bauplan(laufzeit=ende - time.monotonic())
        except WerkstattFehler as fehler:
            if fehler.status == 503:
                raise WerkstattFehler(503, {"fehler": zeit_um_beim_anlegen()})
            raise
        (ordner / "bauplan.json").write_bytes(json.dumps(bauplan, ensure_ascii=False, indent=1).encode("utf-8"))
        ableitung = self.werkstatt._ableitung()
        if ableitung is not None:
            (ordner / "ableitung.json").write_bytes(ableiten.als_text(ableitung))
        if auftrag["art"] == AUSWERTEN and self.auswerten is not None:
            felder = self.auswerten.lege_arbeitsdaten_an(aid, ordner, skill_aus_bezug(auftrag["bezug"]) or "", ende,
                                                         kopie, kopie_stand)
            with self._sperre:
                self._uebersprungen[aid] = uebersprungen = uebersprungen + felder.pop("uebersprungen")
                self._auswerten_felder[aid] = felder
            self.auftraege.setze_laeufe(aid, len(felder["laeufe"]))
            self.auftraege.zeile(aid, kopiert(len(felder["laeufe"]), sum(len(g["dateien"]) for g in felder["gedaechtnis"])))
        self._schreibe_auftrag(auftrag)
        if uebersprungen:
            self.auftraege.zeile(aid, nicht_kopiert(*gezaehlt(uebersprungen)))

    def _schreibe_auftrag(self, auftrag: dict) -> None:
        aid = auftrag["id"]
        ordner, kopie = self._ordner(aid)
        ableitung = ordner / "ableitung.json"
        with self._sperre:
            uebersprungen = list(self._uebersprungen.get(aid, []))
            auswerten = dict(self._auswerten_felder.get(aid, {}))
        daten = {
            "schema": SCHEMA, "id": aid, "art": auftrag["art"], "runde": auftrag["runde"],
            "auftrag": auftrag["text"], "bezug": auftrag["bezug"],
            "kit": {"id": self.werkstatt.kit_id, "art": self.werkstatt.art, "skills": self.werkstatt.ordner.skills,
                    "agenten": self.werkstatt.ordner.agenten},
            "kopie": str(kopie), "bauplan": str(ordner / "bauplan.json"),
            "ableitung": str(ableitung) if ableitung.is_file() else None,
            "ziel": auftrag.get("ziel"),
            "verlauf": auftrag["verlauf"], "pruefung": auftrag["pruefung"],
            "vorschlaege": auftrag.get("vorschlaege"),
            "uebersprungen": uebersprungen,
            **({"entwurf": auftrag["entwurf"]} if auftrag["art"] == ENTWERFEN else {}),
            **auswerten,
        }
        ziel = self._auftragsordner(aid) / "auftrag.json"
        if plattform.ist_verweis(ziel):
            raise pfade.PfadFehler(f"{ziel} ist ein Symlink.")
        ziel.write_bytes((json.dumps(daten, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        # Brach die Seite währenddessen ab, gehört der Ordner sofort wieder weg.
        self.raeume_auf()

    def raeume_auf(self, alle: bool = False) -> None:
        """Entfernt Auftragsordner und Arbeitskopie abgeschlossener Aufträge (bei alle: aller dieses Servers).

        Die Zustände liest es erst unter der eigenen Sperre: ein Auftrag, den warte gerade ausgibt, ist dann entweder
        noch nicht in _mit_ordnern oder schon laeuft, seine frische Arbeitskopie bleibt. Ein Auftrag in Arbeit bleibt
        gemerkt, auch wenn seine Ordner schon weg sind: das Anlegen kann sie noch einmal erzeugen."""
        with self._sperre:
            zustaende = self.auftraege.zustaende()
            in_arbeit = self.auftraege.in_arbeit()
            weg = [aid for aid in self._mit_ordnern
                   if alle or zustaende.get(aid) in (*ENDZUSTAENDE, OFFEN, ENTWURF) or aid not in zustaende]
            for aid in weg:
                for ordner in self._ordner(aid):
                    # nur echte Ordner auf echtem Weg: weder der Ordner noch einer darüber bis zum Ergebnisordner
                    # ist ein Symlink, sonst bleibt er liegen
                    weg_dahin = [ordner, ordner.parent, self.basis]
                    if ordner.is_dir() and not any(plattform.ist_verweis(p) for p in weg_dahin) \
                            and os.path.realpath(ordner) == str(ordner):
                        shutil.rmtree(ordner, ignore_errors=True)
                if alle or aid not in in_arbeit and not any(os.path.lexists(o) for o in self._ordner(aid)):
                    self._mit_ordnern.discard(aid)
                    self._uebersprungen.pop(aid, None)
                    self._kit_stand.pop(aid, None)
                    self._kopie_stand.pop(aid, None)
                    self._auswerten_felder.pop(aid, None)
                    if self.auswerten is not None:
                        self.auswerten.vergiss(aid)

    # ------------------------------------------------------------ Antworten an die Sitzung

    def _fuer_sitzung(self, auftrag: dict, anlass: str) -> dict:
        return {"id": auftrag["id"], "art": auftrag["art"], "agent": ARTEN[auftrag["art"]], "runde": auftrag["runde"],
                "anlass": anlass, "dateien": {"auftrag": str(self._auftrag_datei(auftrag["id"])),
                                              "antwort": str(self._antwort_datei(auftrag["id"], auftrag["runde"]))}}

    def warte(self, sitzung: object, sekunden: float) -> dict:
        """WS-K4: wartet höchstens sekunden auf einen Auftrag; Anlegen der Kopie (Sperre, Einlesen) danach höchstens
        PRUEFUNG_HOECHSTENS_S. Fragt die Sitzung inzwischen neu (sie hat nicht auf diese Antwort gewartet), gibt
        diese Anfrage nichts aus; der Auftrag kommt bei ihr nachgereicht."""
        arbeit_ende = None
        while True:
            ergebnis = self.auftraege.warte(sitzung, sekunden)
            self.raeume_auf()
            if ergebnis == ENDE:
                return {"ende": True}
            if ergebnis == ABGELOEST:
                return {"abgeloest": True}
            if ergebnis is None:
                return {"auftrag": None}
            anlass, auftrag = ergebnis
            if arbeit_ende is None:
                arbeit_ende = time.monotonic() + scanner_lauf.PRUEFUNG_HOECHSTENS_S
            try:
                bereit = self._bereite_vor(anlass, auftrag, arbeit_ende)
            finally:
                verwaist = self.auftraege.ende_arbeit(auftrag["id"], anlass)
            if bereit and not verwaist and self.auftraege.hole(auftrag["id"])["zustand"] not in ENDZUSTAENDE:
                return {"auftrag": self._fuer_sitzung(auftrag, anlass)}
            self.raeume_auf()  # gescheitert oder während des Anlegens abgebrochen: nicht ausgeben, weiterwarten
            if verwaist or time.monotonic() >= arbeit_ende:
                return {"auftrag": None}
            sekunden = 0

    def _bereite_vor(self, anlass: str, auftrag: dict, ende: float) -> bool:
        """Ordner und Auftragsdatei für die Ausgabe; False, wenn der Auftrag daran gescheitert ist."""
        try:
            if auftrag.get("nachgereicht"):
                return True  # Kopie und Auftragsdatei stehen schon
            if anlass in ("neu", "nachricht"):
                self._lege_an(auftrag, ende)
                self.auftraege.zeile(auftrag["id"], KOPIE_SATZ)
            elif anlass == "antwort" and self._kit_geaendert(auftrag["id"]):
                # Während der Rückfrage hat sich das Kit geändert: Der Agent arbeitet auf dem Stand jetzt (WS-K4).
                self._lege_an(auftrag, ende)
                self.auftraege.zeile(auftrag["id"], KOPIE_NEU)
            else:
                self._schreibe_auftrag(auftrag)
            return True
        except (OSError, pfade.PfadFehler, pfade.KopieFehler, WerkstattFehler) as fehler:
            text = fehler.daten.get("fehler") if isinstance(fehler, WerkstattFehler) else str(fehler)
            try:
                self.auftraege.scheitere(auftrag["id"], einzeilig(text))
            except AuftragFehler:
                pass  # inzwischen in der Seite abgebrochen
            return False

    def _kit_geaendert(self, aid: str) -> bool:
        with self._sperre:
            stand = self._kit_stand.get(aid)
        return stand is None or stand != self.werkstatt.signatur()

    def pruefe_fristen(self, frist: float = AUFTRAG_FRIST_S) -> None:
        if self.auftraege.pruefe_fristen(frist):
            self.raeume_auf()

    def fortschritt(self, aid: object, zeile: object) -> None:
        if (not isinstance(zeile, str) or not zeile.strip() or len(zeile) > MELDUNG_HOECHSTENS
                or any(ableiten.ist_steuerzeichen(z) for z in zeile)):
            raise AuftragFehler(400, f"zeile ist eine Zeile mit 1 bis {MELDUNG_HOECHSTENS} Zeichen.")
        self.auftraege.fortschritt(aid, zeile)

    def ergebnis(self, aid: object, nur_frage: bool = False, tokens: object = None) -> dict:
        """Prüft die Antwort der Runde, höchstens PRUEFUNG_HOECHSTENS_S lang (die Sitzung wartet etwas länger)."""
        ende = time.monotonic() + scanner_lauf.PRUEFUNG_HOECHSTENS_S
        auftrag = self.auftraege.laufend(aid, arbeit=True)
        try:
            return self._pruefe_antwort(auftrag, nur_frage, tokens, ende)
        finally:
            # Hat die Sitzung inzwischen neu gefragt, kommt ein zweiter Versuch bei ihr nachgereicht (WS-K4).
            self.auftraege.ende_arbeit(auftrag["id"], "versuch")

    def _pruefe_antwort(self, auftrag: dict, nur_frage: bool, tokens: object, ende: float) -> dict:
        aid = auftrag["id"]
        self.auftraege.zaehle_tokens(aid, tokens)
        try:
            self._auftragsordner(aid)
            inhalt = lies_antwort(self._antwort_datei(auftrag["id"], auftrag["runde"]))
        except (AntwortFehler, pfade.PfadFehler) as fehler:
            return self._ablehnen(auftrag, [str(fehler)])
        daten = ableiten.json_block(inhalt.decode("utf-8", "replace"))
        schluessel = ergebnis_schluessel(auftrag["art"])
        entwerfen = auftrag["art"] == ENTWERFEN
        if not isinstance(daten, dict) or len(daten) != 1 or not set(daten) <= {schluessel, "frage"}:
            return self._ablehnen(auftrag, [kein_block(auftrag["art"])])
        if "frage" in daten:
            meldungen = pruefe_frage(daten["frage"])
            if meldungen:
                return self._ablehnen(auftrag, meldungen)
            frage = {"text": daten["frage"]["text"], "optionen": list(daten["frage"].get("optionen", []))}
            if not self.auftraege.frage(auftrag["id"], frage):
                return self._ablehnen(auftrag, [ZU_VIELE_RUECKFRAGEN_ENTWURF if entwerfen else ZU_VIELE_RUECKFRAGEN])
            self._schreibe_auftrag_still(auftrag["id"])
            return {"zustand": "rueckfrage"}
        if nur_frage:
            return self._ablehnen(auftrag, [KEINE_FRAGE])
        if schluessel == "gutachten":
            return self._gutachten(auftrag, daten["gutachten"], ende)
        if schluessel == "auswertung":
            return self._auswertung(auftrag, daten["auswertung"], ende)
        if entwerfen:
            return self._entwurf_einreichen(auftrag, daten["entwurf"], ende)
        self.auftraege.zeile(auftrag["id"], PRUEFT)
        meldungen, satz = self._pruefe(daten["satz"], ende)
        if not meldungen and auftrag["einbauen"] is not None:
            meldungen = gutachten_form.zuordnung(satz, auftrag["einbauen"]["auswahl"])
        if meldungen:
            return self._ablehnen(auftrag, meldungen)
        try:
            vid = self.werkstatt.lege_vor(satz, HERKUNFT_KI)
        except WerkstattFehler as fehler:
            # Volle Vorschlagsliste (WS-A7): am Satz ist nichts falsch, ein zweiter Agentenlauf hilft nicht.
            grund = einzeilig(fehler.daten.get("fehler"))
            self.auftraege.scheitere(auftrag["id"], grund)
            self.raeume_auf()
            return {"zustand": "fehlgeschlagen", "grund": grund, "meldungen": [grund]}
        try:
            self.auftraege.fertig(auftrag["id"], vid, len(satz["eintraege"]), GEPRUEFT)
        except AuftragFehler:
            self.werkstatt.vorschlaege.entferne(vid)
            raise
        self.raeume_auf()
        return {"zustand": "fertig", "vorschlag": vid}

    def _gutachten(self, auftrag: dict, gutachten: object, ende: float | None = None) -> dict:
        """WS-K15: Rahmen falsch → abgelehnt; ein Vorschlag ohne Beleg oder mit ungültiger Skizze fällt allein weg."""
        aid = auftrag["id"]
        self.auftraege.zeile(aid, PRUEFT_GUTACHTEN)
        try:
            vorschlaege = gutachten_form.rahmen(gutachten)
        except gutachten_form.GutachtenFehler as fehler:
            return self._ablehnen(auftrag, fehler.meldungen)
        kopie = self._ordner(aid)[1]
        with self._sperre:
            stand = self._kopie_stand.get(aid, {})
        gueltig: list[dict] = []
        verworfen: list[str] = []
        gesehen: set[str] = set()
        for nummer, vorschlag in enumerate(vorschlaege, 1):
            vid = vorschlag.get("id") if isinstance(vorschlag, dict) else None
            name = vid if isinstance(vid, str) and gutachten_form.VORSCHLAG_ID.fullmatch(vid) else f"Nr. {nummer}"
            meldungen, geprueft = gutachten_form.pruefe_vorschlag(vorschlag, kopie, gesehen, stand)
            if name == vid:
                gesehen.add(vid)
            if not meldungen:
                meldungen, _ = self._pruefe(gutachten_form.skizze_als_satz(geprueft), ende)
            if meldungen:
                verworfen.append(einzeilig(f"Vorschlag {name} verworfen: {meldungen[0]}"))
                alltag = in_alltag(meldungen[0])
                self.auftraege.zeile(aid, einzeilig(f"Vorschlag {name} verworfen: {alltag}"),
                                     einzeilig(meldungen[0]) if alltag != meldungen[0] else None)
                continue
            gueltig.append(geprueft)
        if vorschlaege and not gueltig:
            return self._ablehnen(auftrag, verworfen)
        self.auftraege.fertig_gutachten(aid, {"vorschlaege": gueltig, "verworfen": len(verworfen)},
                                        gutachten_geprueft(len(gueltig), len(verworfen)))
        self.raeume_auf()
        return {"zustand": "fertig", "vorschlaege": len(gueltig), "verworfen": len(verworfen)}

    def _auswertung(self, auftrag: dict, auswertung: object, ende: float | None = None) -> dict:
        aid = auftrag["id"]
        self.auftraege.zeile(aid, PRUEFT_AUSWERTUNG)
        try:
            auswertung = auswertung_form.rahmen(auswertung)
        except auswertung_form.AuswertungFehler as fehler:
            return self._ablehnen(auftrag, fehler.meldungen)
        daten = self.auswerten.arbeitsdaten(aid) if self.auswerten is not None else None
        if daten is None:
            return self._ablehnen(auftrag, [ARBEITSDATEN_FEHLEN])
        register = self.werkstatt.register
        gesehen: dict = {}
        geprueft: dict = {"bewertungen": [], "vorschlaege": [], "gedaechtnis": []}
        verworfen: list[str] = []
        for art, liste in (("bewertung", "bewertungen"), ("vorschlag", "vorschlaege"), ("gedaechtnis", "gedaechtnis")):
            for punkt in auswertung[liste]:
                meldungen, ergebnis = auswertung_form.pruefe_punkt(art, punkt, daten, register, gesehen)
                if not meldungen and art == "vorschlag":
                    if aenderungssatz.ist_gedaechtnis_satz({"eintraege": ergebnis["skizze"]}):
                        meldungen = [aenderungssatz.NUR_AUS_AUSWERTUNG]
                    else:
                        meldungen, _ = self._pruefe(gutachten_form.skizze_als_satz(ergebnis), ende)
                if meldungen:
                    name = {"bewertung": f"Bewertung zu {str(punkt.get('ziel'))[:80]}",
                            "vorschlag": f"Vorschlag {str(punkt.get('id'))[:20]}",
                            "gedaechtnis": f"Memory-Eintrag {str(punkt.get('id'))[:20]}"}[art]
                    verworfen.append(einzeilig(f"{name} verworfen: {meldungen[0]}"))
                    alltag = in_alltag(meldungen[0])
                    self.auftraege.zeile(aid, einzeilig(f"{name} verworfen: {alltag}"),
                                         einzeilig(meldungen[0]) if alltag != meldungen[0] else None)
                    continue
                geprueft[liste].append(ergebnis)
        eingereicht = sum(len(auswertung[liste]) for liste in ("bewertungen", "vorschlaege", "gedaechtnis"))
        if eingereicht and not any(geprueft[liste] for liste in ("bewertungen", "vorschlaege", "gedaechtnis")):
            return self._ablehnen(auftrag, verworfen)
        geprueft.update(datenlage=auswertung["datenlage"], verworfen=len(verworfen))
        anzeige = auswertung_form.mit_anzeige(geprueft, daten, register)
        intern = {"skill": daten.skill, "gedaechtnis": {p["id"]: p["_intern"] for p in geprueft["gedaechtnis"]}}
        zahlen = {"bewertungen": len(geprueft["bewertungen"]), "vorschlaege": len(geprueft["vorschlaege"]),
                  "gedaechtnis": len(geprueft["gedaechtnis"]), "verworfen": len(verworfen)}
        self.auftraege.fertig_auswertung(aid, anzeige, intern, auswertung_geprueft(zahlen))
        self.auswerten.nach_auswertung(daten)
        self.raeume_auf()
        return {"zustand": "fertig", "auswertung": zahlen}

    def _schreibe_auftrag_still(self, aid: str) -> None:
        try:
            self._schreibe_auftrag(self.auftraege.hole(aid))
        except (OSError, pfade.PfadFehler):
            pass

    def _ablehnen(self, auftrag: dict, meldungen: list[str]) -> dict:
        meldungen = [einzeilig(m) for m in meldungen] or [kein_block(auftrag["art"])]
        weiter = self.auftraege.naechster_versuch(auftrag["id"], meldungen, in_alltag(meldungen[0]))
        if weiter is None:
            self.raeume_auf()
            danach = self.auftraege.hole(auftrag["id"])  # fehlgeschlagen, oder entwurf nach einer Folgerunde (WS-KE7)
            return {"zustand": danach["zustand"], "grund": danach["grund"], "meldungen": meldungen}
        try:
            self._schreibe_auftrag(weiter)
        except (OSError, pfade.PfadFehler) as fehler:
            self.auftraege.scheitere(auftrag["id"], einzeilig(str(fehler)))
            self.raeume_auf()
            danach = self.auftraege.hole(auftrag["id"])  # entwurf nach einer Folgerunde (WS-KE7)
            return {"zustand": danach["zustand"], "grund": danach["grund"], "meldungen": meldungen}
        return {"zustand": "laeuft", "meldungen": meldungen, "auftrag": self._fuer_sitzung(weiter, "versuch")}

    def _pruefe(self, satz: object, ende: float | None = None) -> tuple[list[str], dict]:
        """Prüfung nach WS-K7; mit ende höchstens bis dahin (danach: zeit_um())."""
        if isinstance(satz, dict):
            satz = {**satz, "herkunft": "ki"}
        try:
            aenderungssatz.pruefe(satz)
        except aenderungssatz.SatzFehler as fehler:
            return [f"Form: {m}" for m in fehler.meldungen], {}
        try:
            fehler, neue, nachher, vorher = self.werkstatt.probelauf(satz, laufzeit=_rest(ende))
        except WerkstattFehler as fehler_:
            return [str(fehler_.daten.get("fehler"))], {}
        except (pfade.PfadFehler, pfade.KopieFehler) as fehler_:
            return [str(fehler_)], {}
        if fehler:
            return [f"Eintrag {f['eintrag']}: {fuer_ki(f['text'])}" for f in fehler], {}
        if nachher is None:
            return [KEINE_AENDERUNG], {}
        if neue:
            return [f"Neuer Hinweis in {b.get('datei')}:{b.get('zeile')}: {b.get('text')}" for b in neue], {}
        ohne = schritte_ohne_beleg(vorher, nachher)
        if ohne:
            return ohne, {}
        return [], satz

    # ------------------------------------------------------------ Seite

    def fuer_seite(self) -> list[dict]:
        """Aufträge für die Seite (GET /api/auftraege), je mit dem Ausgang ihres Vorschlags (WS-K13): None, solange er
        offen ist oder es keinen gibt, sonst {art: uebernommen, gewaehlt, von} bzw. {art: verworfen}."""
        vorschlaege = self.werkstatt.vorschlaege
        return [{**a, "ausgang": vorschlaege.ausgang(a["satz"]) if a.get("satz") else None} for a in self.auftraege.liste()]

    def mit_lauf(self, vorschlaege: list[dict]) -> list[dict]:
        """WS-K12: Vorschläge der KI tragen Dauer und Tokens ihres Auftrags für die Kopfzeile der Vorschau."""
        laeufe = self.auftraege.laeufe()
        return [{**v, "lauf": laeufe.get(v["id"])} if v.get("herkunft") == HERKUNFT_KI else v for v in vorschlaege]

    def antworte(self, aid: object, antwort: object) -> dict:
        return self.auftraege.beantworte(aid, pruefe_text(antwort, "antwort"))

    def brich_ab(self, aid: object) -> dict:
        auftrag = self.auftraege.brich_ab(aid)
        self.raeume_auf()
        return auftrag

    def scheitere(self, aid: object) -> dict:
        """WS-K8: Den Grund liest der Server aus der Antwortdatei der aktuellen Runde (erste Zeile,
        höchstens 300 Zeichen), nie aus einem Shell-Argument; fehlt sie, gilt der feste Satz."""
        auftrag = self.auftraege.hole(aid)
        grund = OHNE_GRUND
        # Nur ein laufender Auftrag hat eine Antwortdatei; angelegt wird hier nichts (ein Entwurf hat keine Ordner).
        if auftrag["zustand"] in (LAEUFT, RUECKFRAGE) and auftrag["runde"] > 0 \
                and os.path.lexists(self._ordner(auftrag["id"])[0]):
            try:
                self._auftragsordner(auftrag["id"])
                text = lies_antwort(self._antwort_datei(auftrag["id"], auftrag["runde"])).decode("utf-8", "replace")
            except (AntwortFehler, pfade.PfadFehler, OSError):
                text = ""
            zeile = einzeilig(text.strip().splitlines()[0] if text.strip() else "")
            grund = zeile or OHNE_GRUND
        ergebnis = self.auftraege.scheitere(aid, grund)
        self.raeume_auf()
        return ergebnis

    # ------------------------------------------------------------ Entwürfe (Ticket 17, WS-KE)

    def _namen_im_kit(self, ende: float | None = None) -> tuple[set[str], set[str]]:
        """Agenten und Skills im Bauplan; mit ende höchstens bis dahin (sonst WerkstattFehler 503 mit zeit_um())."""
        knoten = [k for k in self.werkstatt.bauplan(laufzeit=_rest(ende)).get("knoten", []) if isinstance(k, dict)]
        return ({k["name"] for k in knoten if k.get("typ") == "agent" and isinstance(k.get("name"), str)},
                {k["name"] for k in knoten if k.get("typ") == "skill" and isinstance(k.get("name"), str)})

    def _entwurf_einreichen(self, auftrag: dict, entwurf: object, ende: float | None = None) -> dict:
        """WS-KE3: Form und Bauplan, dann der ganze Entwurf als Satz durch die Prüfung nach WS-K7."""
        self.auftraege.zeile(auftrag["id"], PRUEFT_ENTWURF)
        try:
            namen = self._namen_im_kit(ende)
        except WerkstattFehler as fehler:
            return self._ablehnen(auftrag, [str(fehler.daten.get("fehler"))])
        meldungen = werkstatt_entwurf.pruefe(entwurf, *namen)
        if not meldungen:
            # Aufruf-, Schleifen- und Mensch-Sätze erzeugt die Werkstatt aus der Struktur
            entwurf, meldungen = werkstatt_entwurf.normalisiere(entwurf)
        if not meldungen:
            satz, meldungen = werkstatt_entwurf.zu_satz(entwurf, werkstatt_entwurf.teil_ids(entwurf))
            if not meldungen:
                meldungen, _ = self._pruefe(satz, ende)
        if meldungen:
            return self._ablehnen(auftrag, meldungen)
        nr = self.auftraege.entwurf_fertig(auftrag["id"], entwurf)
        self.raeume_auf()
        return {"zustand": ENTWURF, "fassung": nr}

    def nachricht(self, aid: object, text: object) -> dict:
        return self.auftraege.nachricht(aid, pruefe_text(text, "nachricht"))

    def _vorhandene_mit_memory(self, neue_skill: str) -> dict[str, tuple[str | None, list[str]]]:
        bauplan = self.werkstatt.bauplan()
        je_agent = werkstatt_kennzahlen.skills_je_agent(bauplan)
        felder, _ = self.werkstatt.agenten_memory()
        return {name: (feld, sorted(s for s in je_agent.get(name, []) if s != neue_skill))
                for name, feld in felder.items()}

    def uebernehmen(self, aid: object, fassung: object, teile: object, memory: object = None,
                    logs: object = None) -> dict:
        """WS-KE6: Satz aus der Auswahl, Prüfung wie WS-K7, Vorschlag der KI, Auftrag fertig."""
        if not isinstance(memory, bool) or not isinstance(logs, bool):
            raise WerkstattFehler(400, {"fehler": MEMORY_LOGS_PFLICHT})
        if not isinstance(teile, list) or not all(isinstance(t, str) for t in teile) or len(set(teile)) != len(teile):
            raise WerkstattFehler(400, {"fehler": "teile ist eine Liste verschiedener ids."})
        entwurf = self.auftraege.zur_uebernahme(aid, fassung)
        vorhandene = self._vorhandene_mit_memory(entwurf["skill"]["name"]) if memory else None
        satz, meldungen = werkstatt_entwurf.zu_satz(entwurf, teile, vorhandene)
        if meldungen:
            raise WerkstattFehler(409, {"fehler": AUSWAHL_PASST_NICHT, "meldungen": meldungen})
        meldungen, satz = self._pruefe(satz)
        if meldungen:
            raise WerkstattFehler(409, {"fehler": AUSWAHL_ABGELEHNT, "meldungen": [einzeilig(m) for m in meldungen]})
        vid = self.werkstatt.lege_vor(satz, HERKUNFT_KI, {"skill": entwurf["skill"]["name"], "logs": logs})
        try:
            self.auftraege.uebernommen(aid, fassung, vid, len(satz["eintraege"]),
                                       f"Fassung {fassung} übernommen: {len(satz['eintraege'])} Einträge zur Vorschau")
        except AuftragFehler:
            self.werkstatt.vorschlaege.entferne(vid)
            raise
        self.raeume_auf()
        return {"vorschlag": vid, "eintraege": len(satz["eintraege"])}
