"""Server der Werkstatt (specs/werkstatt.yaml WS-S, WS-X, WS-A).

Läuft abgelöst vom Aufruf mit dem Python der eigenen Umgebung (PyYAML für den Schreiber), nur auf
127.0.0.1 und einem Port vom System. Drei Geheimnisse (WS-X1):

- Seiten-Schlüssel: alle Rechte. Er verlässt den Server nie direkt: Der Browser bekommt einen Einmal-Link
  (#einmal=<ticket>, standardmäßig 10 Minuten gültig, höchstens 3 offen), den die Seite über POST
  /api/einloesen gegen den Schlüssel tauscht. Den Link bekommt der Browser nicht als Argument (ps zeigt ihn anderen
  lokalen Nutzern), sondern als Pfad einer Weiterleitungsdatei (0600, neben der Zustand-Datei), die nach dem
  Einlösen, nach Ablauf und beim Ende wegfällt. Ließ sich der Browser nicht öffnen, liegt der Link in einer Datei
  neben der Zustand-Datei (0600, außerhalb des Kits), die der Chat nennt; sie verschwindet nach dem Tausch, beim
  nächsten Öffnen und beim Ende. Der Schlüssel steht nie in Zustand-Datei, Chat oder Protokoll.
- Sitzungs-Schlüssel: für den Aufruf aus der Claude-Code-Sitzung (Zustand-Datei 0600). Darf nur lesen,
  Vorschläge nur über ergebnis mit voller Prüfung einreichen (Herkunft ki), Aufträge abholen (warte), Fortschritt
  melden, einen Auftrag als
  fehlgeschlagen melden und beenden – nicht den Browser öffnen, nicht anwenden, keine Rückfrage beantworten.
- Öffnen-Recht: nur POST /api/oeffnen, für `bin/atlas-kit werkstatt [kit|oeffnen]` (Zustand-Datei).

Die Schlüssel halten den Seiten-Schlüssel aus Chat und Protokollen heraus und verhindern, dass ein Vorschlag
sich selbst anwendet. Gegen eine bösartige lokale Sitzung mit Shell-Zugriff sind sie keine Grenze: Sie kann
alles lesen, was der Nutzer lesen kann.

Aufruf: python werkstatt_server.py --kit <ordner> --ergebnis <atlas-kit-ordner> --zustand <datei>
        --geheimnis <datei> [--ticket-gueltig <s>] [--leerlauf <s>] [--lebenszeichen <s>] [--sitzung-frist <s>]
        [--vorlage <vorlage.html>] [--scanner <atlas-ordner>] [--lauf-still-s <s>] [--lauf-ruhe-s <s>]
        [--startordner <ordner>]

KI (Ticket 13, WS-K): Aufträge der Seite holt die Claude-Code-Sitzung mit POST /api/auftraege/warte ab (lange
Anfrage, höchstens 25 s); Arbeitskopie, Auftragsdatei und Prüfung eingereichter Sätze in werkstatt_ki.py.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402

BEFEHLE = Path(__file__).resolve().parent
PAKET = BEFEHLE.parent
for _ordner in (PAKET / "atlas", BEFEHLE):
    if str(_ordner) not in sys.path:
        sys.path.insert(0, str(_ordner))

import browser  # noqa: E402
import ergebnis  # noqa: E402
import seite  # noqa: E402
import start_plattform  # noqa: E402
import werkstatt_auswerten  # noqa: E402
import werkstatt_einstellungen  # noqa: E402
import werkstatt_kern  # noqa: E402
import werkstatt_ki  # noqa: E402
import werkstatt_laeufe  # noqa: E402
from werkstatt_auftraege import AUSWERTEN, AuftragFehler  # noqa: E402

KOPF_SCHLUESSEL = "X-Atlas-Werkstatt"
INHALT_HOECHSTENS = 1024 * 1024
ABLASSEN_HOECHSTENS = 4 * 1024 * 1024
CONTENT_LENGTH = re.compile(r"[0-9]{1,18}")  # nur ASCII-Ziffern; str.isdigit ließe auch '²' durch
LEERLAUF_S = 2 * 60 * 60
LEBENSZEICHEN_S = 15.0
TAKT_S = 1.0
SOCKET_TIMEOUT_S = 30
VERBINDUNGEN_HOECHSTENS = 32
TICKET_GUELTIG_S = 10 * 60
TICKETS_HOECHSTENS = 3
SITZUNG_FRIST_S = 30.0
LAUF_STILL_S = 120.0
LAUF_RUHE_S = 30 * 60.0
LAEUFE = "/api/laeufe"
WARTE_VORGABE_S = 20.0
WARTE_HOECHSTENS_S = 25.0
LINK_VERBRAUCHT = "Dieser Link wurde schon verwendet. Öffne die Werkstatt neu mit /atlas:werkstatt oeffnen."
OHNE_SCHLUESSEL_PFADE = {"/api/einloesen"}
SEITE, SITZUNG, OEFFNEN = "seite", "sitzung", "oeffnen"
ZIELE = ("neue-pipeline",)  # WS-NK5: was die Seite nach dem Einlösen gleich öffnet


def ticket_aus(link: str) -> str:
    """Das Ticket eines Einmal-Links, mit oder ohne &ziel=…."""
    return link.split("#einmal=", 1)[1].split("&", 1)[0]
LEERER_BAUPLAN = {"schema": "atlas/0.1", "kit": {"id": "Werkstatt"}, "knoten": [], "kanten": [], "befunde": []}
KONFIGURATION = '<script type="application/json" id="atlas-werkstatt">{"version":1}</script>'
BUNDLE = re.compile(r"<script>(.*?)</script>", re.S)
SICHERHEITS_KOEPFE = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}
# Was der Sitzungs-Schlüssel darf (WS-X1); alles andere nur der Seiten-Schlüssel.
SITZUNG_DARF = {
    ("GET", "/api/status"), ("GET", "/api/bauplan"), ("GET", "/api/ereignisse"), ("GET", "/api/saetze"),
    ("GET", "/api/auftraege"), ("POST", "/api/auftraege/warte"),
    ("POST", "/api/auftraege/fortschritt"), ("POST", "/api/auftraege/ergebnis"), ("POST", "/api/auftraege/zustand"),
    ("POST", "/api/beenden"),
}
OEFFNEN_DARF = {("POST", "/api/oeffnen"), ("GET", "/api/status")}
# Was nur die Sitzung darf (WS-K4 bis WS-K6): Die Seite holt keine Aufträge ab und reicht keine Ergebnisse ein.
NUR_SITZUNG = {("POST", "/api/auftraege/warte"), ("POST", "/api/auftraege/fortschritt"),
               ("POST", "/api/auftraege/ergebnis")}


def baue_seite(vorlage: str) -> tuple[bytes, str]:
    html = seite.fuelle(vorlage, LEERER_BAUPLAN)
    if html.count("</head>") != 1:
        raise RuntimeError("Vorlage ohne genau ein </head>")
    html = html.replace("</head>", KONFIGURATION + "</head>")
    treffer = BUNDLE.search(html)
    if treffer is None:
        raise RuntimeError("Vorlage ohne eingebettetes Bundle")
    digest = base64.b64encode(hashlib.sha256(treffer.group(1).encode("utf-8")).digest()).decode("ascii")
    csp = ("default-src 'none'; "
           f"script-src 'sha256-{digest}'; "
           "style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'self'; "
           "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return html.encode("utf-8"), csp


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, werkstatt: werkstatt_kern.Werkstatt, seite_html: bytes, csp: str, leerlauf: float,
                 lebenszeichen: float, link_datei: Path, ticket_gueltig: float = TICKET_GUELTIG_S,
                 sitzung_frist: float = SITZUNG_FRIST_S, auftrag_frist: float = werkstatt_ki.AUFTRAG_FRIST_S):
        super().__init__(("127.0.0.1", 0), Anfrage)
        self.werkstatt = werkstatt
        self.seiten_schluessel = secrets.token_urlsafe(32)
        self.sitzungs_schluessel = secrets.token_urlsafe(32)
        self.oeffnen_recht = secrets.token_urlsafe(32)
        self.ticket_gueltig = ticket_gueltig
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.urspruenge = {f"http://{h}" for h in self.hosts}
        self.seite_html = seite_html
        self.csp = csp
        self.leerlauf = leerlauf
        self.lebenszeichen = lebenszeichen
        self.letzte_anfrage = time.monotonic()
        self.abonnenten: list[queue.Queue] = []
        self.abo_sperre = threading.Lock()
        self.verbindungen = threading.BoundedSemaphore(VERBINDUNGEN_HOECHSTENS)
        self.beendet = threading.Event()
        self.signatur = werkstatt.signatur()
        self.link_datei = link_datei
        self.tickets: list[tuple[str, float]] = []
        self.link_ticket: str | None = None  # das Ticket, dessen Link in link_datei steht
        self.weiterleitungen: dict[str, Path] = {}  # Ticket → Weiterleitungsdatei für den Browser
        self.ticket_sperre = threading.Lock()
        # KI (WS-K): Warteschlange, Dateien, Prüfung; 'Sitzung verbunden' nach WS-K10.
        self.ki = werkstatt_ki.KI(werkstatt)
        werkstatt.auftraege.bei_aenderung = lambda aid: self.sende("auftrag", {"id": aid})
        self.sitzung_frist = sitzung_frist
        self.auftrag_frist = auftrag_frist
        self.sitzung_kontakt: float | None = None
        self.wartend = 0
        self.sitzung_sperre = threading.Lock()
        self.verbunden_gemeldet = False
        self.laeufe: werkstatt_laeufe.Laeufe | None = None
        self.auswerten: werkstatt_auswerten.Auswerten | None = None

    # ------------------------------------------------------------ Sitzung verbunden (WS-K10)

    def sitzung_verbunden(self) -> bool:
        with self.sitzung_sperre:
            if self.wartend > 0:
                return True
            return self.sitzung_kontakt is not None and time.monotonic() - self.sitzung_kontakt <= self.sitzung_frist

    def kontakt(self, wartend: int = 0) -> None:
        with self.sitzung_sperre:
            self.sitzung_kontakt = time.monotonic()
            self.wartend += wartend
        self.pruefe_sitzung()

    def pruefe_sitzung(self) -> None:
        verbunden = self.sitzung_verbunden()
        with self.sitzung_sperre:
            geaendert = verbunden != self.verbunden_gemeldet
            self.verbunden_gemeldet = verbunden
        if geaendert:
            self.sende("sitzung", {"verbunden": verbunden})

    # ------------------------------------------------------------ Verbindungen begrenzen

    def process_request(self, request, client_address) -> None:
        if not self.verbindungen.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.verbindungen.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.verbindungen.release()

    # ------------------------------------------------------------ Ereignisse, Ende, Browser

    def sende(self, name: str, daten: object | None = None) -> None:
        with self.abo_sperre:
            for warteschlange in self.abonnenten:
                warteschlange.put((name, daten))

    def beende(self) -> None:
        if self.beendet.is_set():
            return
        self.beendet.set()
        self.werkstatt.auftraege.beende_alle()
        self.sende("ende")
        threading.Thread(target=self.shutdown, daemon=True).start()

    def beobachte(self) -> None:
        """Jede Sekunde: Leerlauf-Ende, Sitzung verbunden, Fristen, Weiterleitungen, Bauplan. Eine Ausnahme in einem
        Schritt (auch RecursionError aus einer tief verschachtelten Hook-Datei) beendet den Beobachter nicht: sie
        steht im Protokoll, und die übrigen Schritte laufen weiter."""
        while not self.beendet.wait(TAKT_S):
            if time.monotonic() - self.letzte_anfrage > self.leerlauf:
                self.beende()
                return
            for schritt in (self.pruefe_sitzung, lambda: self.ki.pruefe_fristen(self.auftrag_frist),
                            self.raeume_weiterleitungen, self._pruefe_bauplan, self._lauf_takt):
                try:
                    schritt()
                except OSError:
                    pass  # Datei gerade weg oder im Umbau; im nächsten Takt wieder
                except Exception:  # noqa: BLE001
                    traceback.print_exc()

    def _lauf_takt(self) -> None:
        if self.laeufe is not None:
            self.laeufe.takt()
        if self.auswerten is not None:
            self.auswerten.nach_zeit()

    def _pruefe_bauplan(self) -> None:
        signatur = self.werkstatt.signatur()
        if signatur != self.signatur:
            self.signatur = signatur
            self.sende("bauplan")

    def einmal_link(self, ziel: str | None = None) -> str:
        """Neues Ticket; abgelaufene fallen weg, und höchstens TICKETS_HOECHSTENS bleiben offen – ein neues macht das
        älteste darüber ungültig. ziel (WS-NK5, aus ZIELE) hängt als &ziel=<ziel> hinter dem Ticket."""
        ticket = secrets.token_urlsafe(32)
        with self.ticket_sperre:
            jetzt = time.monotonic()
            self.tickets = [(t, ende) for t, ende in self.tickets if ende > jetzt]
            self.tickets.append((ticket, jetzt + self.ticket_gueltig))
            self.tickets = self.tickets[-TICKETS_HOECHSTENS:]
        self.raeume_weiterleitungen()
        anhang = f"&ziel={ziel}" if ziel else ""
        return f"http://127.0.0.1:{self.port}/#einmal={ticket}{anhang}"

    def loese_ein(self, ticket: object) -> str | None:
        """Tauscht ein Einmal-Ticket gegen den Seiten-Schlüssel; danach ist es ungültig."""
        if not isinstance(ticket, str):
            return None
        with self.ticket_sperre:
            jetzt = time.monotonic()
            self.tickets = [(t, ende) for t, ende in self.tickets if ende > jetzt]
            gueltig = next((t for t, _ in self.tickets
                            if hmac.compare_digest(t.encode(), ticket.encode("utf-8", "replace"))), None)
            if gueltig is None:
                return None
            self.tickets = [(t, ende) for t, ende in self.tickets if t != gueltig]
            eigene_datei = self.link_ticket == gueltig
        if eigene_datei:  # die Link-Datei mit einem anderen, noch offenen Ticket bleibt
            self.entferne_link()
        self.raeume_weiterleitungen()
        return self.seiten_schluessel

    def entferne_link(self) -> None:
        with self.ticket_sperre:
            self.link_ticket = None
        try:
            self.link_datei.unlink()
        except OSError:
            pass

    def raeume_weiterleitungen(self, alle: bool = False) -> None:
        """Entfernt die Weiterleitungsdateien eingelöster, abgelaufener und verdrängter Tickets (bei alle: jede)."""
        with self.ticket_sperre:
            offen = {t for t, ende in self.tickets if ende > time.monotonic()}
            weg = [t for t in self.weiterleitungen if alle or t not in offen]
            dateien = [self.weiterleitungen.pop(t) for t in weg]
        for datei in dateien:
            try:
                datei.unlink()
            except OSError:
                pass

    def weiterleitung(self, link: str) -> Path:
        """Kurzlebige HTML-Datei (0600, neben der Zustand-Datei), die den Browser auf den Einmal-Link weiterleitet:
        So steht der Link nie in der Kommandozeile von open/xdg-open, die andere lokale Nutzer mit ps sehen."""
        datei = self.link_datei.with_name(f"{self.link_datei.stem}.oeffnen-{secrets.token_urlsafe(9)}.html")
        verweis = link.replace("&", "&amp;")  # nur &ziel=… (WS-NK5); Ticket und Port enthalten kein &
        html = ('<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                f'<meta http-equiv="refresh" content="0;url={verweis}"><title>Werkstatt</title></head>'
                f'<body><a href="{verweis}">Werkstatt öffnen</a></body></html>\n')
        ergebnis.schreibe_atomar(datei, html.encode("utf-8"), modus=0o600)
        with self.ticket_sperre:
            self.weiterleitungen[ticket_aus(link)] = datei
        return datei

    def oeffne_browser(self, ziel: str | None = None) -> dict:
        """WS-B1: Der Server öffnet den Standardbrowser selbst, mit einem Einmal-Link (auch für ATLAS_KIT_BROWSER),
        über eine Weiterleitungsdatei. Geht das nicht sicher im Browser (nur über die Dateizuordnung oder gar nicht),
        liegt der Link zusätzlich in einer Datei neben der Zustand-Datei (0600, außerhalb des Kits); eine geöffnete
        Weiterleitung bleibt bis zum Einlösen bzw. Ablauf des Tickets."""
        self.entferne_link()
        link = self.einmal_link(ziel)
        ticket = ticket_aus(link)
        try:
            datei = self.weiterleitung(link)
        except (OSError, ergebnis.NichtSchreibbar):
            datei = None
        wie = browser.oeffne(datei) if datei is not None else None
        if wie == browser.SICHER:
            return {"geoeffnet": True, "datei": None}
        if datei is not None and wie is None:
            with self.ticket_sperre:
                self.weiterleitungen.pop(ticket, None)
            try:
                datei.unlink()
            except OSError:
                pass
        versucht = {"versucht": True} if wie == browser.UNSICHER else {}  # mit der Dateizuordnung geöffnet
        try:
            ergebnis.schreibe_atomar(self.link_datei, (link + "\n").encode("utf-8"), modus=0o600)
        except (OSError, ergebnis.NichtSchreibbar):
            return {"geoeffnet": False, "datei": None, **versucht}
        with self.ticket_sperre:
            self.link_ticket = ticket
        return {"geoeffnet": False, "datei": str(self.link_datei), **versucht}


class Anfrage(BaseHTTPRequestHandler):
    server: Server
    server_version = "atlas-werkstatt"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = SOCKET_TIMEOUT_S

    # ------------------------------------------------------------ Protokoll ohne Schlüssel

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pfad = urlsplit(self.path).path if isinstance(self.path, str) else "?"
        sys.stderr.write(f"{self.log_date_time_string()} {self.command} {pfad[:200]}\n")

    def log_request(self, code="-", size="-") -> None:
        pfad = urlsplit(self.path).path if isinstance(self.path, str) else "?"
        sys.stderr.write(f"{self.log_date_time_string()} {self.command} {pfad[:200]} {code}\n")

    # ------------------------------------------------------------ Antworten

    def _koepfe(self, status: int, art: str, laenge: int | None, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", art)
        for name, wert in {**SICHERHEITS_KOEPFE, **(extra or {})}.items():
            self.send_header(name, wert)
        if laenge is not None:
            self.send_header("Content-Length", str(laenge))
        self.end_headers()

    def _json(self, status: int, daten: object) -> None:
        rumpf = json.dumps(daten, ensure_ascii=False).encode("utf-8")
        self._koepfe(status, "application/json; charset=utf-8", len(rumpf))
        self.wfile.write(rumpf)

    def _fehler(self, status: int, text: str) -> None:
        self.close_connection = True
        self._json(status, {"fehler": text})

    # ------------------------------------------------------------ Prüfungen

    def _rolle(self) -> str | None:
        gegeben = self.headers.get(KOPF_SCHLUESSEL, "").encode("utf-8", "replace")
        if hmac.compare_digest(gegeben, self.server.seiten_schluessel.encode("utf-8")):
            return SEITE
        if hmac.compare_digest(gegeben, self.server.sitzungs_schluessel.encode("utf-8")):
            return SITZUNG
        if hmac.compare_digest(gegeben, self.server.oeffnen_recht.encode("utf-8")):
            return OEFFNEN
        return None

    def _origin_passt(self, pflicht: bool) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return not pflicht
        return origin in self.server.urspruenge

    def _inhalt(self) -> dict | None:
        if self.headers.get("Transfer-Encoding"):
            self._fehler(411, "Anfragen brauchen Content-Length.")
            return None
        angabe = self.headers.get("Content-Length")
        if angabe is None or not CONTENT_LENGTH.fullmatch(angabe):
            self._fehler(411, "Anfragen brauchen Content-Length.")
            return None
        laenge = int(angabe)
        if laenge > INHALT_HOECHSTENS:
            rest = laenge
            while 0 < rest <= ABLASSEN_HOECHSTENS:
                stueck = self.rfile.read(min(rest, 65536))
                if not stueck:
                    break
                rest -= len(stueck)
            self._fehler(413, "Die Anfrage ist zu groß.")
            return None
        roh = self.rfile.read(laenge) if laenge else b""
        if len(roh) != laenge:
            self._fehler(400, "Die Anfrage ist unvollständig.")
            return None
        try:
            daten = json.loads(roh.decode("utf-8")) if roh else {}
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._fehler(400, "Die Anfrage ist kein JSON.")
            return None
        if not isinstance(daten, dict):
            self._fehler(400, "Die Anfrage ist kein JSON-Objekt.")
            return None
        return daten

    def _vorbereiten(self, post: bool) -> tuple[str, str | None] | None:
        if self.headers.get("Host", "") not in self.server.hosts:
            self._fehler(403, "Host nicht erlaubt.")
            return None
        pfad = urlsplit(self.path).path
        if not pfad.startswith("/api/"):
            return pfad, None
        if pfad in OHNE_SCHLUESSEL_PFADE:
            if not post or not self._origin_passt(pflicht=True):
                self._fehler(403, "Origin nicht erlaubt.")
                return None
            return pfad, None
        rolle = self._rolle()
        if rolle is None:
            self._fehler(401, "Schlüssel fehlt oder ist falsch.")
            return None
        if not self._origin_passt(pflicht=post):
            self._fehler(403, "Origin nicht erlaubt.")
            return None
        erlaubt = {SITZUNG: SITZUNG_DARF, OEFFNEN: OEFFNEN_DARF}.get(rolle)
        weg = ("POST" if post else "GET", pfad)
        if rolle == SEITE and weg in NUR_SITZUNG:
            self._fehler(403, "Das macht nur die Claude-Code-Sitzung.")
            return None
        if erlaubt is not None and weg not in erlaubt:
            self._fehler(403, "Dieser Schlüssel darf das nicht. Ändern geht nur in der Seite.")
            return None
        self.server.letzte_anfrage = time.monotonic()
        if rolle == SITZUNG:
            self.server.kontakt()
        return pfad, rolle

    # ------------------------------------------------------------ Methoden

    def do_GET(self) -> None:  # noqa: N802
        vorbereitet = self._vorbereiten(post=False)
        if vorbereitet is None:
            return
        pfad, _ = vorbereitet
        w = self.server.werkstatt
        try:
            if pfad == "/":
                self._koepfe(200, "text/html; charset=utf-8", len(self.server.seite_html),
                             {"Content-Security-Policy": self.server.csp})
                self.wfile.write(self.server.seite_html)
            elif pfad == "/api/status":
                self._json(200, w.status())
            elif pfad == "/api/bauplan":
                self._json(200, w.bauplan())
            elif pfad == "/api/saetze":
                self._json(200, {"saetze": self.server.ki.mit_lauf(w.vorschlaege.liste())})
            elif pfad == "/api/auftraege":
                self._json(200, {"auftraege": self.server.ki.fuer_seite(),
                                 "sitzung": {"verbunden": self.server.sitzung_verbunden()}})
            elif pfad == "/api/abschnitte":
                self._json(200, w.abschnitte())
            elif pfad == "/api/agent":
                namen = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get("name", [])
                self._json(200, w.agent(namen[0] if len(namen) == 1 else None))
            elif pfad == "/api/ereignisse":
                self._ereignisse()
            elif pfad == "/api/pipelines":
                self._json(200, self.server.auswerten.pipelines())
            elif pfad == LAEUFE:
                self._json(200, self.server.laeufe.liste())
            elif pfad.startswith(LAEUFE + "/"):
                self._json(200, self.server.laeufe.lauf(pfad[len(LAEUFE) + 1:]))
            else:
                self._fehler(404, "Gibt es nicht.")
        except werkstatt_kern.WerkstattFehler as fehler:
            self._json(fehler.status, fehler.daten)
        except werkstatt_laeufe.LaufFehler as fehler:
            self._json(fehler.status, fehler.daten)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._fehler(500, "Interner Fehler in der Werkstatt.")

    def do_POST(self) -> None:  # noqa: N802
        vorbereitet = self._vorbereiten(post=True)
        if vorbereitet is None:
            return
        pfad, rolle = vorbereitet
        aktionen = {
            "/api/vorschau": self._vorschau,
            "/api/gliederung": self._gliederung,
            "/api/anwenden": self._anwenden,
            "/api/rueckgaengig": self._rueckgaengig,
            "/api/speichern": self._speichern,
            "/api/saetze": self._satz_vorlegen,
            "/api/saetze/verwerfen": self._satz_verwerfen,
            "/api/auftraege": self._auftrag_einstellen,
            "/api/auftraege/warte": self._auftrag_warte,
            "/api/auftraege/fortschritt": self._auftrag_fortschritt,
            "/api/auftraege/ergebnis": self._auftrag_ergebnis,
            "/api/auftraege/antwort": self._auftrag_antwort,
            "/api/auftraege/gutachten": self._auftrag_gutachten,
            "/api/auftraege/auswertung": self._auftrag_auswertung,
            "/api/auftraege/gedaechtnis": self._auftrag_gedaechtnis,
            "/api/pipelines": self._pipelines,
            "/api/auftraege/zustand": self._auftrag_zustand,
            "/api/auftraege/nachricht": self._auftrag_nachricht,
            "/api/auftraege/uebernehmen": self._auftrag_uebernehmen,
            LAEUFE: self._lauf_starten,
            LAEUFE + "/abbrechen": self._lauf_abbrechen,
            LAEUFE + "/abschliessen": self._lauf_abschliessen,
            LAEUFE + "/loeschen": self._lauf_loeschen,
            LAEUFE + "/ordner": self._lauf_ordner,
            LAEUFE + "/ordner/pruefen": self._lauf_ordner_pruefen,
            "/api/alter-verlauf": self._alter_verlauf,
            "/api/oeffnen": self._oeffnen,
            "/api/einloesen": self._einloesen,
            "/api/beenden": self._beenden,
        }
        aktion = aktionen.get(pfad)
        if aktion is None:
            self._fehler(404, "Gibt es nicht.")
            return
        daten = self._inhalt()
        if daten is None:
            return
        try:
            aktion(daten, rolle)
        except werkstatt_kern.WerkstattFehler as fehler:
            self._json(fehler.status, fehler.daten)
        except AuftragFehler as fehler:
            self._json(fehler.status, {"fehler": fehler.text, "zustand": fehler.zustand})
        except werkstatt_laeufe.LaufFehler as fehler:
            self._json(fehler.status, fehler.daten)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._fehler(500, "Interner Fehler in der Werkstatt.")

    def _nicht_erlaubt(self) -> None:
        self._fehler(405, "Methode nicht erlaubt.")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _nicht_erlaubt

    # ------------------------------------------------------------ API

    def _vorschau(self, daten: dict, _rolle: str) -> None:
        self._json(200, self.server.werkstatt.vorschau(daten.get("satz"), daten.get("auswahl"), daten.get("satz_id"),
                                                       daten.get("mit_bauplan") is True))

    def _gliederung(self, daten: dict, _rolle: str) -> None:
        self._json(200, self.server.werkstatt.gliederung(daten.get("text")))

    def _nach_aenderung(self) -> None:
        self.server.signatur = self.server.werkstatt.signatur()
        self.server.sende("status")
        self.server.sende("bauplan")

    def _anwenden(self, daten: dict, _rolle: str) -> None:
        antwort = self.server.werkstatt.anwenden(daten.get("satz"), daten.get("auswahl"), daten.get("stand"),
                                                 daten.get("bestaetigt") is True, daten.get("satz_id"),
                                                 daten.get("pipeline"))
        self._nach_aenderung()
        if daten.get("satz_id") is not None:
            self.server.sende("satz")
        self.server.sende("pipelines")
        self._json(200, antwort)

    def _rueckgaengig(self, daten: dict, _rolle: str) -> None:
        antwort = self.server.werkstatt.rueckgaengig(daten.get("bestaetigt") is True, daten.get("id"))
        self._nach_aenderung()
        self.server.sende("pipelines")
        self._json(200, antwort)

    def _pipelines(self, daten: dict, _rolle: str) -> None:
        antwort = self.server.auswerten.schalte(daten)
        self.server.sende("pipelines")
        self._json(200, antwort)

    def _auftrag_auswertung(self, daten: dict, _rolle: str) -> None:
        self._json(200, {"auswertung": self.server.werkstatt.auftraege.auswertung(daten.get("id"))})

    def _auftrag_gedaechtnis(self, daten: dict, _rolle: str) -> None:
        antwort = self.server.auswerten.bereinige(daten.get("id"), daten.get("auswahl"))
        self.server.sende("satz", {"id": antwort["vorschlag"]})
        self._json(200, antwort)

    def _speichern(self, daten: dict, _rolle: str) -> None:
        antwort = self.server.werkstatt.speichern(daten.get("nachricht"))
        self.server.sende("status")
        self._json(200, antwort)

    def _satz_vorlegen(self, daten: dict, _rolle: str) -> None:
        """Nur die Seite legt Sätze vor (der Sitzungs-Schlüssel darf POST /api/saetze nicht); Sätze der KI kommen über
        die Aufträge."""
        vid = self.server.werkstatt.lege_vor(daten.get("satz"), werkstatt_kern.HAND)
        self.server.sende("satz", {"id": vid})
        self._json(200, {"ok": True, "id": vid})

    def _satz_verwerfen(self, daten: dict, _rolle: str) -> None:
        self.server.werkstatt.verwirf(daten.get("id"))
        self.server.sende("satz")
        self._json(200, {"ok": True})

    def _auftrag_einstellen(self, daten: dict, _rolle: str) -> None:
        if daten.get("art") == AUSWERTEN:
            if daten.get("gutachten") is not None or daten.get("auswahl") is not None \
                    or daten.get("auswertung") is not None or daten.get("ziel") is not None:
                raise AuftragFehler(400, "gutachten und auswahl gibt es nur beim Einbauen.")
            auftrag = self.server.auswerten.stelle(daten.get("bezug"), daten.get("text"))
        else:
            auftrag = self.server.werkstatt.auftraege.stelle_ein(daten.get("art"), daten.get("text"),
                                                                 daten.get("bezug"), daten.get("gutachten"),
                                                                 daten.get("auswahl"), ziel=daten.get("ziel"),
                                                                 auswertung=daten.get("auswertung"))
        self._json(200, {"ok": True, "id": auftrag["id"]})

    def _auftrag_warte(self, daten: dict, _rolle: str) -> None:
        sekunden = daten.get("sekunden", WARTE_VORGABE_S)
        if isinstance(sekunden, bool) or not isinstance(sekunden, (int, float)) or not 0 <= sekunden <= 600:
            raise AuftragFehler(400, "sekunden ist eine Zahl.")
        self.server.kontakt(wartend=1)
        try:
            antwort = self.server.ki.warte(daten.get("sitzung"), min(float(sekunden), WARTE_HOECHSTENS_S))
        finally:
            self.server.kontakt(wartend=-1)
        self._json(200, antwort)

    def _auftrag_fortschritt(self, daten: dict, _rolle: str) -> None:
        self.server.ki.fortschritt(daten.get("id"), daten.get("zeile"))
        self._json(200, {"ok": True})

    def _auftrag_ergebnis(self, daten: dict, _rolle: str) -> None:
        antwort = self.server.ki.ergebnis(daten.get("id"), daten.get("nur_frage") is True, daten.get("tokens"))
        if antwort.get("vorschlag"):
            self.server.sende("satz", {"id": antwort["vorschlag"]})
        self._json(200, antwort)

    def _auftrag_gutachten(self, daten: dict, _rolle: str) -> None:
        """WS-K15: das ganze geprüfte Gutachten eines Auftrags (nur Seite); die Liste trägt nur Zahlen."""
        self._json(200, {"gutachten": self.server.werkstatt.auftraege.gutachten(daten.get("id"))})

    def _auftrag_antwort(self, daten: dict, _rolle: str) -> None:
        self._json(200, {"auftrag": self.server.ki.antworte(daten.get("id"), daten.get("antwort"))})

    def _auftrag_zustand(self, daten: dict, rolle: str) -> None:
        """WS-K8: die Seite bricht ab, die Sitzung meldet fehlgeschlagen; fertig entsteht nur über ergebnis."""
        zustand = daten.get("zustand")
        if rolle == SEITE and zustand == "abgebrochen":
            auftrag = self.server.ki.brich_ab(daten.get("id"))
        elif rolle == SITZUNG and zustand == "fehlgeschlagen":
            auftrag = self.server.ki.scheitere(daten.get("id"))
        else:
            raise AuftragFehler(403, "Abbrechen geht nur aus der Seite, fehlgeschlagen meldet nur die Sitzung.")
        self._json(200, {"auftrag": auftrag})

    def _auftrag_nachricht(self, daten: dict, _rolle: str) -> None:
        """WS-KE5 (nur Seite): Nachricht zur letzten Fassung eines Entwurfs."""
        self._json(200, {"auftrag": self.server.ki.nachricht(daten.get("id"), daten.get("nachricht"))})

    def _auftrag_uebernehmen(self, daten: dict, _rolle: str) -> None:
        """WS-KE6 (nur Seite): gewählte Teile der letzten Fassung als Vorschlag der KI."""
        antwort = self.server.ki.uebernehmen(daten.get("id"), daten.get("fassung"), daten.get("teile"),
                                             daten.get("memory"), daten.get("logs"))
        self.server.sende("satz", {"id": antwort["vorschlag"]})
        self._json(200, {"ok": True, **antwort})

    def _lauf_starten(self, daten: dict, _rolle: str) -> None:
        self._json(200, self.server.laeufe.starte(daten))

    def _lauf_abbrechen(self, daten: dict, _rolle: str) -> None:
        self._json(200, self.server.laeufe.abbrechen(daten.get("id")))

    def _lauf_abschliessen(self, daten: dict, _rolle: str) -> None:
        self._json(200, self.server.laeufe.abschliessen(daten.get("id")))

    def _lauf_loeschen(self, daten: dict, _rolle: str) -> None:
        self._json(200, self.server.laeufe.loesche(daten.get("id")))

    def _lauf_ordner(self, _daten: dict, _rolle: str) -> None:
        self._json(200, self.server.laeufe.waehle_ordner())

    def _lauf_ordner_pruefen(self, daten: dict, _rolle: str) -> None:
        self._json(200, self.server.laeufe.pruefe_ordner(daten))

    def _alter_verlauf(self, daten: dict, _rolle: str) -> None:
        antwort = self.server.werkstatt.alter_verlauf(daten.get("aktion"))
        self.server.sende("status")
        self._json(200, antwort)

    def _einloesen(self, daten: dict, _rolle: str | None) -> None:
        schluessel = self.server.loese_ein(daten.get("ticket"))
        if schluessel is None:
            self._fehler(410, LINK_VERBRAUCHT)
            return
        self.server.letzte_anfrage = time.monotonic()
        self._json(200, {"schluessel": schluessel})

    def _oeffnen(self, daten: dict, _rolle: str) -> None:
        ziel = daten.get("ziel") if "ziel" in daten else None
        if "ziel" in daten and ziel not in ZIELE:
            self._fehler(400, "Unbekanntes Ziel.")
            return
        startordner = daten.get("startordner")
        if isinstance(startordner, str) and os.path.isabs(startordner) and os.path.isdir(startordner):
            self.server.werkstatt.neuer_startordner(Path(startordner))
            self.server.sende("status")
        self._json(200, self.server.oeffne_browser(ziel))

    def _beenden(self, _daten: dict, _rolle: str) -> None:
        self._json(200, {"ok": True})
        self.server.beende()

    def _ereignisse(self) -> None:
        warteschlange: queue.Queue = queue.Queue()
        with self.server.abo_sperre:
            self.server.abonnenten.append(warteschlange)
        try:
            self._koepfe(200, "text/event-stream; charset=utf-8", None)
            self._schreibe_ereignis("hallo", {})
            ende_gesendet = False
            while not self.server.beendet.is_set():
                try:
                    name, daten = warteschlange.get(timeout=self.server.lebenszeichen)
                except queue.Empty:
                    self.wfile.write(b": lebt\n\n")
                    self.wfile.flush()
                    continue
                self._schreibe_ereignis(name, daten if daten is not None else {})
                if name == "ende":
                    ende_gesendet = True
                    break
            # Beendet, während noch Ereignisse warteten: die noch senden, 'ende' immer zuletzt (WS-B4).
            while not ende_gesendet:
                try:
                    name, daten = warteschlange.get_nowait()
                except queue.Empty:
                    name, daten = "ende", {}
                try:
                    self._schreibe_ereignis(name, daten if daten is not None else {})
                except OSError:
                    break
                ende_gesendet = name == "ende"
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.server.abo_sperre:
                if warteschlange in self.server.abonnenten:
                    self.server.abonnenten.remove(warteschlange)
            self.close_connection = True

    def _schreibe_ereignis(self, name: str, daten: object) -> None:
        self.wfile.write(f"event: {name}\ndata: {json.dumps(daten, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()


def entferne_zustand(datei: Path, pid: int) -> None:
    try:
        daten = json.loads(datei.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(daten, dict) and daten.get("pid") == pid:
        try:
            datei.unlink()
        except OSError:
            pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Server der atlas-kit Werkstatt")
    parser.add_argument("--kit", required=True, type=Path)
    parser.add_argument("--ergebnis", required=True, type=Path)
    parser.add_argument("--zustand", required=True, type=Path)
    parser.add_argument("--geheimnis", type=Path, required=True)
    parser.add_argument("--ticket-gueltig", type=float, default=TICKET_GUELTIG_S)
    parser.add_argument("--leerlauf", type=float, default=LEERLAUF_S)
    parser.add_argument("--lebenszeichen", type=float, default=LEBENSZEICHEN_S)
    parser.add_argument("--sitzung-frist", type=float, default=SITZUNG_FRIST_S)
    parser.add_argument("--auftrag-frist", type=float, default=werkstatt_ki.AUFTRAG_FRIST_S)
    parser.add_argument("--vorlage", type=Path, default=PAKET / "seite" / "vorlage.html")
    parser.add_argument("--scanner", type=Path, default=PAKET / "atlas")
    parser.add_argument("--lauf-still-s", type=float, default=LAUF_STILL_S)
    parser.add_argument("--lauf-ruhe-s", type=float, default=LAUF_RUHE_S)
    parser.add_argument("--startordner", type=Path, default=None)
    args = parser.parse_args(argv)
    daten = args.zustand.parent.parent
    for strom in (sys.stdout, sys.stderr):
        strom.reconfigure(encoding="utf-8", errors="replace")
    try:
        werkstatt = werkstatt_kern.Werkstatt(args.kit, args.ergebnis, args.scanner, Path.home(), geheimnis=args.geheimnis,
                                             startordner=args.startordner, daten=daten)
    except Exception as fehler:  # noqa: BLE001
        sys.stderr.write(f"{fehler}\n")
        return 1
    try:
        seite_html, csp = baue_seite(args.vorlage.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, seite.VorlageFehler) as fehler:
        sys.stderr.write(f"Vorlage unbrauchbar ({fehler})\n")
        return 1
    server = Server(werkstatt, seite_html, csp, args.leerlauf, args.lebenszeichen, args.zustand.with_suffix(".link"),
                    args.ticket_gueltig, args.sitzung_frist, args.auftrag_frist)
    einstellungen = werkstatt_einstellungen.Einstellungen(werkstatt.ergebnis, werkstatt.ablage.geheimnis,
                                                          werkstatt.sperre)
    werkstatt.einstellungen = einstellungen
    server.laeufe = werkstatt_laeufe.Laeufe(werkstatt, werkstatt.ergebnis, daten, PAKET,
                                            start_plattform.aktuelle(), server.sende, args.lauf_still_s,
                                            args.lauf_ruhe_s, logs_an=lambda skill: einstellungen.fuer(skill).logs,
                                            bei_ende=lambda lauf: server.auswerten.bei_lauf_ende(lauf))
    server.auswerten = werkstatt_auswerten.Auswerten(werkstatt, server.laeufe, einstellungen, werkstatt.register,
                                                     server.sende)
    server.ki.auswerten = server.auswerten
    werkstatt.lauf_offen = server.laeufe.offen_im_kit
    server.auswerten.beim_start()
    signal.signal(signal.SIGTERM, lambda *_: server.beende())
    signal.signal(signal.SIGINT, lambda *_: server.beende())
    beobachter = threading.Thread(target=server.beobachte, daemon=True)
    beobachter.start()
    lauf = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    lauf.start()
    # eltern: unter Windows ist der Prozess, den werkstatt.py startet, oft der Starter der venv (WS-PW3).
    zustand = {"pid": os.getpid(), "eltern": os.getppid(), "port": server.port, "schluessel": server.sitzungs_schluessel,
               "oeffnen": server.oeffnen_recht,
               "kit": str(werkstatt.kit), "kit_id": werkstatt.kit_id, "ergebnis": str(server.ki.basis.parent),
               "gestartet": time.time()}
    if not args.zustand.parent.exists():
        args.zustand.parent.mkdir(parents=True, mode=0o700)
    ergebnis.schreibe_atomar(args.zustand, json.dumps(zustand).encode("utf-8"), modus=0o600)
    sys.stderr.write(f"Werkstatt für {werkstatt.kit_id} bereit auf Port {server.port}\n")
    sys.stderr.flush()
    try:
        while lauf.is_alive():
            lauf.join(0.5)
    finally:
        entferne_zustand(args.zustand, os.getpid())
        server.entferne_link()
        server.raeume_weiterleitungen(alle=True)
        server.ki.raeume_auf(alle=True)
        ende = time.monotonic() + 1.0
        while server.abonnenten and time.monotonic() < ende:
            time.sleep(0.05)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
