"""Kern der Werkstatt: Bauplan, Vorschau, Anwenden, Rückgängig, Speichern, Vorschläge (specs/werkstatt.yaml).

Der Server ruft nur diese Klasse. Sie hält den Verlauf auf der Platte, den zwischengespeicherten Bauplan
(WS-S2), die offenen Vorschläge und Aufträge im Speicher, die Dateien, die sie in dieser Sitzung selbst
geschrieben hat (nur die speichert sie, WS-V5), und eine Sperre, damit nie zwei Änderungen gleichzeitig
schreiben. Fehler gehen als WerkstattFehler mit HTTP-Status und JSON-Daten hinaus.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import NamedTuple

import ableiten
import aenderungssatz
import agent_prompt
import ergebnis as ergebnis_modul
import finden
import hooks as hooks_modul
import ordner as ordner_modul
import plattform
import scanner_lauf
import texte
import vokabular
import werkstatt_ablage as ablage_modul
import werkstatt_alt
import werkstatt_bereinigen as bereinigen
import werkstatt_einstellungen
import werkstatt_gedaechtnis as gd
import werkstatt_kontext as kontext_modul
import werkstatt_kontext_schutz as ks
import werkstatt_leitplanken as leitplanken_modul
import werkstatt_pfade as pfade
from ableitung import belegzeile
from befunde import Rueck, befund_identitaet
from schreiber import SchreibFehler, Schreiber, VirtuellesKit
from schreiber_agenten import agent_text, text_stand
from virtuelles_kit import anzeige, diff
from zeilen import Textdatei, ZeilenFehler, ueberschrift
from werkstatt_auftraege import Auftraege
from werkstatt_pfade import ABLEITUNG, PfadFehler
from werkstatt_vorschlaege import VorschlagFehler, Vorschlaege

NUR_LESEN = texte.WERKSTATT_NUR_LESEN
STAND_ALT = "Die Dateien haben sich seit der Vorschau geändert. Schau dir die Vorschau noch einmal an."
AUSWAHL_FEHLER = "Die Auswahl lässt sich so nicht übernehmen."
NOCH_GENANNT = ("Der Text nennt noch, was wegfällt (siehe Warnungen). Bestätige, um trotzdem zu übernehmen.")
BESTAETIGEN = "Betroffene Dateien haben Änderungen, die nicht von der Werkstatt stammen. Bestätige, um trotzdem zu übernehmen."
NICHTS_GEWAEHLT = "Die Auswahl ändert nichts."
ZURUECKGENOMMEN = "Die Änderung hätte neue Hinweise zum Bauplan ergeben und wurde nicht geschrieben."
NICHTS_RUECKGAENGIG = "Es gibt nichts rückgängig zu machen."
KEIN_GIT = "Das Kit liegt in keinem Git-Repo. Speichern geht nur dort; deine Änderungen stehen schon in den Dateien."
NICHTS_ZU_SPEICHERN = "Es gibt nichts zu speichern."
SCAN_FEHLER = "Das Kit ließ sich nicht einlesen"
ANDERER_SATZ = "Der letzte Satz hat sich inzwischen geändert. Klick noch einmal auf Rückgängig."
DATEIEN_HOECHSTENS = 5000
PROBEN_GEMERKT = 8
# Den Eintrag, nach dem ein neuer Befund auftritt, misst die Vorschau mit höchstens so vielen Scans in so vielen Sekunden;
# danach nennt sie keinen Eintrag, sondern „durch die Auswahl“.
SCHULD_SCANS_HOECHSTENS = 12
SCHULD_ZEIT_S = 15.0
BAUPLAN_VERSUCHE = 3  # so oft liest bauplan() das Kit, wenn es sich währenddessen ändert, dann 503
KIT_WIRD_GESCHRIEBEN = "Das Kit wird gerade geschrieben. Bitte versuch es gleich noch einmal."
SIGNATUR_WALK_S = 5.0
GIT_WURZEL_NEU_S = SIGNATUR_WALK_S  # so lange gilt „kein Git-Repo“, dann wird neu gefragt
# So lange gilt die Liste „was Speichern committen würde“ im Status, solange sich weder die Dateien noch Index oder HEAD
# geändert haben; danach fragt der Status git neu (eine neue .gitignore-Regel z. B. zeigt keine Datei an).
ZU_SPEICHERN_GILT_S = 2.0
HAND, KI = "hand", "ki"
NACHRICHT_UNGUELTIG = "Die Commit-Nachricht ist ungültig."
NACHRICHT_HOECHSTENS = 5000
AGENT_NAME_UNGUELTIG = "Der Name des Agenten ist ungültig."
STEUERZEICHEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
NICHTS_GEAENDERT = "Nichts wurde geändert."
PIPELINE_PASST_NICHT = "pipeline passt nicht zur Auswahl."
LOGS_WAHR_ODER_FALSCH = "logs muss wahr oder falsch sein."
SCHREIBRECHTE = ("Write", "Edit")


def logs_nicht_gespeichert(grund: str) -> str:
    return (f"Die Pipeline ist angelegt, aber die Einstellung für Logs ließ sich nicht speichern: {grund.rstrip('.')}. "
            f"Logs sind an; ändern kannst du das unter „Memory und Logs“.")


def darf_nicht_schreiben(agent: str) -> str:
    return (f"{agent} darf heute nicht schreiben. Mit Memory schaltet Claude Code für ihn Lesen, Schreiben und "
            f"Bearbeiten ein, damit er seine Memory-Dateien pflegen kann; das gilt nicht nur für den Memory-Ordner.")


def nicht_alles_zurueck(dateien: list[str], sicherung: str) -> str:
    return f"Die Werkstatt konnte nicht alles zurücknehmen: {', '.join(dateien)}. Sicherung liegt unter {sicherung}."


def verlauf_nicht_gespeichert(grund: str) -> str:
    return f"Der Commit ist gemacht, aber der Verlauf der Werkstatt ließ sich nicht speichern: {grund}"


def zeit_um() -> str:
    return f"Die Zeit für die Prüfung ist um (höchstens {scanner_lauf.PRUEFUNG_HOECHSTENS_S:g} s)."


def zeit_um_beim_anlegen() -> str:
    return (f"Die Werkstatt kam nicht innerhalb von {scanner_lauf.PRUEFUNG_HOECHSTENS_S:g} s an das Kit, es wurde "
            f"gerade geändert oder eingelesen. Stell den Auftrag noch einmal.")


def pruefe_nachricht(nachricht: object) -> str:
    """Eigene Commit-Nachricht (WS-A6): eine Zeile vorn, keine Steuerzeichen, nicht wie eine Option."""
    if not isinstance(nachricht, str) or not 0 < len(nachricht) <= NACHRICHT_HOECHSTENS:
        raise WerkstattFehler(400, {"fehler": NACHRICHT_UNGUELTIG})
    erste = nachricht.split("\n", 1)[0].strip()
    if not erste or erste.startswith("-") or STEUERZEICHEN.search(nachricht.replace("\r\n", "\n")):
        raise WerkstattFehler(400, {"fehler": NACHRICHT_UNGUELTIG})
    return nachricht


def _werkzeuge(wert: object) -> list[str] | None:
    if wert is None:
        return None
    teile = wert if isinstance(wert, list) else str(wert).split(",")
    return [str(t).strip() for t in teile if str(t).strip()]


class WerkstattFehler(Exception):
    def __init__(self, status: int, daten: dict):
        super().__init__(json.dumps(daten, ensure_ascii=False))
        self.status = status
        self.daten = daten


def fremde_aenderung(pfad: str) -> str:
    return f"{pfad} hat Änderungen, die nicht von der Werkstatt stammen und noch nicht gespeichert sind."


def git_antwortet_nicht(grund: str) -> str:
    return f"Git antwortet nicht: {grund}, nichts verändert."


def fremde_aenderung_ungeprueft(pfad: str, grund: str) -> str:
    return (f"Ob {pfad} Änderungen hat, die nicht von der Werkstatt stammen, konnte nicht geprüft werden "
            f"(Git antwortet nicht: {grund}).")


def _nummern(satz: dict) -> dict[str, str]:
    """Nummer eines Eintrags, wie die Vorschau ihn zeigt (1, 2, …), statt seiner Kennung."""
    return {e["id"]: str(i + 1) for i, e in enumerate(satz["eintraege"])}


def braucht(eintrag: str, anderer: str, beschreibung: str) -> str:
    return f"Eintrag {eintrag} braucht Eintrag {anderer} ({beschreibung})."


def auszug(text: str, name: str, breite: int = 60) -> str:
    """Höchstens breite Zeichen der Zeile rund um die erste Nennung von name, Schnitte mit „…“."""
    text = text.strip()
    if len(text) <= breite:
        return text
    treffer = re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) or re.search(re.escape(name), text)
    if treffer is None:
        return text[:breite] + "…"
    anfang = max(0, min(treffer.start() - (breite - len(name)) // 2, len(text) - breite))
    stueck = text[anfang:anfang + breite]
    return ("…" if anfang > 0 else "") + stueck + ("…" if anfang + breite < len(text) else "")


def aufgerufen(name: str, datei: str, zeile: int, text: str) -> str:
    return (f"Agent {name} wird in {datei} (Zeile {zeile}) noch aufgerufen: „{auszug(text, name)}“. "
            f"Schreib die Stelle im selben Satz um.")


def _neu_gemeldet(befund: dict) -> str:
    zeile = befund.get("zeile")
    ort = f"{befund.get('datei')} (Zeile {zeile})" if zeile else str(befund.get("datei"))
    text = str(befund.get("text", "")).strip()
    return f"meldet der Bauplan neu in {ort}: {text}{'' if text.endswith('.') else '.'}"


def neuer_befund(eintrag: str, befund: dict) -> str:
    """Ein Befund, den der Neuscan nach den Einträgen neu meldet, mit Datei, Zeile und Grund des Scanners."""
    return f"Nach Eintrag {eintrag} {_neu_gemeldet(befund)}"


def durch_die_auswahl(befund: dict) -> str:
    """Wie neuer_befund, wenn sich der Eintrag nicht messen ließ (Budget um, Zwischenstand nicht einlesbar)."""
    return f"Durch die Auswahl {_neu_gemeldet(befund)}"


def aenderungen_schluessel(aenderungen: dict) -> str:
    """Was die Probe-Kopie über das Kit legt (Schlüssel und Prüfsumme danach): gleich für jeden Satz und jede Auswahl
    mit denselben Änderungen, z. B. die Prüfung eines Satzes der KI und die Vorschau seiner Skizze."""
    teile = sorted((schluessel, pfade.sha(neu)) for schluessel, (_, neu) in aenderungen.items())
    return hashlib.sha256(json.dumps(teile, ensure_ascii=False).encode("utf-8")).hexdigest()


def noch_gesagt(eintrag: str, datei: str, zeile: int, zitat: str) -> str:
    return (f"„{zitat[:60]}“ steht nach Eintrag {eintrag} noch in {datei} (Zeile {zeile}); was im Bild wegfällt, darf "
            f"der Text nicht mehr sagen.")


def noch_gerufen(datei: str, zeile: int, name: str) -> str:
    return f"Der Text in {datei} ruft {name} noch auf (Zeile {zeile}), im Bild kommt er dort nicht mehr vor."


def genannt(name: str, datei: str, zeile: int, text: str) -> str:
    return f"Der gelöschte Agent {name} wird in {datei} (Zeile {zeile}) noch genannt: „{auszug(text, name)}“."


def nicht_umbenannt(datei: str, name: str, grund: str) -> str:
    return f"{datei} nennt {name} noch (nicht geändert: {grund})"


def nicht_einlesbar(grund: str, nachher: bool = True) -> str:
    wann = "nach der Änderung" if nachher else "vor der Änderung"
    return f"Der Bauplan lässt sich {wann} nicht einlesen: {grund}"


def nicht_nachpruefbar(grund: str) -> str:
    return f"Nach der Änderung lässt sich nicht prüfen, ob noch etwas auf Weggefallenes verweist: {grund}"


VERWEIS = re.compile(r"\b(?:Schritt(?:e|en)?|[Ss]teps?)\s+(\d+)(?:\s*(?:bis|und|–|-|to|and)\s*(\d+))?")
NUMMER_VORN = re.compile(r"^\s*\d+\.")


def _schritt_ids(datei: Textdatei) -> list:
    atlas = datei.frontmatter().werte().get("atlas")
    schritte = atlas.get("schritte") if isinstance(atlas, dict) else None
    return [s.get("id") if isinstance(s, dict) else None for s in schritte] if isinstance(schritte, list) else []


def _zeilen_von(inhalt: bytes | None, alt: str) -> list[int] | None:
    """Erste und letzte Zeile (1-basiert), die text_ersetzen ändert; die Seite markiert so den Schritt (Gutachten).
    Zeilenumbrüche in alt gelten wie bei text_ersetzen als Zeilenende der Datei."""
    try:
        text = inhalt.decode("utf-8") if inhalt is not None else None
    except UnicodeDecodeError:
        return None
    if text and alt and Textdatei(text).ende == "\r\n":
        alt = alt.replace("\r\n", "\n").replace("\n", "\r\n")
    if not text or not alt or alt not in text:
        return None
    anfang = text[:text.index(alt)].count("\n") + 1
    return [anfang, anfang + alt.count("\n")]


def schrittnummern_verweise(rel: str, alt: bytes, neu: bytes) -> list[dict]:
    """Manueller Test 24.10.2026: Zählt die Werkstatt die Schritte einer Skill mit Atlas-Angaben neu, warnt die
    Vorschau vor Zeilen, die Schrittnummern nennen, deren Schritt jetzt eine andere Nummer hat (nicht umschreiben)."""
    try:
        vorher, nachher = Textdatei.aus_bytes(alt), Textdatei.aus_bytes(neu)
        ids_vorher, ids_nachher = _schritt_ids(vorher), _schritt_ids(nachher)
    except Exception:  # noqa: BLE001 – eine Warnung, kein Fehler: unlesbares Frontmatter prüft der Scan
        return []
    if not ids_vorher or ids_vorher == ids_nachher:
        return []
    neue_nummer = {sid: i + 1 for i, sid in enumerate(ids_nachher)}
    anders = {i + 1 for i, sid in enumerate(ids_vorher) if neue_nummer.get(sid) != i + 1}
    alte_zeilen = {NUMMER_VORN.sub("", z) for z in vorher.texte()}
    meldungen = []
    for i, zeile in enumerate(nachher.texte()):
        if NUMMER_VORN.sub("", zeile) not in alte_zeilen:
            continue
        for treffer in VERWEIS.finditer(zeile):
            von = int(treffer.group(1))
            bis = int(treffer.group(2)) if treffer.group(2) else von
            if any(n in anders for n in range(von, min(bis, von + 50) + 1)):
                meldungen.append({"pfad": rel, "art": "verweis",
                                  "text": f"{rel} (Zeile {i + 1}) verweist auf Schrittnummern, die sich ändern: "
                                          f"„{auszug(zeile, treffer.group(0))}“ Prüf die Nummern, bevor du übernimmst."})
                break
    return meldungen


def genutzt(name: str, stellen: list[str]) -> str:
    return (f"Agent {name} wird noch in {', '.join(stellen)} genutzt. "
            f"Entferne diese Schritte im selben Satz oder ändere sie.")


def _rest(ende: float | None) -> float | None:
    """Restzeit bis ende (None: ohne Grenze); ist sie um, 503 mit zeit_um()."""
    if ende is None:
        return None
    rest = ende - time.monotonic()
    if rest <= 0:
        raise WerkstattFehler(503, {"fehler": zeit_um()})
    return rest


class _Berechnung:
    def __init__(self) -> None:
        self.eintraege: list[dict] = []
        self.fehler: list[dict] = []
        self.aenderungen: dict = {}
        self.gelesen: dict = {}
        # Umbenennungen rückwärts, wie der Schreiber sie meldet: neue Agent-Datei → Datei vorher, neuer Name eines
        # Agenten des Kits → Name vorher. Befunde danach zählen so unter dem Namen vorher (befund_identitaet).
        self.rueck_dateien: dict[str, str] = {}
        self.rueck_namen: dict[str, str] = {}
        self.nennungen: list[dict] = []
        self.rechte: list[dict] = []
        self.nummern: dict[str, str] = {}  # Nummer je Eintrag wie in der Vorschau, einmal je Berechnung
        self.letzter = ""  # letzter gewählter Eintrag
        self.leitplanken: list[dict] = []
        self.skripte: list[dict] = []

    @property
    def rueck(self) -> Rueck:
        return Rueck(self.rueck_dateien, self.rueck_namen)


class _Messung(NamedTuple):
    """Ein Scan einer Teilauswahl in _schuldige: Identitäten gezählt, die Berechnung, der Bauplan, seine neuen
    Befunde (_neu) und deren Identitäten gezählt."""
    zaehlung: Counter
    berechnung: _Berechnung
    atlas: dict
    neue: list[dict]
    neue_zaehlung: Counter


def _messung(atlas: dict, berechnung: _Berechnung, neue: list[dict]) -> _Messung:
    return _Messung(Counter(befund_identitaet(b, berechnung.rueck) for b in atlas.get("befunde", [])), berechnung,
                    atlas, neue, Counter(befund_identitaet(b, berechnung.rueck) for b in neue))


def _paare(links: list, rechts: list[dict], merkmale: tuple, passt=lambda i, j: True) -> list[int | None]:
    """Je Element aus links der Index seines Paars aus rechts (None ohne): je Merkmal der Reihe nach paart jedes noch
    freie Element aus links mit dem ersten noch freien aus rechts, das passt (passt(i, j)) und dasselbe Merkmal hat;
    das Merkmal None paart in der Reihenfolge, auch ein None aus links. Jedes Element höchstens einmal."""
    frei = list(range(len(rechts)))
    paar: list[int | None] = [None] * len(links)
    for merkmal in merkmale:
        for i, links_i in enumerate(links):
            if paar[i] is not None or (merkmal is not None and links_i is None):
                continue
            treffer = next((j for j in frei if passt(i, j)
                            and (merkmal is None or merkmal(rechts[j]) == merkmal(links_i))), None)
            if treffer is not None:
                frei.remove(treffer)
                paar[i] = treffer
    return paar


def _ungepaart(jetzt: list[dict], frueher: list[dict]) -> list[dict]:
    """Die Befunde aus jetzt, die keinem aus frueher entsprechen: Paare zuerst über den ganzen Befund, dann über den
    Text, dann über die Zeile, je einer zu einem."""
    paar = _paare(jetzt, frueher, (lambda b: b, lambda b: b.get("text"), lambda b: b.get("zeile")))
    return [befund for befund, j in zip(jetzt, paar) if j is None]


def _im_endergebnis(neue: list[dict], idents: list[tuple], im_praefix: list[dict | None]) -> list[dict]:
    """Je Befund aus neue die Instanz derselben Identität aus neue, die zu seiner Instanz im Präfix (im_praefix, None
    ohne) passt: Paare zuerst über den ganzen Befund, dann über den Text, sonst in der Reihenfolge; jede höchstens
    einmal. So passen Datei, Zeile und Text der Meldung zum Bauplan nach der ganzen Auswahl."""
    paar = _paare(im_praefix, neue, (lambda b: b, lambda b: b.get("text"), None),
                  lambda i, j: idents[j] == idents[i])
    return [neue[j] for j in paar]


class Werkstatt:
    def __init__(self, kit: Path, ergebnisordner: Path, scanner: Path, home: Path, *, geheimnis: Path,
                 konfiguration: Path | None = None, startordner: Path | None = None, daten: Path | None = None):
        self.kit = Path(os.path.realpath(kit))
        self.ergebnis = Path(os.path.abspath(ergebnisordner))
        self.startordner = Path(os.path.abspath(startordner)) if startordner is not None else None
        self.alter_startordner = self.startordner
        self.daten = Path(daten) if daten is not None else None
        self.scanner = Path(scanner)
        self.home = Path(home)
        konfig = konfiguration or werkstatt_einstellungen.claude_ordner(self.home, os.environ)
        self.konfiguration = Path(konfig)
        gefunden = finden.pruefe_ordner(self.kit, self.home, "/atlas:werkstatt")
        self.kit_id = gefunden.kit_id
        self.ordner = ordner_modul.erkenne(self.kit)
        self.art = self.ordner.art
        self.name = ergebnis_modul.dateiname(self.kit_id)
        self.quelle = finden.quelle(self.kit, self.home)
        self.ableitung_pfad = ableiten.ableitung_datei(self.ergebnis, self.name)
        self.sitzung = secrets.token_hex(8)
        self.ablage = ablage_modul.Ablage(self.ergebnis, self.kit, self.ableitung_pfad, self.ordner.skills,
                                          self.ordner.agenten, ablage_modul.lade_geheimnis(Path(geheimnis)),
                                          sitzung=self.sitzung, ableitung_lesen=lambda: self.ableitung_lesen,
                                          hooks=self.ordner.hook_freigabe)
        self._alter_fund: tuple[werkstatt_alt.Fund | None] | None = None
        self.sperre = threading.RLock()
        self.geschrieben: set[str] = set()
        self.vorschlaege = Vorschlaege()
        self.auftraege = Auftraege()  # Warteschlange der KI; Dateien und Prüfung in werkstatt_ki.KI (Ticket 13)
        self._bauplan: tuple | None = None
        self._signatur_sperre = threading.Lock()
        self._signatur_liste: tuple | None = None
        self._hook_skripte: tuple | None = None  # (Stand der Hook-Datei, Zeit, Skripte)
        self._wurzel: tuple | None = None  # (Git-Wurzel oder None, Zeit)
        self._git_ordner_gemerkt: tuple | None = None  # (Git-Wurzel, ihr .git-Ordner)
        self._zu_speichern_gemerkt: tuple | None = None  # (Schlüssel, Zeit, Ergebnis) für den Status
        # Bauplan nach den Änderungen der ganzen Auswahl je (aenderungen_schluessel, Signatur, Commit), aus Vorschau,
        # Übernehmen und der Prüfung eines Satzes der KI: dieselben Änderungen lesen das Kit nicht noch einmal ein.
        # Höchstens PROBEN_GEMERKT, der am längsten nicht gebrauchte fällt zuerst heraus. Die Scans der Präfixe und
        # Kandidaten (_schuldige) stehen nicht hier.
        self._proben: OrderedDict[tuple, str] = OrderedDict()
        self._proben_sperre = threading.Lock()
        self.verboten = gd.Verboten(self.home, self.konfiguration, self.ergebnis)
        self.register = gd.Register(self.ablage.geheimnis, self.home, self.art, self.kit_id)
        self.ablage.register = self.register
        self.ablage.verboten = self.verboten
        self.einstellungen = None
        self.lauf_offen = lambda: False
        self._agenten_memory: tuple | None = None

    # ------------------------------------------------------------ Lage

    @property
    def schreibbar(self) -> bool:
        return not finden.ist_installierte_kopie(self.kit, self.home, self.konfiguration)

    @property
    def ableitung_lesen(self) -> Path:
        if os.path.lexists(self.ableitung_pfad) or self.startordner is None:
            return self.ableitung_pfad
        rueckfall = ableiten.ableitung_datei(self.startordner / ergebnis_modul.ORDNER_NAME, self.name)
        if rueckfall.is_file() and not plattform.ist_verweis(rueckfall) \
                and not plattform.ist_verweis(rueckfall.parent) and self.schreibbar:
            return rueckfall
        return self.ableitung_pfad

    def _ableitung(self) -> dict | None:
        try:
            return ableiten.vorhandene(self.ableitung_lesen, self.quelle)
        except ableiten.AbleitungKaputt:
            return None

    def quellen_je_art(self, skill_ordner: list[Path] | None = None) -> dict[str, list[str]]:
        """Die Dateien, die der Scanner liest, je Quelle: Ordner.dateien() und die Hook-Skripte (Probe-Kopie,
        Arbeitskopie, Beobachtung). Die Hook-Datei (hooks.json bzw. settings.json) wird nur neu gelesen, wenn sie sich
        ändert (mtime, Größe), spätestens nach SIGNATUR_WALK_S, damit auch ein neu angelegtes Skript dazukommt."""
        stand = self._eintrag(str(self.kit / self.ordner.hooks))
        gemerkt = self._hook_skripte
        if gemerkt is None or gemerkt[0] != stand or time.monotonic() - gemerkt[1] > SIGNATUR_WALK_S:
            gemerkt = self._hook_skripte = (stand, time.monotonic(), hooks_modul.skripte(self.ordner))
        return {**self.ordner.dateien(skill_ordner), "hook_skripte": list(gemerkt[2])}

    def quellen(self, skill_ordner: list[Path] | None = None) -> list[str]:
        """Die Dateien aus quellen_je_art, jede einmal, relativ zum Kit; skill_ordner wie dort."""
        return list(dict.fromkeys(rel for liste in self.quellen_je_art(skill_ordner).values() for rel in liste))

    def signatur(self) -> tuple:
        """Stand aller Dateien, die der Scanner liest (Pfad, mtime, Größe), dazu die Ableitung.

        Die Liste der Dateien kommt aus dem letzten Durchgang (quellen_je_art); neu gelesen wird sie nur, wenn sich die
        Hook-Datei oder ein beobachteter Ordner (mtime: Datei dazu, weg, umbenannt) geändert hat, spätestens nach
        SIGNATUR_WALK_S. Sonst kostet die Signatur nur ein stat je Datei und Ordner."""
        hooks = self._eintrag(str(self.kit / self.ordner.hooks))
        with self._signatur_sperre:
            liste = self._signatur_liste
            if liste is None or liste[0] != hooks or time.monotonic() - liste[3] > SIGNATUR_WALK_S \
                    or any(self._mtime(o) != m for o, m in liste[2]):
                liste = self._signatur_liste = self._sammle(hooks)
        teile = [self._eintrag(p) for p in liste[1]]
        ableitung = self.ableitung_lesen
        if os.path.isfile(ableitung):
            teile.append(self._eintrag(str(ableitung)))
        return (*teile, *self._git_stand())

    def _git_stand(self) -> tuple:
        """Stand von HEAD (HEAD und sein Reflog, je mtime und Größe): Der Bauplan nennt den Commit, also gilt er nach
        einem Commit, auch einem von außen, nicht mehr. Ohne Git-Repo leer; kostet ein stat je Datei."""
        wurzel = self.git_wurzel()
        if wurzel is None:
            return ()
        ordner = self._git_ordner(wurzel)
        return tuple(self._eintrag(str(ordner / name)) for name in ("HEAD", "logs/HEAD"))

    def _git_ordner(self, wurzel: Path) -> Path:
        """Der .git-Ordner der Wurzel; in einem Worktree steht sein Pfad in der Datei .git (gemerkt je Wurzel)."""
        gemerkt = self._git_ordner_gemerkt
        if gemerkt is not None and gemerkt[0] == wurzel:
            return gemerkt[1]
        punkt_git = wurzel / ".git"
        ordner = punkt_git
        if punkt_git.is_file():
            try:
                zeile = punkt_git.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                zeile = ""
            if zeile.startswith("gitdir:"):
                ordner = (wurzel / zeile.removeprefix("gitdir:").strip()).resolve()
        self._git_ordner_gemerkt = (wurzel, ordner)
        return ordner

    def _sammle(self, hooks: tuple) -> tuple:
        """(Stand der Hook-Datei, Dateien in Scanner-Reihenfolge, beobachtete Ordner mit mtime, Zeit, abgeschnitten).

        Beobachtet werden die Kit-Wurzel, die Ordner der Quellen, jeder Skill-Ordner (auch ohne SKILL.md) und die Ordner
        auf dem Weg zu jeder Datei; mtime folgt Symlinks wie der Scanner. Höchstens DATEIEN_HOECHSTENS je Quelle: Eine
        große Quelle nimmt den späteren nichts."""
        dateien: list[str] = []
        abgeschnitten = False
        skill_ordner = self.ordner.skill_ordner()  # einmal: für die Dateiliste und als beobachtete Ordner
        for liste in self.quellen_je_art(skill_ordner).values():
            abgeschnitten = abgeschnitten or len(liste) > DATEIEN_HOECHSTENS
            dateien += liste[:DATEIEN_HOECHSTENS]
        dateien = list(dict.fromkeys(dateien))
        wege = [*dateien, f"{self.ordner.skills}/-", f"{self.ordner.agenten}/-", f"{vokabular.WERKZEUG_ORDNER}/-",
                self.ordner.hooks, ordner_modul.PLUGIN_JSON,
                *(f"{p.relative_to(self.kit).as_posix()}/-" for p in skill_ordner)]
        ordner_: dict[str, int | None] = {str(self.kit): self._mtime(str(self.kit))}
        for rel in wege:
            teile = rel.split("/")[:-1]
            for i in range(1, len(teile) + 1):
                pfad = str(self.kit.joinpath(*teile[:i]))
                if pfad not in ordner_:
                    ordner_[pfad] = self._mtime(pfad)
        return hooks, [str(self.kit / rel) for rel in dateien], list(ordner_.items()), time.monotonic(), abgeschnitten

    @staticmethod
    def _mtime(pfad: str) -> int | None:
        try:
            return os.stat(pfad).st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _eintrag(pfad: str) -> tuple:
        try:
            info = os.stat(pfad)
        except OSError:
            return (pfad, None, None)
        return (pfad, info.st_mtime_ns, info.st_size)

    # ------------------------------------------------------------ Bauplan

    def _scanne(self, kit: Path | None = None, ableitung: Path | None = None, laufzeit: float | None = None) -> dict:
        if kit is None:
            kit = self.kit
            ableitung = self.ableitung_lesen if self._ableitung() is not None else None
        try:
            inhalt, _ = scanner_lauf.scanne(sys.executable, self.scanner / "scan.py", kit, ableitung, self.home,
                                            laufzeit=laufzeit)
        except scanner_lauf.ScanFehler as fehler:
            if fehler.zeit_um and laufzeit is not None and laufzeit < scanner_lauf.LAUFZEIT_HOECHSTENS_S:
                raise WerkstattFehler(503, {"fehler": zeit_um()})  # die Restzeit der Prüfung ist um
            raise WerkstattFehler(500, {"fehler": f"{SCAN_FEHLER}: {fehler.zeile}"})
        return json.loads(inhalt.decode("utf-8"))

    def bauplan(self, laufzeit: float | None = None) -> dict:
        """Mit laufzeit endet alles spätestens danach (Warten auf die Sperre und Einlesen), sonst 503 mit zeit_um().
        Unter der Sperre steht nur die Signatur fest; eingelesen wird ohne sie (außer der Aufrufer hält sie). Ist die
        Signatur danach eine andere, hat sich das Kit beim Einlesen geändert: dann liest er neu ein, höchstens
        BAUPLAN_VERSUCHE-mal, sonst 503 mit KIT_WIRD_GESCHRIEBEN; ein Bauplan aus halb alten, halb neuen Dateien geht
        nie hinaus."""
        ende = None if laufzeit is None else time.monotonic() + laufzeit
        for _ in range(BAUPLAN_VERSUCHE):
            if not self.sperre.acquire(timeout=-1 if ende is None else _rest(ende)):
                raise WerkstattFehler(503, {"fehler": zeit_um()})
            try:
                signatur = self.signatur()
                if self._bauplan is not None and self._bauplan[0] == signatur:
                    return self._bauplan[1]
            finally:
                self.sperre.release()
            atlas = self._scanne(laufzeit=_rest(ende))
            if self.signatur() != signatur:
                continue
            if self.sperre.acquire(blocking=False):
                try:
                    if self.signatur() == signatur:
                        self._bauplan = (signatur, atlas)
                finally:
                    self.sperre.release()
            return atlas
        raise WerkstattFehler(503, {"fehler": KIT_WIRD_GESCHRIEBEN})

    def _probe(self, kit: VirtuellesKit, aenderungen: dict, temp: Path) -> tuple[Path, Path | None]:
        """Legt in temp eine Kopie der Quellen mit den Änderungen an; (Kopie, Ableitung oder None)."""
        kopie = temp / self.kit.name
        pfade.kopiere_quellen(self.kit, self.quellen(), kopie)
        ableitung = None
        for schluessel, (_, neu) in aenderungen.items():
            if schluessel == ABLEITUNG:
                continue
            ziel = pfade.pruefe_ziel(kopie, schluessel)
            if neu is None:
                if ziel.is_file():
                    ziel.unlink()
            else:
                ziel.parent.mkdir(parents=True, exist_ok=True)
                ziel.write_bytes(neu)
        if ABLEITUNG in aenderungen:
            inhalt = aenderungen[ABLEITUNG][1]
        else:
            inhalt = kit.ableitung_als_bytes(kit.ableitung) if kit.ableitung is not None else None
        if inhalt is not None and self._ableitung() is not None:
            ableitung = temp / "ableitung.json"
            ableitung.write_bytes(inhalt)
        return kopie, ableitung

    def _scanne_probe(self, kopie: Path, ableitung: Path | None, laufzeit: float | None, commit: str | None) -> dict:
        atlas = self._scanne(kopie, ableitung, laufzeit)
        atlas["kit"]["quelle"] = self.quelle
        atlas["kit"]["commit"] = commit
        return atlas

    def _bauplan_nach(self, kit: VirtuellesKit, aenderungen: dict, commit: str | None,
                      ende: float | None = None) -> dict:
        """Wendet die Änderungen in einer Temp-Kopie des Kits an und liest sie ein (WS-A3, WS-W10). commit kommt aus
        dem Bauplan vorher, den der Aufrufer unter der Sperre geholt hat; hier wird die Sperre nicht gebraucht."""
        with tempfile.TemporaryDirectory(prefix="atlas-werkstatt-probe-") as temp:
            kopie, ableitung = self._probe(kit, aenderungen, Path(temp))
            return self._scanne_probe(kopie, ableitung, _rest(ende), commit)

    # ------------------------------------------------------------ Status

    def git_wurzel(self) -> Path | None:
        """Git-Wurzel des Kits, gemerkt: eine gefundene, solange dort .git liegt; „keine“ für GIT_WURZEL_NEU_S."""
        gemerkt = self._wurzel
        if gemerkt is not None:
            wurzel, zeit = gemerkt
            if wurzel is not None and os.path.lexists(wurzel / ".git"):
                return wurzel
            if wurzel is None and time.monotonic() - zeit < GIT_WURZEL_NEU_S:
                return None
        wurzel = ablage_modul.git_wurzel(self.kit)
        self._wurzel = (wurzel, time.monotonic())
        return wurzel

    def status(self) -> dict:
        verlauf = self.ablage.verlauf()
        wurzel = self.git_wurzel()
        return {
            "kit": {"id": self.kit_id, "art": self.art, "ordner": self.quelle},
            "schreibbar": self.schreibbar,
            "grund": None if self.schreibbar else NUR_LESEN,
            "git": wurzel is not None,
            "ungespeichert": len(self._ungespeichert(verlauf)),
            "rueckgaengig": any(not s.get("rueckgaengig") for s in verlauf["saetze"]),
            "verlauf": [{"titel": s.get("titel"), "zeit": s.get("zeit"), "rueckgaengig": bool(s.get("rueckgaengig"))}
                        for s in verlauf["saetze"]],
            "vorschlaege": len(self.vorschlaege.liste()),
            "alter_verlauf": self._alter_status(),
            "zu_speichern": {**self._zu_speichern_liste(verlauf, wurzel), **self._gedaechtnis_ungespeichert(verlauf),
                             **self._kontext_ungespeichert(verlauf)},
        }

    def _gedaechtnis_ungespeichert(self, verlauf: dict) -> dict:
        pfade_ = sorted({p for s in verlauf["saetze"] if not s.get("rueckgaengig") and s.get("sitzung") == self.sitzung
                         for p in s.get("gedaechtnis") or [] if isinstance(p, str)})
        return {"gedaechtnis": pfade_} if pfade_ else {}

    def _kontext_ungespeichert(self, verlauf: dict) -> dict:
        pfade_ = sorted({p for s in verlauf["saetze"] if not s.get("rueckgaengig") and s.get("sitzung") == self.sitzung
                         for p in s.get("kontext") or [] if isinstance(p, str)})
        return {"kontext": pfade_} if pfade_ else {}

    @staticmethod
    def _kontext_kit(eintraege: list[dict]) -> set[str]:
        return {p for u in eintraege for p in u.get("kontext_kit") or [] if isinstance(p, str)}

    def _pfad_von(self, schluessel: str, kontext_kit: set[str]) -> Path:
        if schluessel in kontext_kit:
            return ks.pfad_im_kit(self.kit, schluessel)
        return self.ablage.pfad(schluessel)

    # ------------------------------------------------------------ Alter Verlauf (WS-B8)

    def _alte_orte(self) -> list[Path]:
        return [o for o in (self.alter_startordner, self.home) if o is not None]

    def neuer_startordner(self, ordner: Path) -> None:
        with self.sperre:
            self.alter_startordner = Path(os.path.abspath(ordner))
            self._alter_fund = None

    def _alter(self) -> werkstatt_alt.Fund | None:
        if self._alter_fund is None:
            fund = None
            if self.daten is not None and self.schreibbar:
                fund = werkstatt_alt.finde(self.kit, self.ergebnis, self._alte_orte(), self.daten,
                                           self.ablage.geheimnis, (self.ordner.skills, self.ordner.agenten))
            self._alter_fund = (fund,)
        return self._alter_fund[0]

    def _alter_status(self) -> dict | None:
        gemerkt = self._alter_fund
        if gemerkt is not None:
            fund = gemerkt[0]
        else:
            with self.sperre:
                fund = self._alter()
        return fund.fuer_status(self.home) if fund is not None else None

    def alter_verlauf(self, aktion: object) -> dict:
        with self.sperre:
            fund = self._alter()
            if fund is None:
                raise WerkstattFehler(404, {"fehler": texte.KEIN_ALTER_VERLAUF})
            if aktion not in ("uebernehmen", "ablehnen"):
                raise WerkstattFehler(400, {"fehler": "aktion: erwartet uebernehmen oder ablehnen."})
            antwort: dict = {"ok": True}
            if aktion == "uebernehmen":
                if self.ablage.verlauf()["saetze"]:
                    raise WerkstattFehler(409, {"fehler": texte.eigener_verlauf(finden.tilde(fund.ort, self.home))})
                try:
                    werkstatt_alt.uebernimm(fund, self.ergebnis)
                except werkstatt_alt.UebernahmeFehler as fehler:
                    raise WerkstattFehler(409, {"fehler": fehler.text})
                antwort["meldung"] = texte.alter_verlauf_uebernommen(finden.tilde(fund.ort, self.home))
            werkstatt_alt.merke(self.daten, self.kit, fund.ort)
            self._alter_fund = (None,)
            return antwort

    def _zu_speichern_liste(self, verlauf: dict, wurzel: Path | None) -> dict:
        """Was Speichern committen würde, nach derselben Auswahl wie speichern(); der Rest steht in nicht_im_commit
        (nicht im Repo versioniert, von git ignoriert, unverändert oder nicht in diesem Serverlauf geschrieben)."""
        saetze = self._ungespeichert(verlauf)
        kontext_kit = self._kontext_kit(saetze)
        schluessel = sorted({s for u in saetze for s in u.get("dateien", [])} | kontext_kit)
        git_fehler = None
        if wurzel is None:
            im_commit = [s for s in schluessel if s != ABLEITUNG]
        else:
            try:
                im_commit = self._im_commit_gemerkt(schluessel, wurzel, kontext_kit)
            except ablage_modul.GitFehler as fehler:
                im_commit, git_fehler = [], git_antwortet_nicht(str(fehler))
        return {"saetze": [str(u.get("titel", "")) for u in saetze],
                "dateien": sorted(anzeige(s, self.ableitung_pfad) for s in im_commit),
                "nicht_im_commit": sorted(anzeige(s, self.ableitung_pfad) for s in schluessel if s not in im_commit),
                **({"git_fehler": git_fehler} if git_fehler else {})}

    def _im_commit_gemerkt(self, schluessel: list[str], wurzel: Path, kontext_kit: set[str] | None = None) -> list[str]:
        """_zu_speichern für den Status, der bei jedem Ereignis kommt: ohne git, solange sich weder die Dateien noch
        Index und HEAD geändert haben, höchstens ZU_SPEICHERN_GILT_S. Speichern selbst fragt git immer."""
        if not schluessel:
            return []
        git_ordner = self._git_ordner(wurzel)
        dateien = []
        kontext_kit = kontext_kit or set()
        for s in schluessel:
            try:
                dateien.append(self._eintrag(str(self._pfad_von(s, kontext_kit))))
            except PfadFehler:
                dateien.append((s, None, None))
        stand = (wurzel, tuple(schluessel), tuple(s in self.geschrieben for s in schluessel), tuple(dateien),
                 self._eintrag(str(git_ordner / "index")), *self._git_stand())
        gemerkt = self._zu_speichern_gemerkt
        if gemerkt is not None and gemerkt[0] == stand and time.monotonic() - gemerkt[1] < ZU_SPEICHERN_GILT_S:
            return list(gemerkt[2])
        im_commit = [s for s, _ in self._zu_speichern(schluessel, wurzel, kontext_kit)]
        self._zu_speichern_gemerkt = (stand, time.monotonic(), tuple(im_commit))
        return im_commit

    # ------------------------------------------------------------ Agent fürs Formular (WS-A13)

    def abschnitte(self) -> dict:
        """Je Skill mit abgeleitetem Ablauf und je Schritt: steht seine Belegzeile als Überschrift im Text (WS-W6)?
        Nur dort kann die Werkstatt von Hand einen eigenen Abschnitt einfügen; die Seite bietet sonst „Mit KI einfügen“."""
        with self.sperre:
            ableitung = self._ableitung()
            ergebnis: dict[str, dict[str, bool]] = {}
            if not ableitung:
                return {"skills": ergebnis}
            kit = VirtuellesKit(self.ordner, self.kit_id, None, None)
            dateien = kit.skills()
            for skill, eintrag in (ableitung.get("skills") or {}).items():
                rel = dateien.get(skill)
                schritte = eintrag.get("schritte") if isinstance(eintrag, dict) else None
                if rel is None or not isinstance(schritte, list):
                    continue
                try:
                    kit.pruefe_ziel(rel)
                    zeilen = kit.textdatei(rel).zeilen
                except SchreibFehler:
                    continue
                je_schritt = {}
                for s in schritte:
                    if not isinstance(s, dict) or not isinstance(s.get("id"), str):
                        continue
                    zeile = belegzeile(s.get("beleg"))
                    je_schritt[s["id"]] = (zeile is not None and 1 <= zeile <= len(zeilen)
                                           and ueberschrift(zeilen[zeile - 1]) is not None)
                ergebnis[skill] = je_schritt
            return {"skills": ergebnis}

    def agent(self, name: object) -> dict:
        if not isinstance(name, str) or not aenderungssatz.NAME.fullmatch(name):
            raise WerkstattFehler(400, {"fehler": AGENT_NAME_UNGUELTIG})
        with self.sperre:
            kit = VirtuellesKit(self.ordner, self.kit_id, None, None)
            rel = kit.agenten().get(name)
            if rel is None:
                raise WerkstattFehler(404, {"fehler": f"Den Agenten {name} gibt es nicht."})
            # WS-A13/WS-X5: gelesen wird nur, was der Pfadschutz auch schreiben ließe (kein Symlink auf dem Weg).
            try:
                kit.pruefe_ziel(rel)
                datei = kit.textdatei(rel)
            except SchreibFehler as fehler:
                raise WerkstattFehler(409, {"fehler": f"Den Agenten {name} liest die Werkstatt nicht: {fehler}"})
            try:
                werte = datei.frontmatter().werte()
            except ZeilenFehler:
                werte = {}
            text = agent_text(datei)
        atlas = werte.get("atlas") if isinstance(werte.get("atlas"), dict) else {}
        rolle = atlas.get("rolle") if atlas.get("rolle") in vokabular.ROLLEN else None
        model = werte.get("model")
        beschreibung = werte.get("description")
        memory = werte.get("memory")
        return {"name": name, "datei": rel, "beschreibung": str(beschreibung) if beschreibung is not None else "",
                "rolle": rolle, "tools": _werkzeuge(werte.get("tools")),
                "model": str(model) if model is not None else None,
                "memory": str(memory) if memory is not None else None, "text": text,
                "prompt": agent_prompt.gliedere(text.split("\n")),  # WS-PT2
                "stand": text_stand(text)}  # Review r20-2

    def agenten_memory(self) -> tuple[dict[str, str | None], list[str]]:
        signatur = self.signatur()
        gemerkt = self._agenten_memory
        if gemerkt is not None and gemerkt[0] == signatur:
            return dict(gemerkt[1]), list(gemerkt[2])
        werte_je: dict[str, str | None] = {}
        nicht_lesbar: list[str] = []
        with self.sperre:
            kit = VirtuellesKit(self.ordner, self.kit_id, None, None)
            for pfad in sorted(self.ordner.agent_dateien()):
                rel = pfad.relative_to(self.kit).as_posix()
                try:
                    kit.pruefe_ziel(rel)
                    werte = Textdatei.aus_bytes(pfad.read_bytes()).frontmatter().werte()
                except (SchreibFehler, OSError, UnicodeDecodeError, ZeilenFehler):
                    werte_je.setdefault(pfad.stem, None)
                    nicht_lesbar.append(pfad.stem)
                    continue
                name = werte.get("name") if isinstance(werte.get("name"), str) and werte.get("name") else pfad.stem
                memory = werte.get("memory")
                werte_je.setdefault(name, None if memory is None else str(memory))
        self._agenten_memory = (signatur, dict(werte_je), list(nicht_lesbar))
        return werte_je, nicht_lesbar

    def leitplanken(self) -> dict:
        """GET /api/leitplanken (WS-L7)."""
        bauplan = self.bauplan()
        with self.sperre:
            return leitplanken_modul.uebersicht(self.ordner, self.kit, bauplan, self.schreibbar)

    def leitplanken_skript(self, pfad: object) -> dict:
        """GET /api/leitplanken/skript (WS-L7)."""
        with self.sperre:
            try:
                return leitplanken_modul.lies_skript(self.kit, self.ordner, pfad)
            except leitplanken_modul.LeitplankenFehler as fehler:
                raise WerkstattFehler(fehler.status, {"fehler": fehler.text})

    def gliederung(self, text: object) -> dict:
        """POST /api/gliederung (WS-PT9): die Gliederung eines Texts, wie sie GET /api/agent liefert. Nur lesen, nichts
        im Kit; das Formular vergleicht damit, ob seine Teile beim nächsten Lesen dieselben bleiben."""
        if not isinstance(text, str) or len(text) > aenderungssatz.TEXT_HOECHSTENS:
            raise WerkstattFehler(400, {"fehler": "text: erwartet einen Text mit höchstens 20000 Zeichen."})
        return {"prompt": agent_prompt.gliedere(text.split("\n"))}

    def _ungespeichert(self, verlauf: dict) -> list[dict]:
        return [u for u in verlauf["ungespeichert"] if u.get("sitzung") == self.sitzung]

    # ------------------------------------------------------------ Vorschläge (WS-A7)

    def lege_vor(self, satz: object, herkunft: str, pipeline: dict | None = None) -> str:
        self._pruefe_form(satz)
        try:
            return self.vorschlaege.lege_vor(satz, herkunft, pipeline=pipeline)
        except VorschlagFehler as fehler:
            raise WerkstattFehler(fehler.status, {"fehler": fehler.text})

    def lege_vor_kontext(self, satz: dict, ziele: dict[str, ks.KontextZiel]) -> str:
        self._pruefe_form(satz, kontext_erlaubt=True)
        try:
            return self.vorschlaege.lege_vor(satz, HAND, kontext=ziele)
        except VorschlagFehler as fehler:
            raise WerkstattFehler(fehler.status, {"fehler": fehler.text})

    def lege_vor_gedaechtnis(self, satz: dict) -> str:
        self._pruefe_form(satz, gedaechtnis_erlaubt=True)
        try:
            return self.vorschlaege.lege_vor(satz, KI, gedaechtnis=True)
        except VorschlagFehler as fehler:
            raise WerkstattFehler(fehler.status, {"fehler": fehler.text})

    def verwirf(self, vid: object) -> None:
        if not self.vorschlaege.entferne(vid, {"art": "verworfen"}):
            raise WerkstattFehler(404, {"fehler": "Diesen Vorschlag gibt es nicht (mehr)."})

    def _vorschlag(self, vid: object) -> dict:
        try:
            return self.vorschlaege.hole(vid)
        except VorschlagFehler as fehler:
            raise WerkstattFehler(fehler.status, {"fehler": fehler.text})

    # ------------------------------------------------------------ Vorschau

    def _pruefe_form(self, satz: object, gedaechtnis_erlaubt: bool = False, kontext_erlaubt: bool = False) -> dict:
        try:
            return aenderungssatz.pruefe(satz, gedaechtnis_erlaubt, kontext_erlaubt, self.ordner.hook_ordner)
        except aenderungssatz.SatzFehler as fehler:
            raise WerkstattFehler(400, {"fehler": "Der Änderungssatz ist ungültig.", "meldungen": fehler.meldungen})

    def _satz_und_auswahl(self, satz: object, auswahl: object, satz_id: object,
                          gedaechtnis_erlaubt: bool = False, kontext_erlaubt: bool = False) -> tuple[dict, set[str]]:
        if satz_id is not None:
            vorschlag = self._vorschlag(satz_id)
            satz = vorschlag["satz"]
            gedaechtnis_erlaubt = bool(vorschlag.get("gedaechtnis"))
            kontext_erlaubt = bool(vorschlag.get("kontext"))
        self._pruefe_form(satz, gedaechtnis_erlaubt, kontext_erlaubt)
        ids = [e["id"] for e in satz["eintraege"]]
        if auswahl is None:
            return satz, set(ids)
        if not isinstance(auswahl, list) or not all(isinstance(a, str) for a in auswahl) or set(auswahl) - set(ids):
            raise WerkstattFehler(400, {"fehler": "Die Auswahl nennt Einträge, die es im Satz nicht gibt."})
        return satz, set(auswahl)

    @classmethod
    def _anbieter(cls, satz: dict, gewaehlt: set[str], fehlt: tuple) -> dict | None:
        return next(cls._anbietende(satz, gewaehlt, fehlt), None)

    @staticmethod
    def _anbietende(satz: dict, gewaehlt: set[str], fehlt: tuple):
        """Die nicht gewählten Einträge, die fehlt anlegen oder ändern könnten, in der Reihenfolge des Satzes."""
        for e in satz["eintraege"]:
            if e["id"] in gewaehlt:
                continue
            art, op = aenderungssatz.operation(e)
            if fehlt[0] == "agent" and (art == "agent_anlegen" and op["name"] == fehlt[1]
                                        or art == "agent_aendern" and op.get("neuer_name") == fehlt[1]):
                yield e
            if fehlt[0] == "skill" and art == "skill_anlegen" and op["name"] == fehlt[1]:
                yield e
            if fehlt[0] == "schritt" and art == "schritt_einfuegen" and op["skill"] == fehlt[1] \
                    and op["schritt"]["id"] == fehlt[2]:
                yield e
            if fehlt[0] == "zitat" and art == "text_ersetzen" and op["datei"] == fehlt[1] and fehlt[2] in op["neu"]:
                yield e
            if fehlt[0] == "zitat_weg" and art == "text_ersetzen" and op["datei"] == fehlt[1] \
                    and (fehlt[2] in op["alt"] or op["alt"].strip() in fehlt[2]):
                yield e
            if fehlt[0] == "zeile" and art == "text_ersetzen" and op["datei"] == fehlt[1] \
                    and (fehlt[2].strip() in op["alt"] or op["alt"].strip() in fehlt[2]):
                yield e
            if fehlt[0] == "beleg" and op.get("skill") == fehlt[1] \
                    and fehlt[2] in (op.get("id"), op.get("von")):
                yield e

    def _berechne(self, satz: dict, gewaehlt: set[str]) -> tuple[_Berechnung, VirtuellesKit]:
        ergebnis = _Berechnung()
        ableitung = self._ableitung()
        kit = VirtuellesKit(self.ordner, self.kit_id, self.ableitung_lesen if ableitung is not None else None, ableitung)
        schreiber_ = Schreiber(kit)
        nr = ergebnis.nummern = _nummern(satz)
        for e in satz["eintraege"]:
            art, op = aenderungssatz.operation(e)
            info = {"id": e["id"], "beschreibung": e["beschreibung"], "operation": art,
                    "gewaehlt": e["id"] in gewaehlt, "dateien": []}
            ergebnis.eintraege.append(info)
            if e["id"] not in gewaehlt:
                continue
            if art == "text_ersetzen":
                info["zeilen"] = _zeilen_von(kit.lies(op["datei"]), op["alt"])
            try:
                schreiber_.wende_an(art, op, e["id"])
            except SchreibFehler as fehler:
                anbieter = self._anbieter(satz, gewaehlt, fehler.fehlt) if fehler.fehlt else None
                text = braucht(nr[e["id"]], nr[anbieter["id"]], anbieter["beschreibung"]) if anbieter else fehler.text
                ergebnis.fehler.append({"eintrag": e["id"], "text": text})
                continue
            info["dateien"] = [anzeige(s, self.ableitung_pfad) for s in schreiber_.zuletzt_geaendert]
        for eid, skript in schreiber_.hook_skripte_aufraeumen():
            next(i for i in ergebnis.eintraege if i["id"] == eid)["dateien"].append(anzeige(skript, self.ableitung_pfad))
        ergebnis.rueck_dateien = {neu: alt for alt, neu in schreiber_.buch.umbenannt.items()}
        ergebnis.rueck_namen = {neu: wer[1] for neu, wer in schreiber_.buch.wer_heisst.items()
                                if wer[0] == "kit" and wer[1] != neu}
        ergebnis.letzter = next((e["id"] for e in reversed(satz["eintraege"]) if e["id"] in gewaehlt), "")
        try:
            self._nachpruefen(ergebnis, satz, gewaehlt, schreiber_)
        except (SchreibFehler, ZeilenFehler) as fehler:
            ergebnis.fehler.append({"eintrag": ergebnis.letzter, "text": nicht_nachpruefbar(str(fehler))})
        self._gruppen(ergebnis, satz, gewaehlt)
        ergebnis.rechte = self._rechte(satz, gewaehlt, kit)
        ergebnis.leitplanken, ergebnis.skripte, nicht_gezeigt = leitplanken_modul.vorschau_teile(satz, gewaehlt, kit)
        gescheitert = {f["eintrag"] for f in ergebnis.fehler}
        ergebnis.fehler += [f for f in nicht_gezeigt if f["eintrag"] not in gescheitert]
        ergebnis.aenderungen = kit.aenderungen()
        ergebnis.gelesen = dict(kit.gelesen)
        return ergebnis, kit

    @staticmethod
    def _memory_von(inhalt: bytes | None) -> object:
        if inhalt is None:
            return None
        try:
            return Textdatei.aus_bytes(inhalt).frontmatter().werte().get("memory")
        except (UnicodeDecodeError, ZeilenFehler):
            return None

    @staticmethod
    def _rechte(satz: dict, gewaehlt: set[str], kit: VirtuellesKit) -> list[dict]:
        warnungen: list[dict] = []
        vorher_agenten = VirtuellesKit(kit.ordner, kit.kit_id, None, None).agenten()
        agenten = kit.agenten()
        for e in satz["eintraege"]:
            if e["id"] not in gewaehlt:
                continue
            art, op = aenderungssatz.operation(e)
            if art == "agent_anlegen" and op.get("memory"):
                hatte = False
            elif art == "agent_aendern" and op.get("memory") is not None:
                rel_vorher = vorher_agenten.get(op["name"])
                hatte = rel_vorher is not None and Werkstatt._memory_von(kit.von_platte(rel_vorher)) is not None
            else:
                continue
            name = op.get("neuer_name") or op["name"]
            rel = agenten.get(name)
            inhalt = kit.lies(rel) if rel is not None else None
            if hatte or inhalt is None or any(w.get("agent") == name for w in warnungen):
                continue
            try:
                werte = Textdatei.aus_bytes(inhalt).frontmatter().werte()
            except (UnicodeDecodeError, ZeilenFehler):
                continue
            tools = _werkzeuge(werte.get("tools"))
            verbote = _werkzeuge(werte.get("disallowedTools")) or []
            if (tools is not None and not set(SCHREIBRECHTE) & set(tools)) or set(SCHREIBRECHTE) & set(verbote):
                warnungen.append({"pfad": rel, "art": "gedaechtnis_rechte", "agent": name,
                                  "text": darf_nicht_schreiben(name)})
        return warnungen

    def _nachpruefen(self, ergebnis: _Berechnung, satz: dict, gewaehlt: set[str], schreiber_: Schreiber) -> None:
        """Nach allen Einträgen: gelöschte Agenten, die noch genutzt, aufgerufen oder genannt werden, und Weggefallenes,
        das der Text noch sagt. Belege, die nicht mehr stimmen, meldet der Neuscan (vorschau)."""
        nr = ergebnis.nummern
        genutzte = schreiber_.noch_genutzt()
        for eid, (name, *stellen) in genutzte.items():
            ergebnis.fehler.append({"eintrag": eid, "text": genutzt(name, stellen)})
        aufrufe = schreiber_.noch_aufgerufen()
        for eid, name, rel, zeile, text in aufrufe:
            if eid not in genutzte:
                self._melde(ergebnis, satz, gewaehlt, eid, ("zeile", rel, text), aufgerufen(name, rel, zeile, text))
        for eid, rel, text, zeile, art, was in schreiber_.noch_im_text():
            meldung = noch_gesagt(nr[eid], rel, zeile, was) if art == "zitat" else noch_gerufen(rel, zeile, was)
            self._melde(ergebnis, satz, gewaehlt, eid, ("zeile", rel, text), meldung)
        gemeldet = {(name, rel) for _, name, rel, _, _ in aufrufe}
        ergebnis.nennungen = [{"pfad": rel, "art": "nennung", "text": genannt(name, rel, zeile, text)}
                              for name, rel, zeile, text in schreiber_.nennungen() if (name, rel) not in gemeldet]
        ergebnis.nennungen += [{"pfad": rel, "art": "nennung", "text": text}
                               for rel, _, text in schreiber_.wegfall_hinweise()]
        ergebnis.nennungen += [{"pfad": rel, "art": "nennung", "text": nicht_umbenannt(rel, name, grund)}
                               for rel, name, grund in schreiber_.offene_nicht_umbenannt()]
        einmal: list[dict] = []
        for f in ergebnis.fehler:
            if f not in einmal:
                einmal.append(f)
        ergebnis.fehler = einmal

    def _melde(self, ergebnis: _Berechnung, satz: dict, gewaehlt: set[str], eid: str, fehlt: tuple | None,
               text: str) -> None:
        anbieter = self._anbieter(satz, gewaehlt, fehlt) if fehlt else None
        nr = ergebnis.nummern
        ergebnis.fehler.append({"eintrag": eid, "text": braucht(nr[eid], nr[anbieter["id"]], anbieter["beschreibung"])
                                if anbieter else text})

    @staticmethod
    def _gruppen(ergebnis: _Berechnung, satz: dict, gewaehlt: set[str]) -> None:
        """WS-M9: Einträge derselben gruppe sind nur zusammen wählbar."""
        for e in satz["eintraege"]:
            if e["id"] not in gewaehlt or "gruppe" not in e:
                continue
            fehlt = next((a for a in satz["eintraege"] if a.get("gruppe") == e["gruppe"] and a["id"] not in gewaehlt), None)
            if fehlt is not None:
                nr = ergebnis.nummern
                ergebnis.fehler.append({"eintrag": e["id"], "text": braucht(nr[e["id"]], nr[fehlt["id"]],
                                                                            fehlt["beschreibung"])})

    def _stand(self, berechnung: _Berechnung, satz: dict, gewaehlt: set[str]) -> str:
        """Bindet die Bestätigung an das Gesehene: gelesene Dateien, Ableitung, Satz und Auswahl."""
        teile: list = sorted(berechnung.gelesen.items(), key=lambda p: p[0])
        teile += [(ABLEITUNG, pfade.sha(self.ablage.lies(ABLEITUNG)) if self.ableitung_lesen.is_file() else None)]
        teile += [("@satz", satz), ("@auswahl", sorted(gewaehlt))]
        return hashlib.sha256(json.dumps(teile, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _warnungen(self, berechnung: _Berechnung) -> list[dict]:
        verweise = [w for schluessel, (alt, neu) in sorted(berechnung.aenderungen.items())
                    if schluessel != ABLEITUNG and alt is not None and neu is not None and schluessel.endswith("SKILL.md")
                    for w in schrittnummern_verweise(schluessel, alt, neu)]
        return self._warnungen_ohne_verweise(berechnung) + verweise

    def _warnungen_ohne_verweise(self, berechnung: _Berechnung) -> list[dict]:
        return berechnung.leitplanken + self._warnungen_ohne_leitplanken(berechnung)

    def _warnungen_ohne_leitplanken(self, berechnung: _Berechnung) -> list[dict]:
        wurzel = self.git_wurzel()
        if wurzel is None:
            return list(berechnung.nennungen) + berechnung.rechte
        eigene = self.ablage.verlauf()["eigene"]
        fraglich = [schluessel for schluessel, (alt, _) in sorted(berechnung.aenderungen.items())
                    if schluessel != ABLEITUNG and alt is not None and eigene.get(schluessel) != pfade.sha(alt)]
        try:
            geaendert = ablage_modul.ungespeichert_im_git(wurzel, [self.kit / s for s in fraglich])
        except ablage_modul.GitFehler as fehler:
            # Vorsichtshalber: jede fragliche Datei könnte fremde Änderungen haben.
            return [{"pfad": s, "text": fremde_aenderung_ungeprueft(s, str(fehler))} for s in fraglich] \
                + berechnung.nennungen + berechnung.rechte
        warnungen = [{"pfad": s, "text": fremde_aenderung(s)} for s in fraglich if self.kit / s in geaendert]
        return warnungen + berechnung.nennungen + berechnung.rechte

    def vorschau(self, satz: object = None, auswahl: object = None, satz_id: object = None,
                 mit_bauplan: bool = False) -> dict:
        """Harte Fehler kommen aus derselben Prüfung wie beim Übernehmen (WS-W10): Der Scanner liest das Kit mit den
        Änderungen, jeder neue Befund ist ein Fehler. Unter der Sperre läuft nur die Berechnung im Speicher; Bauplan
        vorher, Probe-Kopie und Einlesen laufen ohne sie, die Seite wartet währenddessen nicht. Der Bauplan danach
        bleibt je (Änderungen, Signatur, Commit) gemerkt, auch für Übernehmen und aus der Prüfung eines Satzes der KI."""
        satz, gewaehlt = self._satz_und_auswahl(satz, auswahl, satz_id)
        if kontext_modul.ist_kontext_satz(satz):
            return self._vorschau_kontext(satz, satz_id, mit_bauplan)
        if bereinigen.ist_gedaechtnis_satz(satz):
            return self._vorschau_gedaechtnis(satz, gewaehlt, mit_bauplan)
        with self.sperre:
            berechnung, kit = self._berechne(satz, gewaehlt)
            dateien = []
            for schluessel, (alt, neu) in sorted(berechnung.aenderungen.items()):
                pfad = anzeige(schluessel, self.ableitung_pfad)
                art = "neu" if alt is None else "geloescht" if neu is None else "geaendert"
                dateien.append({"pfad": pfad, "art": art, "diff": diff(pfad, alt, neu), "im_kit": schluessel != ABLEITUNG})
            antwort = {"eintraege": berechnung.eintraege, "dateien": dateien, "fehler": berechnung.fehler,
                       "warnungen": self._warnungen(berechnung), "stand": self._stand(berechnung, satz, gewaehlt),
                       "skripte": berechnung.skripte}
            probe = bool(berechnung.aenderungen) and not berechnung.fehler
            signatur = self.signatur() if probe else None
        nachher = None
        if probe:
            vorher = self._bauplan_vorher()
            try:
                neue, nachher = self._neue_befunde(berechnung, kit, vorher, signatur)
                if neue:
                    antwort["fehler"] = self._befund_fehler(satz, gewaehlt, berechnung, neue, vorher, nachher)
                antwort["fehler"] = antwort["fehler"] + leitplanken_modul.ohne_bewacht(satz, gewaehlt, nachher)
            except (PfadFehler, pfade.KopieFehler) as fehler:
                raise WerkstattFehler(409, {"fehler": str(fehler)})
        if mit_bauplan:
            antwort["bauplan_nachher"] = nachher
        return antwort

    @staticmethod
    def _stand_gedaechtnis(satz: dict, gewaehlt: set[str], gelesen: dict[str, bytes | None]) -> str:
        teile = ["@gedaechtnis", satz, sorted(gewaehlt), sorted((s, pfade.sha(i)) for s, i in gelesen.items())]
        return hashlib.sha256(json.dumps(teile, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _vorschau_gedaechtnis(self, satz: dict, gewaehlt: set[str], mit_bauplan: bool) -> dict:
        lauf_offen = self.lauf_offen()
        with self.sperre:
            b = bereinigen.berechne(satz, gewaehlt, self.register, self.verboten, lauf_offen)
            antwort = {"eintraege": b.eintraege, "dateien": b.dateien, "fehler": b.fehler, "warnungen": b.warnungen,
                       "stand": self._stand_gedaechtnis(satz, gewaehlt, b.gelesen)}
        if mit_bauplan:
            antwort["bauplan_nachher"] = self._bauplan_vorher()
        return antwort

    def _anwenden_gedaechtnis(self, satz: dict, gewaehlt: set[str], stand: object, bestaetigt: bool, satz_id: object,
                              herkunft: str) -> dict:
        lauf_offen = self.lauf_offen()
        with self.sperre:
            for e in satz["eintraege"]:
                if e["id"] not in gewaehlt:
                    continue
                _, op = aenderungssatz.operation(e)
                try:
                    gd.pruefe(self.register.hole(op["gedaechtnis_id"]), op["datei"], self.verboten)
                except gd.GedaechtnisFehler as fehler:
                    raise WerkstattFehler(fehler.status, {"fehler": fehler.text})
            b = bereinigen.berechne(satz, gewaehlt, self.register, self.verboten, lauf_offen)
            if b.seit_vorschau:
                raise WerkstattFehler(409, {"fehler": b.seit_vorschau[0]})
            if b.fehler:
                raise WerkstattFehler(409, {"fehler": AUSWAHL_FEHLER, "eintraege": b.fehler})
            if stand != self._stand_gedaechtnis(satz, gewaehlt, b.gelesen):
                raise WerkstattFehler(409, {"fehler": STAND_ALT})
            if b.warnungen and not bestaetigt:
                raise WerkstattFehler(409, {"fehler": BESTAETIGEN, "warnungen": b.warnungen})
            if not b.aenderungen:
                raise WerkstattFehler(409, {"fehler": NICHTS_GEWAEHLT})
            self.ablage.ziele.update(b.ziele)
            eintraege = [e for e in satz["eintraege"] if e["id"] in gewaehlt]
            basis = self.ergebnis / ablage_modul.ORDNER / ablage_modul.SICHERUNG
            basis_neu = not os.path.lexists(basis)
            try:
                sicherung = self.ablage.sichere(satz["titel"], herkunft, eintraege, b.aenderungen)
            except (OSError, PfadFehler, ergebnis_modul.NichtSchreibbar):
                raise self._nicht_schreibbar()
            try:
                verlauf = self._verlauf_mit(satz, herkunft, sicherung, b, gedaechtnis=[b.anzeigen[s]
                                                                                       for s in sorted(b.aenderungen)])
            except (OSError, ablage_modul.AblageFehler):
                self.ablage.entferne_sicherung(sicherung)
                raise self._nicht_schreibbar()
            try:
                self._schreibe_mit_verlauf(sicherung, {s: neu for s, (_, neu) in b.aenderungen.items()}, verlauf,
                                           erwartet={s: alt for s, (alt, _) in b.aenderungen.items()})
            except WerkstattFehler:
                if basis_neu:
                    try:
                        basis.rmdir()
                    except OSError:
                        pass
                raise
            if satz_id is not None:
                self.vorschlaege.entferne(satz_id, {"art": "uebernommen", "gewaehlt": len(eintraege),
                                                    "von": len(satz["eintraege"])})
            return {"ok": True, "titel": satz["titel"], "dateien": [b.anzeigen[s] for s in sorted(b.aenderungen)],
                    "meldung": bereinigen.meldung_bereinigt(b.entfernt)}

    def _kontext_ziel(self, satz_id: object) -> ks.KontextZiel:
        ziele = self.vorschlaege.kontext_ziele(satz_id)
        if not ziele or "k1" not in ziele:
            raise WerkstattFehler(404, {"fehler": "Diesen Vorschlag gibt es nicht (mehr)."})
        return ziele["k1"]

    def _kontext_berechnet(self, satz: dict, ziel: ks.KontextZiel, lauf_offen: bool) -> kontext_modul.Berechnung:
        try:
            return kontext_modul.berechne(satz, ziel, self.verboten, self.kit, lauf_offen)
        except ks.KontextFehler as fehler:
            raise WerkstattFehler(fehler.status, {"fehler": fehler.text})

    @staticmethod
    def _stand_kontext(satz: dict, b: kontext_modul.Berechnung) -> str:
        teile = ["@kontext", satz, sorted((s, pfade.sha(alt)) for s, (alt, _) in b.aenderungen.items())]
        return hashlib.sha256(json.dumps(teile, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _vorschau_kontext(self, satz: dict, satz_id: object, mit_bauplan: bool) -> dict:
        ziel = self._kontext_ziel(satz_id)
        lauf_offen = self.lauf_offen()
        with self.sperre:
            b = self._kontext_berechnet(satz, ziel, lauf_offen)
            antwort = {"eintraege": b.eintraege, "dateien": b.dateien, "fehler": b.fehler, "warnungen": b.warnungen,
                       "stand": self._stand_kontext(satz, b)}
        if mit_bauplan:
            antwort["bauplan_nachher"] = self._bauplan_vorher()
        return antwort

    def _anwenden_kontext(self, satz: dict, gewaehlt: set[str], stand: object, bestaetigt: bool, satz_id: object,
                          herkunft: str) -> dict:
        if not gewaehlt:
            raise WerkstattFehler(409, {"fehler": NICHTS_GEWAEHLT})
        ziel = self._kontext_ziel(satz_id)
        lauf_offen = self.lauf_offen()
        with self.sperre:
            b = self._kontext_berechnet(satz, ziel, lauf_offen)
            if stand != self._stand_kontext(satz, b):
                raise WerkstattFehler(409, {"fehler": STAND_ALT})
            if b.warnungen and not bestaetigt:
                raise WerkstattFehler(409, {"fehler": BESTAETIGEN, "warnungen": b.warnungen})
            if not b.aenderungen:
                raise WerkstattFehler(409, {"fehler": NICHTS_GEWAEHLT})
            self.ablage.kontext_ziele[ziel.id] = ziel
            eintraege = kontext_modul.ohne_inhalt(satz["eintraege"])
            try:
                sicherung = self.ablage.sichere(satz["titel"], herkunft, eintraege, b.aenderungen)
            except (OSError, PfadFehler, ergebnis_modul.NichtSchreibbar):
                raise self._nicht_schreibbar()
            try:
                verlauf = self._verlauf_kontext(satz, herkunft, sicherung, b)
            except (OSError, ablage_modul.AblageFehler):
                self.ablage.entferne_sicherung(sicherung)
                raise self._nicht_schreibbar()
            self._schreibe_mit_verlauf(sicherung, {s: neu for s, (_, neu) in b.aenderungen.items()}, verlauf,
                                       erwartet={s: alt for s, (alt, _) in b.aenderungen.items()})
            self.geschrieben |= set(b.kontext_kit)
            if satz_id is not None:
                self.vorschlaege.entferne(satz_id, {"art": "uebernommen", "gewaehlt": len(eintraege),
                                                    "von": len(satz["eintraege"])})
            return {"ok": True, "titel": satz["titel"], "dateien": [b.anzeigen[s] for s in sorted(b.aenderungen)],
                    "meldung": f"{ziel.datei} geändert."}

    def _verlauf_kontext(self, satz: dict, herkunft: str, sicherung: Path, b: kontext_modul.Berechnung) -> dict:
        verlauf = self.ablage.verlauf()
        _, info = self.ablage.info(sicherung.name)
        eintrag = {"id": sicherung.name, "titel": satz["titel"], "herkunft": herkunft, "zeit": info["zeit"],
                   "sicherung": sicherung.name, "dateien": sorted(b.aenderungen), "rueckgaengig": False,
                   "sitzung": self.sitzung}
        if b.kontext_kit:
            eintrag["kontext_kit"] = list(b.kontext_kit)
            verlauf["ungespeichert"].append({"id": sicherung.name, "titel": satz["titel"], "dateien": [],
                                             "kontext_kit": list(b.kontext_kit), "sitzung": self.sitzung})
        else:
            eintrag["kontext"] = [b.anzeigen[s] for s in sorted(b.aenderungen)]
        verlauf["saetze"].append(eintrag)
        return verlauf

    def _anzeige(self, schluessel: str) -> str:
        if ks.ist_schluessel(schluessel):
            try:
                return self.ablage.kontext_ziel(schluessel).anzeige
            except PfadFehler:
                return schluessel
        if gd.ist_schluessel(schluessel):
            try:
                ziel, datei = self.ablage.ziel(schluessel)
                return gd.tilde(Path(ziel.ordner) / datei, self.home)
            except PfadFehler:
                return schluessel
        return anzeige(schluessel, self.ableitung_pfad)

    def _pipeline(self, pipeline: object, vorschlag: dict | None, satz: dict, gewaehlt: set[str]) -> tuple | None:
        namen = set()
        for e in satz["eintraege"]:
            art, op = aenderungssatz.operation(e)
            if e["id"] in gewaehlt and art == "skill_anlegen":
                namen.add(op["name"])
        gemerkt = vorschlag.get("pipeline") if vorschlag is not None else None
        if gemerkt is not None:
            return (gemerkt["skill"], gemerkt["logs"]) if gemerkt.get("skill") in namen else None
        if pipeline is None:
            return None
        if not isinstance(pipeline, dict) or pipeline.get("skill") not in namen:
            raise WerkstattFehler(400, {"fehler": PIPELINE_PASST_NICHT})
        if not isinstance(pipeline.get("logs"), bool):
            raise WerkstattFehler(400, {"fehler": LOGS_WAHR_ODER_FALSCH})
        return pipeline["skill"], pipeline["logs"]

    def _speichere_logs(self, skill: str, logs: bool, im_bauplan: set[str]) -> str | None:
        if self.einstellungen is None:
            return None
        try:
            self.einstellungen.setze(skill, im_bauplan, logs=logs)
        except Exception as fehler:  # noqa: BLE001
            return logs_nicht_gespeichert(getattr(fehler, "text", str(fehler)))
        return None

    @staticmethod
    def _zaehlung(atlas: dict, berechnung: _Berechnung | None = None) -> Counter:
        rueck = berechnung.rueck if berechnung is not None else None
        return Counter(befund_identitaet(b, rueck) for b in atlas.get("befunde", []))

    def _befund_fehler(self, satz: dict, gewaehlt: set[str], berechnung: _Berechnung, neue: list[dict],
                       vorher: dict, nachher: dict | None = None) -> list[dict]:
        """Neue Befunde als Fehler des Eintrags, nach dem sie auftreten (_schuldige): 'Eintrag x braucht Eintrag y',
        wenn der Scan mit dem nicht gewählten y den Befund beseitigt, sonst der Befund mit Datei, Zeile und Text. Ließ
        sich der Eintrag nicht messen, steht der Befund 'durch die Auswahl' beim letzten gewählten Eintrag."""
        nr = berechnung.nummern
        beschreibung = {e["id"]: e["beschreibung"] for e in satz["eintraege"]}
        fehler: list[dict] = []
        for schuld in self._schuldige(satz, gewaehlt, berechnung, neue, vorher, nachher):
            eid, anderer = schuld["eintrag"], schuld["braucht"]
            if eid is None:
                eid, text = berechnung.letzter, durch_die_auswahl(schuld["befund"])
            elif anderer is not None:
                text = braucht(nr[eid], nr[anderer], beschreibung[anderer])
            else:
                text = neuer_befund(nr[eid], schuld["befund"])
            if {"eintrag": eid, "text": text} not in fehler:
                fehler.append({"eintrag": eid, "text": text})
        return fehler

    def _schuldige(self, satz: dict, gewaehlt: set[str], berechnung: _Berechnung, neue: list[dict],
                   vorher: dict, nachher: dict | None = None) -> list[dict]:
        """Je neuem Befund {befund, identitaet, eintrag, braucht}, gemessen statt geraten: eintrag ist der erste
        gewählte Eintrag, nach dem der Befund auftritt (binäre Suche über die Präfixe der Auswahl in der Reihenfolge des
        Satzes, je Präfix ein Scan; die k-te Instanz einer Identität dort, wo es sie mehr als vorher + k - 1 gibt);
        befund ist die Instanz aus neue, die der im Scan dieses Präfixes dazugekommenen entspricht (_im_endergebnis),
        so passen Datei, Zeile und Text zum Diff der ganzen Auswahl. braucht ist ein nicht gewählter Eintrag, der den
        Schritt des Befunds ändert oder seine Datei umschreibt, mit dem sich die Auswahl ohne Fehler berechnen lässt
        und der den Befund beseitigt, ohne etwas dazuzubringen: seine neuen Befunde sind als Multimenge der
        Identitäten in denen von neue ohne ident enthalten. Gemessen wird er gleich zusammen mit den Helfern der
        Befunde davor (Reihenfolge von neue), so rechnet die Auswahl mit allen angebotenen Helfern ohne Fehler und
        bringt nichts dazu; passt ein Kandidat nicht, kommt der nächste dran; sonst None.

        Gescannt wird ohne die Sperre, jede Auswahl einmal, höchstens SCHULD_SCANS_HOECHSTENS Scans in SCHULD_ZEIT_S;
        eine Auswahl mit Helfer, die schon in der Berechnung scheitert, wird nicht gescannt und zählt nicht. Die Scans
        bleiben nur hier gemerkt (der Bauplan danach der ganzen Auswahl steht in _proben, nachher ist er). Fehlt ein
        Scan, weil das Budget um ist oder sich der Zwischenstand nicht einlesen lässt, ist eintrag None."""
        reihe = [e["id"] for e in satz["eintraege"] if e["id"] in gewaehlt]
        alt = self._zaehlung(vorher)
        commit = vorher["kit"].get("commit")
        gemessen: dict[frozenset, _Messung | None] = {}
        if nachher is not None:
            gemessen[frozenset(reihe)] = _messung(nachher, berechnung, neue)
        scans = SCHULD_SCANS_HOECHSTENS
        ende = time.monotonic() + SCHULD_ZEIT_S

        def messe(ids: frozenset) -> _Messung | None:
            nonlocal scans
            if ids in gemessen:
                return gemessen[ids]
            if scans <= 0 or time.monotonic() >= ende:
                return None
            with self.sperre:
                teil, teil_kit = self._berechne(satz, set(ids))
            atlas, neu = vorher, []
            if teil.fehler and not ids <= gewaehlt:
                gemessen[ids] = _messung(atlas, teil, neu)
                return gemessen[ids]
            if teil.aenderungen:
                scans -= 1
                try:
                    atlas = self._bauplan_nach(teil_kit, teil.aenderungen, commit, ende)
                    neu = self._neu(vorher, atlas, teil, self._betroffen(teil, teil_kit))
                except (WerkstattFehler, PfadFehler, pfade.KopieFehler):
                    atlas = None
            gemessen[ids] = None if atlas is None else _messung(atlas, teil, neu)
            return gemessen[ids]

        idents = [befund_identitaet(befund, berechnung.rueck) for befund in neue]
        neu_gezaehlt = Counter(idents)
        schon: Counter = Counter()
        vergeben: Counter = Counter()
        eintraege, im_praefix = [], []
        for ident in idents:
            grenze = alt[ident] + schon[ident]
            schon[ident] += 1
            eintrag = self._erster_mit(reihe, messe, ident, grenze)
            eintraege.append(eintrag)
            im_praefix.append(None if eintrag is None else self._im_praefix(reihe, messe, eintrag, ident, vergeben))
        gemeldet = _im_endergebnis(neue, idents, im_praefix)
        schuldige = []
        behalten: set[str] = set()
        for befund, ident, eintrag in zip(gemeldet, idents, eintraege):
            ohne = neu_gezaehlt - Counter({ident: neu_gezaehlt[ident]})
            anderer = None
            for kandidat in self._helfer_kandidaten(satz, gewaehlt, befund, ident) if eintrag is not None else []:
                mit = messe(frozenset(gewaehlt | behalten | {kandidat}))
                if mit is None:
                    break
                if not mit.berechnung.fehler and not mit.neue_zaehlung - ohne and mit.zaehlung[ident] <= alt[ident]:
                    anderer = kandidat
                    behalten.add(kandidat)
                    break
            schuldige.append({"befund": befund, "identitaet": ident, "eintrag": eintrag, "braucht": anderer})
        return schuldige

    @staticmethod
    def _im_praefix(reihe: list[str], messe, eintrag: str, ident: tuple, vergeben: Counter) -> dict | None:
        """Die Instanz von ident, die mit eintrag dazukommt: im Scan des Präfixes bis eintrag, ohne Gegenstück im
        Präfix davor (_ungepaart); die n-te solche für die n-te Instanz, die diesem Eintrag gehört."""
        k = reihe.index(eintrag) + 1
        jetzt, davor = messe(frozenset(reihe[:k])), messe(frozenset(reihe[:k - 1]))
        if jetzt is None or davor is None:
            return None
        gruppe = [b for b in jetzt.atlas.get("befunde", []) if befund_identitaet(b, jetzt.berechnung.rueck) == ident]
        frueher = [b for b in davor.atlas.get("befunde", []) if befund_identitaet(b, davor.berechnung.rueck) == ident]
        neu = _ungepaart(gruppe, frueher)
        n = vergeben[(ident, eintrag)]
        vergeben[(ident, eintrag)] += 1
        return neu[n] if n < len(neu) else None

    @staticmethod
    def _erster_mit(reihe: list[str], messe, ident: tuple, grenze: int) -> str | None:
        """Der erste Eintrag der Reihe, nach dem es ident öfter als grenze gibt (die ganze Reihe hat es so oft); None,
        wenn ein Präfix sich nicht messen ließ."""
        unten, oben = 1, len(reihe)
        while unten < oben:
            mitte = (unten + oben) // 2
            gemessen = messe(frozenset(reihe[:mitte]))
            if gemessen is None:
                return None
            if gemessen[0][ident] > grenze:
                oben = mitte
            else:
                unten = mitte + 1
        return reihe[unten - 1]

    def _helfer_kandidaten(self, satz: dict, gewaehlt: set[str], befund: dict, ident: tuple) -> list[str]:
        """Nicht gewählte Einträge, die den Befund beseitigen könnten: die seinen Schritt ändern, entfernen oder seine
        Schleife setzen, und die den Text seiner Datei (unter dem Namen danach oder vorher) umschreiben."""
        kandidaten = []
        if isinstance(befund.get("skill"), str) and isinstance(befund.get("schritt"), str):
            kandidaten += [e["id"] for e in self._anbietende(satz, gewaehlt, ("beleg", befund["skill"], befund["schritt"]))]
        for e in satz["eintraege"]:
            art, op = aenderungssatz.operation(e)
            if e["id"] not in gewaehlt and art == "text_ersetzen" and op["datei"] in (befund.get("datei"), ident[1]):
                kandidaten.append(e["id"])
        return list(dict.fromkeys(kandidaten))

    # ------------------------------------------------------------ Anwenden

    def _nur_schreibbar(self) -> None:
        if not self.schreibbar:
            raise WerkstattFehler(403, {"fehler": NUR_LESEN})

    def _nicht_schreibbar(self) -> WerkstattFehler:
        return WerkstattFehler(409, {"fehler": texte.werkstatt_nicht_schreibbar(finden.tilde(self.ergebnis, self.home))})

    def _betroffen(self, berechnung: _Berechnung, kit: VirtuellesKit) -> set[str]:
        """Geänderte Dateien; hat sich die Ableitung geändert, auch die SKILL.md jeder Skill, die sie nennt (nach dem
        Namen, wie der Scanner, und nach datei, wenn das ein Text ist)."""
        dateien = {s for s in berechnung.aenderungen if s != ABLEITUNG}
        dateien |= leitplanken_modul.hook_skripte_betroffen(kit, berechnung.aenderungen)
        if ABLEITUNG in berechnung.aenderungen and isinstance(kit.ableitung, dict):
            skills = kit.skills()
            for name, e in kit.ableitung.get("skills", {}).items():
                dateien |= {skills[name]} if name in skills else set()
                dateien |= {e["datei"]} if isinstance(e, dict) and isinstance(e.get("datei"), str) else set()
        return dateien

    def _neue_befunde(self, berechnung: _Berechnung, kit: VirtuellesKit, vorher: dict | None = None,
                      signatur: tuple | None = None) -> tuple[list[dict], dict]:
        """WS-W10: Befunde in einer betroffenen Datei, die es nachher öfter gibt als vorher (_neu), und der Bauplan
        danach. vorher: der Bauplan vor den Änderungen (sonst bauplan()); signatur: die Signatur, unter der die
        Berechnung entstand (sonst jetzt)."""
        vorher = vorher if vorher is not None else self.bauplan()
        nachher = self._nachher_409(berechnung, kit, vorher, signatur)
        return self._neu(vorher, nachher, berechnung, self._betroffen(berechnung, kit)), nachher

    def _nachher_409(self, berechnung: _Berechnung, kit: VirtuellesKit, vorher: dict,
                     signatur: tuple | None = None) -> dict:
        try:
            return self._nachher(berechnung, kit, vorher, signatur)
        except WerkstattFehler as fehler:
            raise self._als_409(fehler, nachher=True)

    def _nachher(self, berechnung: _Berechnung, kit: VirtuellesKit, vorher: dict, signatur: tuple | None = None,
                 ende: float | None = None) -> dict:
        """Der Bauplan nach den Änderungen der Berechnung, gemerkt je (Änderungen, Signatur, Commit): gemerkt wird nur,
        wenn die Signatur nach dem Einlesen noch die ist, unter der die Berechnung entstand."""
        signatur = signatur if signatur is not None else self.signatur()
        commit = vorher["kit"].get("commit")
        schluessel = (aenderungen_schluessel(berechnung.aenderungen), signatur, commit)
        atlas = self._probe_gemerkt(schluessel)
        if atlas is None:
            atlas = self._bauplan_nach(kit, berechnung.aenderungen, commit, ende)
            if self.signatur() == signatur:
                self._probe_merken(schluessel, atlas)
        return atlas

    def _neu(self, vorher: dict, nachher: dict, berechnung: _Berechnung, betroffen: set[str]) -> list[dict]:
        """Neu ist, was es je Identität (befund_identitaet) in einer betroffenen Datei nachher öfter gibt als vorher; ein
        Befund, den es vorher schon gab, ist nie neu. Gibt es eine Identität mehrfach, zählen zuerst die als neu, die
        keinem Befund vorher gleichen."""
        alt = Counter(befund_identitaet(b) for b in vorher.get("befunde", []) if b.get("datei") in betroffen)
        gruppen: dict[tuple, list[dict]] = {}
        for befund in nachher.get("befunde", []):
            ident = befund_identitaet(befund, berechnung.rueck)
            if ident[1] in betroffen:
                gruppen.setdefault(ident, []).append(befund)
        gewaehlt: set[int] = set()
        for ident, liste in gruppen.items():
            zuviel = len(liste) - alt[ident]
            if zuviel > 0:
                reihe = sorted(liste, key=lambda b: b in vorher.get("befunde", []))
                gewaehlt |= {id(b) for b in reihe[:zuviel]}
        return [b for b in nachher.get("befunde", []) if id(b) in gewaehlt]

    def _bauplan_vorher(self) -> dict:
        try:
            return self.bauplan()
        except WerkstattFehler as fehler:
            raise self._als_409(fehler, nachher=False)

    @staticmethod
    def _als_409(fehler: WerkstattFehler, nachher: bool) -> WerkstattFehler:
        """Vorschau und Übernehmen antworten nie 500: Scheitert das Einlesen, 409 mit dem Grund des Scanners."""
        if fehler.status != 500:
            return fehler
        grund = str(fehler.daten.get("fehler", "")).removeprefix(f"{SCAN_FEHLER}: ")
        return WerkstattFehler(409, {"fehler": nicht_einlesbar(grund, nachher)})

    def _probe_gemerkt(self, schluessel: tuple | None) -> dict | None:
        if schluessel is None:
            return None
        with self._proben_sperre:
            gemerkt = self._proben.get(schluessel)
            if gemerkt is not None:
                self._proben.move_to_end(schluessel)
        return json.loads(gemerkt) if gemerkt is not None else None

    def _probe_merken(self, schluessel: tuple | None, atlas: dict) -> None:
        if schluessel is None:
            return
        with self._proben_sperre:
            self._proben[schluessel] = json.dumps(atlas, ensure_ascii=False)
            while len(self._proben) > PROBEN_GEMERKT:
                self._proben.popitem(last=False)

    def probelauf(self, satz: dict, laufzeit: float | None = None
                  ) -> tuple[list[dict], list[dict], dict | None, dict | None]:
        """Für eingereichte Sätze der KI (WS-K7): alle Einträge gewählt; (Fehler je Eintrag, neue Befunde, Bauplan
        danach, Bauplan vorher). Ohne Änderung sind beide Baupläne None. Schreibt nichts.

        Die Sperre gilt nur für die Berechnung im Speicher und die Signatur; Bauplan vorher, Probe-Kopie und Einlesen
        laufen ohne sie, damit die Seite während der Prüfung eines Satzes oder Gutachtens der KI nicht wartet. Der
        Bauplan danach bleibt gemerkt wie bei der Vorschau (dieselben Änderungen lesen das Kit nicht noch einmal ein).
        Ändert sich das Kit währenddessen, prüft Anwenden ohnehin alles noch einmal unter der Sperre. Mit laufzeit endet
        alles spätestens danach (Warten auf die Sperre und Einlesen), sonst 503 mit zeit_um()."""
        self._pruefe_form(satz)
        ende = None if laufzeit is None else time.monotonic() + laufzeit
        if not self.sperre.acquire(timeout=-1 if ende is None else _rest(ende)):
            raise WerkstattFehler(503, {"fehler": zeit_um()})
        try:
            berechnung, kit = self._berechne(satz, {e["id"] for e in satz["eintraege"]})
            if berechnung.fehler or not berechnung.aenderungen:
                return berechnung.fehler, [], None, None
            betroffen = self._betroffen(berechnung, kit)
            signatur = self.signatur()
        finally:
            self.sperre.release()
        bauplan = self.bauplan(_rest(ende))
        nachher = self._nachher(berechnung, kit, bauplan, signatur, ende)
        ohne_bewacht = leitplanken_modul.ohne_bewacht(satz, {e["id"] for e in satz["eintraege"]}, nachher)
        return ohne_bewacht, self._neu(bauplan, nachher, berechnung, betroffen), nachher, bauplan

    def anwenden(self, satz: object = None, auswahl: object = None, stand: object = None, bestaetigt: bool = False,
                 satz_id: object = None, pipeline: object = None) -> dict:
        vorschlag = self._vorschlag(satz_id) if satz_id is not None else None
        herkunft = vorschlag["herkunft"] if vorschlag is not None else HAND
        satz, gewaehlt = self._satz_und_auswahl(vorschlag["satz"] if vorschlag is not None else satz, auswahl, None,
                                                bool(vorschlag is not None and vorschlag.get("gedaechtnis")),
                                                bool(vorschlag is not None and vorschlag.get("kontext")))
        self._nur_schreibbar()
        if kontext_modul.ist_kontext_satz(satz):
            return self._anwenden_kontext(satz, gewaehlt, stand, bestaetigt, satz_id, herkunft)
        if bereinigen.ist_gedaechtnis_satz(satz):
            return self._anwenden_gedaechtnis(satz, gewaehlt, stand, bestaetigt, satz_id, herkunft)
        logs_fuer = self._pipeline(pipeline, vorschlag, satz, gewaehlt)
        with self.sperre:
            berechnung, kit = self._berechne(satz, gewaehlt)
            if berechnung.fehler:
                raise WerkstattFehler(409, {"fehler": AUSWAHL_FEHLER, "eintraege": berechnung.fehler})
            if stand != self._stand(berechnung, satz, gewaehlt):
                raise WerkstattFehler(409, {"fehler": STAND_ALT})
            warnungen = self._warnungen(berechnung)
            if warnungen and not bestaetigt:
                nur_nennung = all(w.get("art") == "nennung" for w in warnungen)
                raise WerkstattFehler(409, {"fehler": NOCH_GENANNT if nur_nennung else BESTAETIGEN, "warnungen": warnungen})
            if not berechnung.aenderungen:
                raise WerkstattFehler(409, {"fehler": NICHTS_GEWAEHLT})
            try:
                neue, nachher = self._neue_befunde(berechnung, kit, self._bauplan_vorher(), self.signatur())
            except (PfadFehler, pfade.KopieFehler) as fehler:
                raise WerkstattFehler(409, {"fehler": str(fehler)})
            if neue:
                raise WerkstattFehler(422, {"fehler": ZURUECKGENOMMEN, "befunde": neue})
            ohne_bewacht = leitplanken_modul.ohne_bewacht(satz, gewaehlt, nachher)
            if ohne_bewacht:
                raise WerkstattFehler(409, {"fehler": AUSWAHL_FEHLER, "eintraege": ohne_bewacht})
            eintraege = [e for e in satz["eintraege"] if e["id"] in gewaehlt]
            try:
                sicherung = self.ablage.sichere(satz["titel"], herkunft, eintraege, berechnung.aenderungen)
            except (OSError, PfadFehler, ergebnis_modul.NichtSchreibbar):
                raise self._nicht_schreibbar()
            try:
                verlauf = self._verlauf_mit(satz, herkunft, sicherung, berechnung)
            except (OSError, ablage_modul.AblageFehler):
                self.ablage.entferne_sicherung(sicherung)
                raise self._nicht_schreibbar()
            self._schreibe_mit_verlauf(sicherung, {s: neu for s, (_, neu) in berechnung.aenderungen.items()}, verlauf)
            self.geschrieben |= set(berechnung.aenderungen)
            if satz_id is not None:
                self.vorschlaege.entferne(satz_id, {"art": "uebernommen", "gewaehlt": len(eintraege),
                                                    "von": len(satz["eintraege"])})
            self._bauplan = None
            antwort = {"ok": True, "titel": satz["titel"],
                       "dateien": [anzeige(s, self.ableitung_pfad) for s in sorted(berechnung.aenderungen)],
                       "leitplanken": self._leitplanken_betroffen(berechnung.aenderungen)}
            if logs_fuer is not None:
                warnung = self._speichere_logs(*logs_fuer, set(kit.skills()))
                if warnung is not None:
                    antwort["warnung"] = warnung
            return antwort

    def _schreibe_mit_verlauf(self, sicherung: Path, inhalte: dict[str, bytes | None], verlauf: dict,
                              modi: dict[str, int | None] | None = None, wiederherstellen: bool = False,
                              erwartet: dict[str, bytes | None] | None = None) -> None:
        """Gemeinsamer Ablauf von Anwenden und Rückgängig: Der Verlauf steht schon fertig bereit, die Sicherung hält
        den Stand davor. Erst die Dateien, dann der Verlauf; scheitert ein Schritt, nimmt _nimm_zurueck alles zurück,
        Dateien und Verlauf laufen nie auseinander."""
        try:
            self._schreibe(inhalte, modi, wiederherstellen, erwartet)
        except (OSError, PfadFehler, ergebnis_modul.NichtSchreibbar) as fehler:
            self._nimm_zurueck(sicherung, sorted(inhalte), str(fehler) if isinstance(fehler, PfadFehler)
                               else texte.werkstatt_nicht_schreibbar(finden.tilde(self.kit, self.home)),
                               isinstance(fehler, ablage_modul.GedaechtnisSchreibFehler),
                               self._gedaechtnis_geschrieben(inhalte))
        try:
            self.ablage.speichere_verlauf(verlauf)
        except (OSError, PfadFehler, ergebnis_modul.NichtSchreibbar):
            self._nimm_zurueck(sicherung, sorted(inhalte),
                               texte.werkstatt_nicht_schreibbar(finden.tilde(self.ergebnis, self.home)),
                               geschrieben={s: inhalte[s] for s in inhalte if ablage_modul.ist_extern(s)})

    def _schreibe(self, inhalte: dict[str, bytes | None], modi: dict[str, int | None] | None,
                  wiederherstellen: bool, erwartet: dict[str, bytes | None] | None = None) -> None:
        if any(ablage_modul.ist_extern(s) for s in inhalte):
            self.ablage.schreibe(inhalte, modi, wiederherstellen, erwartet)
        else:
            self.ablage.schreibe(inhalte)

    def _gedaechtnis_geschrieben(self, inhalte: dict[str, bytes | None]) -> dict[str, bytes | None]:
        geschrieben = getattr(self.ablage, "gedaechtnis_geschrieben", [])
        return {s: inhalte[s] for s in geschrieben if s in inhalte}

    def _nimm_zurueck(self, sicherung: Path, schluessel: list[str], grund: str, allein: bool = False,
                      geschrieben: dict[str, bytes | None] | None = None) -> None:
        """Stellt jede Datei einzeln aus der Sicherung her und prüft danach, was wirklich zurück ist. Ist alles zurück,
        fällt die Sicherung weg; sonst bleibt sie, und die Meldung nennt Dateien und Ort. Wirft immer 409."""
        try:
            _, info = self.ablage.info(sicherung.name)
        except (OSError, ablage_modul.AblageFehler):
            info = None
        offen = []
        geschrieben = geschrieben or {}
        for s in schluessel:
            if ablage_modul.ist_extern(s) and s not in geschrieben:
                continue
            try:
                if info is not None:
                    self._schreibe({s: self.ablage.vorher_aus(sicherung, info, s)},
                                   {s: info["dateien"][s].get("modus")}, True,
                                   {s: geschrieben[s]} if ablage_modul.ist_extern(s) else None)
            except (OSError, PfadFehler, ablage_modul.AblageFehler, ergebnis_modul.NichtSchreibbar):
                pass
            if info is None or pfade.sha(self.ablage.lies(s)) != info["dateien"][s]["vorher"]:
                offen.append(self._anzeige(s))
        if offen:
            raise WerkstattFehler(409, {"fehler": f"{grund} {nicht_alles_zurueck(offen, finden.tilde(sicherung, self.home))}"})
        self.ablage.entferne_sicherung(sicherung)
        raise WerkstattFehler(409, {"fehler": grund if allein else f"{grund} {NICHTS_GEAENDERT}"})

    def _verlauf_mit(self, satz: dict, herkunft: str, sicherung: Path, berechnung,
                     gedaechtnis: list[str] | None = None) -> dict:
        """Der Verlauf mit dem neuen Satz, noch nicht gespeichert."""
        verlauf = self.ablage.verlauf()
        dateien = sorted(berechnung.aenderungen)
        _, info = self.ablage.info(sicherung.name)
        eintrag = {"id": sicherung.name, "titel": satz["titel"], "herkunft": herkunft, "zeit": info["zeit"],
                   "sicherung": sicherung.name, "dateien": dateien, "rueckgaengig": False}
        if gedaechtnis is not None:
            verlauf["saetze"].append({**eintrag, "sitzung": self.sitzung, "gedaechtnis": gedaechtnis})
            return verlauf
        verlauf["saetze"].append(eintrag)
        verlauf["ungespeichert"].append({"id": sicherung.name, "titel": satz["titel"], "dateien": dateien,
                                         "sitzung": self.sitzung})
        for schluessel, (_, neu) in berechnung.aenderungen.items():
            verlauf["eigene"][schluessel] = pfade.sha(neu)
        return verlauf

    def _leitplanken_betroffen(self, schluessel) -> bool:
        """WS-L12: Hat der Satz die Hook-Datei oder ein Hook-Skript geändert?"""
        return any(s == self.ordner.hooks or pfade.ist_hook_skript(s, self.ordner.hook_ordner) for s in schluessel)

    # ------------------------------------------------------------ Rückgängig

    def rueckgaengig(self, bestaetigt: bool = False, satz_id: object = None) -> dict:
        self._nur_schreibbar()
        with self.sperre:
            verlauf = self.ablage.verlauf()
            offen = [s for s in verlauf["saetze"] if not s.get("rueckgaengig")]
            if not offen:
                raise WerkstattFehler(409, {"fehler": ablage_modul.UNBEGLAUBIGT if self.ablage.unbeglaubigt
                                            else NICHTS_RUECKGAENGIG})
            eintrag = offen[-1]
            if satz_id is not None and satz_id != eintrag["id"]:
                raise WerkstattFehler(409, {"fehler": ANDERER_SATZ})
            try:
                sicherung, info = self.ablage.info(eintrag["sicherung"])
            except ablage_modul.AblageFehler as fehler:
                raise WerkstattFehler(409, {"fehler": f"„{eintrag['titel']}“ lässt sich nicht rückgängig machen: {fehler}"})
            gedaechtnis = [s for s in info["dateien"] if ablage_modul.ist_extern(s)]
            for s in gedaechtnis:
                try:
                    if ks.ist_schluessel(s):
                        ks.pruefe(self.ablage.kontext_ziel(s), self.verboten, wiederherstellen=True)
                    else:
                        gd.pruefe(*self.ablage.ziel(s), self.verboten, wiederherstellen=True)
                except (gd.GedaechtnisFehler, ks.KontextFehler) as fehler:
                    raise WerkstattFehler(fehler.status, {"fehler": fehler.text})
                except PfadFehler as fehler:
                    raise WerkstattFehler(409, {"fehler": str(fehler)})
            geaendert = [s for s, d in sorted(info["dateien"].items())
                         if pfade.sha(self.ablage.lies(s)) != d["nachher"]]
            if geaendert and not bestaetigt:
                angezeigt = [self._anzeige(s) for s in geaendert]
                raise WerkstattFehler(409, {"fehler": f"Seit „{eintrag['titel']}“ wurde {', '.join(angezeigt)} verändert. "
                                                      f"Bestätige, wenn du trotzdem zurückgehen willst.",
                                            "dateien": angezeigt, "id": eintrag["id"]})
            try:
                inhalte = {s: self.ablage.vorher_aus(sicherung, info, s) for s in info["dateien"]}
            except ablage_modul.AblageFehler as fehler:
                raise WerkstattFehler(409, {"fehler": f"„{eintrag['titel']}“ lässt sich nicht rückgängig machen: {fehler}"})
            eintrag["rueckgaengig"] = True
            ungespeichert = verlauf["ungespeichert"]
            kit_dateien = sorted(s for s in info["dateien"] if s not in gedaechtnis)
            kontext_kit = sorted(self._kontext_kit([eintrag]))
            if any(u.get("id") == eintrag["id"] and u.get("sitzung") == self.sitzung for u in ungespeichert):
                verlauf["ungespeichert"] = [u for u in ungespeichert if u.get("id") != eintrag["id"]]
            elif kit_dateien or kontext_kit:
                ungespeichert.append({"id": f"{eintrag['id']}-zurueck", "titel": f"Rückgängig: {eintrag['titel']}",
                                      "dateien": kit_dateien, "sitzung": self.sitzung,
                                      **({"kontext_kit": kontext_kit} if kontext_kit else {})})
            for schluessel in kit_dateien:
                verlauf["eigene"][schluessel] = info["dateien"][schluessel]["vorher"]
            # Wie beim Anwenden: eine eigene Sicherung des Stands jetzt, damit ein Fehler mittendrin zurückgeht.
            jetzt = {s: (self.ablage.lies(s), inhalt) for s, inhalt in inhalte.items()}
            try:
                vorher = self.ablage.sichere(f"Vor Rückgängig: {eintrag['titel']}", HAND, [], jetzt)
            except (OSError, PfadFehler, ergebnis_modul.NichtSchreibbar):
                raise self._nicht_schreibbar()
            self._schreibe_mit_verlauf(vorher, inhalte, verlauf, {s: d.get("modus") for s, d in info["dateien"].items()},
                                       True, {s: alt for s, (alt, _) in jetzt.items() if ablage_modul.ist_extern(s)})
            self.ablage.entferne_sicherung(vorher)
            self.geschrieben |= set(kit_dateien) | set(kontext_kit)
            self._bauplan = None
            return {"ok": True, "titel": eintrag["titel"], "dateien": [self._anzeige(s) for s in kit_dateien],
                    "leitplanken": self._leitplanken_betroffen(kit_dateien)}

    # ------------------------------------------------------------ Speichern

    def _zu_speichern(self, schluessel: list[str], wurzel: Path,
                      kontext_kit: set[str] | None = None) -> list[tuple[str, Path]]:
        """Nur, was diese Sitzung selbst geschrieben hat, im Kit liegt, nicht ignoriert und geändert ist (git je
        Prüfung einmal für alle Pfade)."""
        kandidaten: list[tuple[str, Path]] = []
        kontext_kit = kontext_kit or set()
        for s in schluessel:
            if s not in self.geschrieben:
                continue
            try:
                pfad = self._pfad_von(s, kontext_kit)
            except PfadFehler:
                continue
            if pfade.liegt_unter(pfad.parent, self.kit) and pfade.liegt_unter(pfad.parent, wurzel):
                kandidaten.append((s, pfad))
        alle = [p for _, p in kandidaten]
        ignoriert = ablage_modul.ignoriert(wurzel, alle)
        geaendert = ablage_modul.ungespeichert_im_git(wurzel, [p for p in alle if p not in ignoriert])
        return [(s, p) for s, p in kandidaten if p in geaendert
                and (s != ABLEITUNG or ablage_modul.versioniert(wurzel, p))]

    def speichern(self, nachricht: object = None) -> dict:
        self._nur_schreibbar()
        eigene = None if nachricht is None else pruefe_nachricht(nachricht)
        with self.sperre:
            wurzel = self.git_wurzel()
            if wurzel is None:
                raise WerkstattFehler(409, {"fehler": KEIN_GIT})
            verlauf = self.ablage.verlauf()
            saetze = self._ungespeichert(verlauf)
            kontext_kit = self._kontext_kit(saetze)
            schluessel = sorted({s for u in saetze for s in u.get("dateien", [])} | kontext_kit)
            try:
                pfade_ = [p for _, p in self._zu_speichern(schluessel, wurzel, kontext_kit)]
            except ablage_modul.GitFehler as fehler:
                raise WerkstattFehler(409, {"fehler": git_antwortet_nicht(str(fehler))})
            if not pfade_:
                verlauf["ungespeichert"] = [u for u in verlauf["ungespeichert"] if u not in saetze]
                try:
                    self.ablage.speichere_verlauf(verlauf)
                except (OSError, PfadFehler, ergebnis_modul.NichtSchreibbar):
                    pass  # nur aufgeräumt wird hier; beim nächsten Speichern noch einmal
                raise WerkstattFehler(409, {"fehler": NICHTS_ZU_SPEICHERN})
            titel = [u.get("titel", "") for u in saetze]
            nachricht = eigene or (f"Werkstatt: {titel[0]}" if len(titel) == 1
                                   else f"Werkstatt: {len(titel)} Änderungen\n\n" + "\n".join(f"- {t}" for t in titel))
            try:
                commit = ablage_modul.committe(wurzel, pfade_, nachricht)
            except ablage_modul.AblageFehler as fehler:
                raise WerkstattFehler(409, {"fehler": f"git commit hat nicht geklappt: {fehler}"})
            verlauf["ungespeichert"] = [u for u in verlauf["ungespeichert"] if u not in saetze]
            verlauf["eigene"] = {}
            self.geschrieben.clear()
            self._bauplan = None  # der Fuß nennt den neuen Commit
            antwort = {"ok": True, "commit": commit, "nachricht": nachricht}
            # Der Commit ist gemacht und lässt sich nicht zurücknehmen: ein Fehler beim Verlauf ist nur eine Warnung.
            try:
                self.ablage.speichere_verlauf(verlauf)
            except (OSError, PfadFehler, ergebnis_modul.NichtSchreibbar):
                antwort["warnung"] = verlauf_nicht_gespeichert(
                    texte.werkstatt_nicht_schreibbar(finden.tilde(self.ergebnis, self.home)))
            return antwort
