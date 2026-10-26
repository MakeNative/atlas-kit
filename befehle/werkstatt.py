"""bin/atlas-kit werkstatt [ordner | kit-name | stopp | oeffnen] (specs/werkstatt.yaml WS-B1 bis WS-B5, WS-K9).

Die Unterbefehle der Werkstatt-Sitzung (warte, fortschritt, frage, ergebnis, abbruch) stehen in werkstatt_sitzung.py.
Der Start endet mit der Steuerzeile '@atlas werkstatt: <kennung>', mit der die Skill danach warte aufruft.

Startet den Server abgelöst vom Aufruf mit dem Python der eigenen Umgebung, verwendet einen laufenden
Server für dasselbe Kit wieder, öffnet mit 'oeffnen' den Browser erneut und beendet mit 'stopp [kit]' die
Werkstatt eines Kits, mit 'stopp --alle' alle Werkstätten des Datenordners. Den Seiten-Schlüssel sieht dieser Befehl nie: Den Browser öffnet der Server
selbst mit einem Einmal-Link; im Chat steht keine Adresse. 'stopp', 'oeffnen' und die Befehle der Sitzung sind keine
Kit-Namen.
Nur Standardbibliothek; kein Shell-Aufruf.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import browser
import einrichtung
import ergebnis
import finden
import kit_anlegen
import plattform
import texte
import werkstatt_sitzung
import zuletzt

ORDNER = "werkstatt"
SERVER = "werkstatt_server.py"
WARTEN_S = 20.0
BROWSER_WARTEN_S = browser.WARTEN_HOECHSTENS_S + 15
GEHEIMNIS = "geheimnis"  # einziger Ort des Beglaubigungs-Schlüssels: <datenordner>/werkstatt/geheimnis
STOPP = "stopp"
STOPP_WARTEN_S = 5.0
OEFFNEN = "oeffnen"
ALLE = "--alle"
KNOPF = "knopf"
ZIEL_NEUE_PIPELINE = "neue-pipeline"  # WS-NK5: die Seite öffnet gleich den Dialog „Neue Pipeline“


def _datenordner(aufruf) -> Path:
    return einrichtung.datenordner(aufruf.paket, aufruf.daten) / ORDNER


def _ordner(aufruf) -> Path:
    """Der Zustandsordner, nur für den eigenen Nutzer (0700). Unter Windows setzt chmod nur das Schreibschutz-Bit;
    dort schützen die vererbten Rechte des Benutzerprofils, in dem der Datenordner liegt (docs/windows.md)."""
    ordner = _datenordner(aufruf)
    if not ordner.exists():
        ordner.mkdir(parents=True, mode=0o700)
    os.chmod(ordner, 0o700)
    return ordner


def _zustand_datei(ordner: Path, kit: Path) -> Path:
    """Ein Server je Ordner: Kennung aus st_dev/st_ino, nicht aus der Schreibweise des Pfads."""
    info = os.stat(kit)
    return ordner / f"{info.st_dev:x}-{info.st_ino:x}.json"


def _lies_zustand(datei: Path) -> dict | None:
    return werkstatt_sitzung.lies_zustand(datei, mit_pid=True)


def _anfrage(zustand: dict, methode: str, pfad: str, daten: object = None, warten: float = 30,
             recht: str = "schluessel") -> dict | None:
    """JSON einer erfolgreichen Antwort oder None (antwortet nicht, Fehlerstatus)."""
    antwort = werkstatt_sitzung.anfrage(zustand, methode, pfad, daten, warten=warten, recht=recht)
    return antwort[1] if antwort is not None and 200 <= antwort[0] < 300 else None


def _status(zustand: dict) -> dict | None:
    """Status für die eigenen Abfragen dieses Befehls: mit dem Öffnen-Recht, damit der Server sie nicht als Kontakt
    der Claude-Code-Sitzung zählt (WS-K10)."""
    return _anfrage(zustand, "GET", "/api/status", recht="oeffnen" if isinstance(zustand.get("oeffnen"), str)
                    else "schluessel")


ZUSTAND_IN_BEFEHL = re.compile(r' --zustand ("?)(.+?)\1 --geheimnis ')


def _ist_eigener_server(zustand: dict, datei: Path) -> bool | None:
    """Führt der Prozess werkstatt_server.py mit genau dieser Zustand-Datei aus? True bei gleichem Pfad (nach normcase)
    oder, bei anderer Schreibweise, derselben Datei (samefile). False, wenn es die pid nicht gibt, ihre Befehlszeile
    werkstatt_server.py nicht enthält oder eine Zustand-Datei mit anderem Namen nennt (<dev>-<ino>.json: Server eines
    anderen Kits). None: unbekannt (ps bzw. PowerShell ließ sich nicht starten, lief zu lange, meldete einen Fehler)
    oder nicht eindeutig (nicht sicher lesbar, oder gleicher Name in anderer Schreibweise, ohne dass sich prüfen lässt,
    ob es dieselbe Datei ist); das heißt nie „weg“ (WS-PW2)."""
    befehl = plattform.befehlszeile(zustand["pid"])
    if befehl is None:
        return None
    if SERVER not in befehl:
        return False
    # _starte_server ruft immer '… --zustand <datei> --geheimnis …'; unter Windows steht ein Pfad mit Leerzeichen in "".
    treffer = ZUSTAND_IN_BEFEHL.search(befehl)
    if treffer is None or "\ufffd" in treffer.group(2):
        return None
    genannt = treffer.group(2)
    if os.path.normcase(genannt) == os.path.normcase(str(datei)):
        return True
    if os.path.normcase(re.split(r"[\\/]", genannt)[-1]) != os.path.normcase(datei.name):
        return False
    try:  # gleicher Name, andere Schreibweise (/./, 8.3-Kurzname, Symlink oder Junction im Weg)
        return True if os.path.samefile(genannt, datei) else None
    except (OSError, ValueError):
        return None


def _entferne(datei: Path) -> None:
    try:
        datei.unlink()
    except OSError:
        pass


class AntwortetNicht(Exception):
    """Der eigene Server dieses Kits lebt, antwortet aber nicht (beschäftigt oder angehalten)."""


def _weg(zustand: dict, datei: Path) -> bool:
    """Ist der Prozess der Zustand-Datei nachweislich weg? Auch, wenn die pid inzwischen ein anderer Prozess hat."""
    if not werkstatt_sitzung.lebt(zustand["pid"]):
        return True
    return _ist_eigener_server(zustand, datei) is False


def _laufender(datei: Path, kit: Path) -> dict | None:
    """Der laufende Server dieses Kits oder None. Die Zustand-Datei fällt nur weg, wenn ihr Prozess nachweislich weg
    ist; lebt er, antwortet aber nicht, wirft es AntwortetNicht, statt einen zweiten Server neben ihm zu starten."""
    zustand = _lies_zustand(datei)
    if zustand is None:
        return None
    if _weg(zustand, datei):
        _entferne(datei)
        return None
    status = _status(zustand)
    try:
        gleich = os.path.samefile(zustand.get("kit", ""), kit)
    except OSError:
        gleich = False
    if status is None or not gleich:
        raise AntwortetNicht()
    return {**zustand, "status": status}


class StartFehler(Exception):
    pass


def _starte_server(aufruf, sage, kit, ergebnisordner: Path, ordner: Path, datei: Path) -> dict:
    python = einrichtung.python_der_umgebung(aufruf.paket, aufruf.daten, aufruf.home, sage)
    protokoll = datei.with_suffix(".log")
    befehl = [str(python), "-X", "utf8", "-B", "-E", "-s", str(aufruf.paket / "befehle" / SERVER),
              "--kit", str(kit.ordner), "--ergebnis", str(ergebnisordner), "--zustand", str(datei),
              "--geheimnis", str(ordner / GEHEIMNIS), "--startordner", str(aufruf.arbeitsordner)]
    with open(protokoll, "ab") as log:
        prozess = plattform.starte_abgeloest(befehl, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                             cwd=str(ordner), close_fds=True)
    ende = time.monotonic() + WARTEN_S
    while time.monotonic() < ende:
        zustand = _lies_zustand(datei)
        if zustand is not None and plattform.vom_start(zustand, prozess.pid):
            status = _status(zustand)
            if status is not None:
                return {**zustand, "status": status}
        if prozess.poll() is not None:
            break
        time.sleep(0.1)
    if prozess.poll() is None:
        prozess.terminate()  # Windows: das Kind des venv-Starters endet mit ihm (im CI gemessen, r23-10)
    zeilen = [z for z in protokoll.read_text(encoding="utf-8", errors="replace").splitlines() if z.strip()]
    raise StartFehler(zeilen[-1] if zeilen else "keine Rückmeldung")


def _oeffne(aufruf, zustand: dict, ziel: str | None = None) -> str:
    """Der Server öffnet den Browser (Einmal-Link) mit dem Öffnen-Recht; die Antwort kommt erst danach. ziel (WS-NK5)
    geht als Teil des Links mit, z. B. ZIEL_NEUE_PIPELINE."""
    anfrage = {"startordner": str(aufruf.arbeitsordner), **({"ziel": ziel} if ziel else {})}
    ergebnis_browser = _anfrage(zustand, "POST", "/api/oeffnen", anfrage, warten=BROWSER_WARTEN_S, recht="oeffnen")
    if isinstance(ergebnis_browser, dict) and ergebnis_browser.get("geoeffnet"):
        return texte.BROWSER_OFFEN
    datei = ergebnis_browser.get("datei") if isinstance(ergebnis_browser, dict) else None
    versucht = isinstance(ergebnis_browser, dict) and ergebnis_browser.get("versucht") is True
    if isinstance(datei, str):
        if versucht:
            return texte.werkstatt_link_unsicher(aufruf.tilde(Path(datei)))
        return texte.werkstatt_link_in_datei(aufruf.tilde(Path(datei)))
    return texte.WERKSTATT_BROWSER_UNSICHER if versucht else texte.WERKSTATT_BROWSER_NICHT


def _kit_zum_start(aufruf) -> finden.Kit:
    """Das gewählte Kit; ohne Kit im Arbeitsordner die Liste der zuletzt geöffneten und /atlas:neu (WS-NK8,
    WS-NK10)."""
    if aufruf.argument:
        return aufruf.gewaehltes_kit()
    try:
        return aufruf.kit_im_arbeitsordner()
    except finden.KeinKit as fehler:
        if fehler.zeilen != [texte.kein_kit(aufruf.tilde(aufruf.arbeitsordner), aufruf.befehl_im_chat)]:
            raise
        eigene = aufruf.zuletzt_geoeffnet()
        zeilen = [texte.zuletzt_kopf(aufruf.tilde(aufruf.arbeitsordner)),
                  *(texte.zuletzt_eintrag(k.kit_id, aufruf.tilde(k.ordner)) for k in eigene)] if eigene else fehler.zeilen
        raise finden.KeinKit([*zeilen, texte.NEU_NENNEN])


class KitNichtBeschreibbar(Exception):
    def __init__(self, ordner: Path):
        super().__init__(str(ordner))
        self.ordner = ordner


def _ergebnisordner(aufruf, kit: finden.Kit) -> Path:
    if finden.ist_installierte_kopie(kit.ordner, aufruf.home):
        ordner = aufruf.ergebnisordner_fuer(kit)
        aufruf.lege_ergebnisordner_an(ordner)
        return ordner
    ordner = Path(os.path.realpath(kit.ordner)) / ergebnis.ORDNER_NAME
    try:
        aufruf.lege_ergebnisordner_an(ordner)
    except ergebnis.NichtSchreibbar:
        raise KitNichtBeschreibbar(ordner)
    if not os.access(ordner, os.W_OK | os.X_OK):
        raise KitNichtBeschreibbar(ordner)
    return ordner


def starte(aufruf, sage, steuere, kit: finden.Kit | None = None, ziel: str | None = None,
           vor_dem_warten: list[str] | None = None) -> int:
    """Startet oder verwendet die Werkstatt eines Kits und öffnet den Browser; endet mit der Steuerzeile der Sitzung.
    kit: schon gewählt (z. B. von /atlas:neu), sonst nach dem Argument; ziel: Teil des Einmal-Links (WS-NK5);
    vor_dem_warten: Zeilen vor dem festen Satz der Sitzung. Merkt ein schreibbares Kit in der Liste (WS-NK8)."""
    if kit is None:
        kit = _kit_zum_start(aufruf)
    try:
        ergebnisordner = _ergebnisordner(aufruf, kit)
    except KitNichtBeschreibbar as fehler:
        sage(texte.werkstatt_kit_nicht_schreibbar(aufruf.tilde(fehler.ordner)))
        return 1
    ordner = _ordner(aufruf)
    datei = _zustand_datei(ordner, kit.ordner)
    try:
        laufend = _laufender(datei, kit.ordner)
    except AntwortetNicht:
        sage(texte.werkstatt_antwortet_nicht(kit.kit_id))
        return 1
    if laufend is not None:
        sage(texte.werkstatt_laeuft_schon(kit.kit_id))
    else:
        try:
            laufend = _starte_server(aufruf, sage, kit, ergebnisordner, ordner, datei)
        except StartFehler as fehler:
            sage(texte.werkstatt_nicht_gestartet(str(fehler)))
            return 1
        sage(texte.werkstatt_laeuft(kit.kit_id))
    if not laufend["status"].get("schreibbar", False):
        sage(texte.WERKSTATT_NUR_LESEN)
    else:
        zuletzt.merke(aufruf.datenordner(), kit.ordner)
    sage(_oeffne(aufruf, laufend, ziel))
    if aufruf.neu_angelegt:
        sage(texte.NEU_ANGELEGT)
        aufruf.neu_angelegt = False
    for zeile in vor_dem_warten or []:
        sage(zeile)
    alter_verlauf = (_status(laufend) or laufend["status"]).get("alter_verlauf")
    if isinstance(alter_verlauf, dict) and isinstance(alter_verlauf.get("ort"), str):
        sage(texte.alter_verlauf_im_chat(alter_verlauf["ort"]))
    sage(texte.WERKSTATT_WARTET)
    # Sitzungs-ID dieser Claude-Code-Sitzung: übernimmt die Werkstatt, eine ältere Sitzung gilt als abgelöst.
    steuere([f"{werkstatt_sitzung.STEUER}werkstatt: {datei.stem}.s{secrets.token_hex(4)}"])
    return 0


def _laufende(aufruf) -> list[tuple[Path, dict]]:
    ordner = _datenordner(aufruf)
    gefunden = []
    for datei in sorted(ordner.glob("*.json")) if ordner.is_dir() else []:
        zustand = _lies_zustand(datei)
        if zustand is None or not werkstatt_sitzung.lebt(zustand["pid"]):
            _entferne(datei)
            continue
        gefunden.append((datei, zustand))
    return gefunden


def oeffne(aufruf, sage) -> int:
    geoeffnet = 0
    for _datei, zustand in _laufende(aufruf):
        if _status(zustand) is None:
            continue
        geoeffnet += 1
        sage(texte.werkstatt_offen(str(zustand.get("kit_id", "?"))))
        sage(_oeffne(aufruf, zustand))
    if not geoeffnet:
        sage(texte.KEINE_WERKSTATT)
    return 0


def stoppe(aufruf, sage, ziel: str = ALLE) -> int:
    """'stopp [ordner | kit-name]' beendet nur die Werkstatt dieses Kits (ohne Angabe: des Arbeitsordners),
    'stopp --alle' alle des Datenordners (manueller Test 24.10.2026). Die Zustand-Datei fällt nur weg, wenn ihr
    Prozess weg ist (oder nachweislich kein eigener Server); lebt er nach STOPP_WARTEN_S noch oder lässt sich das
    nicht prüfen, bleibt sie, und eine Zeile sagt es."""
    laufende = _laufende(aufruf)
    if ziel != ALLE:
        aufruf.argument = ziel
        aufruf.unterbefehl = STOPP
        try:
            kit = aufruf.gewaehltes_kit()
        except finden.KeinKit:
            if ziel:
                raise
            sage(texte.WERKSTATT_STOPP_OHNE_KIT)
            return 1
        eigene = _zustand_datei(_datenordner(aufruf), kit.ordner)
        laufende = [(d, z) for d, z in laufende if d == eigene]
        if not laufende:
            sage(texte.keine_werkstatt_fuer(kit.kit_id))
            return 0
    beendet = 0
    noch_da = []
    for datei, zustand in laufende:
        if _anfrage(zustand, "POST", "/api/beenden") is None:
            eigener = _ist_eigener_server(zustand, datei)
            if eigener is False:
                _entferne(datei)
                continue
            if eigener:
                gestartet = zustand.get("gestartet")
                plattform.beende(zustand["pid"], gestartet if isinstance(gestartet, (int, float)) else None)
        ende = time.monotonic() + STOPP_WARTEN_S
        while werkstatt_sitzung.lebt(zustand["pid"]) and time.monotonic() < ende:
            time.sleep(0.05)
            _ernte(zustand["pid"])
        if werkstatt_sitzung.lebt(zustand["pid"]):
            noch_da.append(texte.werkstatt_laeuft_noch(str(zustand.get("kit_id", "?")), zustand["pid"]))
            continue
        _entferne(datei)
        beendet += 1
    for zeile in noch_da:
        sage(zeile)
    if ziel != ALLE and beendet:
        sage(texte.werkstatt_fuer_beendet(kit.kit_id))
    elif beendet or not noch_da:
        sage(texte.werkstatt_beendet(beendet) if beendet else texte.KEINE_WERKSTATT)
    return 0


def _ernte(pid: int) -> None:
    plattform.ernte(pid)


def knopf(aufruf, sage, ziel: str) -> int:
    """WS-NK7: legt die Doppelklick-Datei der Plattform in den Kit-Ordner; eine vorhandene bleibt."""
    aufruf.argument = ziel
    aufruf.unterbefehl = KNOPF
    kit = aufruf.gewaehltes_kit()
    if finden.ist_installierte_kopie(kit.ordner, aufruf.home):
        sage(texte.WERKSTATT_NUR_LESEN)
        return 1
    name = kit_anlegen.knopf_name()
    if name is None:
        sage(texte.KNOPF_NUR_MAC_WINDOWS)
        return 1
    try:
        kit_anlegen.schreibe_knopf(kit.ordner)
    except FileExistsError:
        # r21-7: Nur die eigene Datei heißt „bleibt“; eine fremde gleichen Namens wird nicht als unsere ausgegeben.
        stand = kit_anlegen.knopf_stand(kit.ordner / name)
        if stand == kit_anlegen.KNOPF_AKTUELL:
            sage(texte.knopf_schon_da(name, aufruf.tilde(kit.ordner)))
            return 0
        if stand == kit_anlegen.KNOPF_AELTER:  # r21-16
            sage(texte.knopf_aelter(name, aufruf.tilde(kit.ordner)))
            return 0
        sage(texte.knopf_fremd(name, aufruf.tilde(kit.ordner)))
        return 1
    except OSError:
        raise ergebnis.NichtSchreibbar(kit.ordner)
    sage(texte.knopf_angelegt(name, aufruf.tilde(kit.ordner)))
    return 0


def fuehre_aus(aufruf, sage, steuere) -> int:
    teile = aufruf.argument.split(None, 1)
    if teile and teile[0] == STOPP:
        rest = " ".join([*teile[1:], *aufruf.weitere]).strip()
        return stoppe(aufruf, sage, finden.ohne_anfuehrungszeichen(rest) if rest else "")
    if aufruf.argument == OEFFNEN:
        return oeffne(aufruf, sage)
    if teile and teile[0] == KNOPF:
        rest = " ".join([*teile[1:], *aufruf.weitere]).strip()
        return knopf(aufruf, sage, finden.ohne_anfuehrungszeichen(rest) if rest else "")
    if aufruf.argument in werkstatt_sitzung.BEFEHLE:
        return werkstatt_sitzung.fuehre_aus(aufruf, sage, steuere, [aufruf.argument, *aufruf.weitere])
    return starte(aufruf, sage, steuere)


if __name__ == "__main__":
    sys.exit(1)
