"""Startseiten-Server der Start-App (specs/start-app.yaml SA-S, SA-A, SA-T).

Läuft abgelöst von `bin/atlas-kit start` mit demselben Python, nur Standardbibliothek, nur auf 127.0.0.1 und einem
Port vom System. Zwei Geheimnisse wie in der Werkstatt (werkstatt_server.py): Der Seiten-Schlüssel verlässt den
Server nur gegen einen Einmal-Link (#einmal=<ticket>), den das Fenster über eine Weiterleitungsdatei bekommt; das
Öffnen-Recht steht in start.json (0600) und darf nur den Status lesen und das Fenster öffnen. Der Server liest und
prüft nur Pfade und legt höchstens den Standard-Ort ~/Pipelines an; ein Kit ändert er nie. Er endet nach Leerlauf.

Aufruf: python start_server.py --daten <ordner> [--leerlauf <s>] [--ticket-gueltig <s>]
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
import re  # noqa: E402
import secrets  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

BEFEHLE = Path(__file__).resolve().parent
PAKET = BEFEHLE.parent
if str(BEFEHLE) not in sys.path:
    sys.path.insert(0, str(BEFEHLE))

import ergebnis  # noqa: E402
import finden  # noqa: E402
import start_anbindung  # noqa: E402
import start_plattform  # noqa: E402

KOPF_SCHLUESSEL = "X-Atlas-Start"
ZUSTAND = "start.json"
ORT_DATEI = "ort.json"
STANDARD_ORT = "Pipelines"
SEITE_DATEI = PAKET / "start" / "startseite.html"
VORLAGE = PAKET / "seite" / "vorlage.html"
INHALT_HOECHSTENS = 64 * 1024
CONTENT_LENGTH = re.compile(r"[0-9]{1,18}")
LEERLAUF_S = 10 * 60
TAKT_S = 1.0
SOCKET_TIMEOUT_S = 30
VERBINDUNGEN_HOECHSTENS = 16
TICKET_GUELTIG_S = 10 * 60
TICKETS_HOECHSTENS = 3
SEITE, OEFFNEN = "seite", "oeffnen"
OEFFNEN_DARF = {("GET", "/api/status"), ("POST", "/api/oeffnen")}
OHNE_SCHLUESSEL = {"/api/einloesen"}
SCHRIFT = re.compile(r"url\((['\"]?)(data:font/woff2;base64,[A-Za-z0-9+/=]+)\1\)")
SCHRIFT_BLOCK = re.compile(r"/\*schrift\*/.*?/\*schrift-ende\*/", re.S)
SKRIPT = re.compile(r"<script>(.*?)</script>", re.S)
SICHERHEITS_KOEPFE = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}

LINK_VERBRAUCHT = "Dieser Link wurde schon verwendet. Öffne Atlas Werkstatt noch einmal."
KEIN_KIT = ("In {0} liegt kein Kit. Ein Kit ist ein Ordner mit .claude-plugin/plugin.json oder mit eigenen Skills bzw. "
            "Agenten in .claude. Wähl einen anderen Ordner oder leg ein neues Kit an.")
CLAUDE_FEHLT = ("Claude Code fehlt auf diesem Rechner. Die Werkstatt braucht es für die Arbeit der KI. Installier es von "
                "claude.com/claude-code und öffne Atlas Werkstatt danach noch einmal.")
ORDNER_BELEGT = "In {0} liegt schon ein Ordner {1}. Nimm einen anderen Namen oder Ort."
ORT_FEHLT = "Den Ort {0} gibt es nicht. Wähl mit „Ort ändern…“ einen anderen."
NICHT_ANLEGEN = "Neue Kits legt diese Fassung noch nicht an."
AUSWAHL_OFFEN = "Die Ordnerauswahl ist schon offen."
TERMINAL_NICHT = "Das Terminal-Fenster ließ sich nicht öffnen."
WAEHLEN_GEHT_NICHT = "Die Ordnerauswahl gibt es auf diesem System nicht. Atlas Werkstatt läuft auf macOS und Windows."
TITEL_KIT = "Wähl den Ordner des Kits, das du öffnen willst."
TITEL_ORT = "Wähl den Ort, an dem das neue Kit liegen soll."


class Fehler(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
        self.text = text


def baue_seite(html: str, vorlage: str | None) -> tuple[bytes, str]:
    """Setzt die Schrift aus der Vorlage des Pakets ein (sonst Systemschrift) und berechnet die CSP (SA-S3)."""
    treffer = SCHRIFT.search(vorlage) if vorlage else None
    if treffer:
        html = html.replace("__ATLAS_SCHRIFT__", treffer.group(2))
    else:
        html = SCHRIFT_BLOCK.sub("", html)
    skripte = SKRIPT.findall(html)
    if len(skripte) != 1:
        raise RuntimeError("Startseite ohne genau ein Skript")
    digest = base64.b64encode(hashlib.sha256(skripte[0].encode("utf-8")).digest()).decode("ascii")
    csp = ("default-src 'none'; "
           f"script-src 'sha256-{digest}'; "
           "style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'self'; "
           "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return html.encode("utf-8"), csp


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, daten: Path, seite_html: bytes, csp: str, plattform: start_plattform.Plattform,
                 home: Path, leerlauf: float = LEERLAUF_S, ticket_gueltig: float = TICKET_GUELTIG_S):
        super().__init__(("127.0.0.1", 0), Anfrage)
        self.daten = daten
        self.seite_html = seite_html
        self.csp = csp
        self.plattform = plattform
        self.home = home
        self.leerlauf = leerlauf
        self.ticket_gueltig = ticket_gueltig
        self.seiten_schluessel = secrets.token_urlsafe(32)
        self.oeffnen_recht = secrets.token_urlsafe(32)
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.urspruenge = {f"http://{h}" for h in self.hosts}
        self.letzte_anfrage = time.monotonic()
        self.beendet = threading.Event()
        self.tickets: list[tuple[str, float]] = []
        self.weiterleitungen: dict[str, Path] = {}
        self.sperre = threading.Lock()
        self.auswahl = threading.Lock()
        self.verbindungen = threading.BoundedSemaphore(VERBINDUNGEN_HOECHSTENS)

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

    # ------------------------------------------------------------ Ende, Tickets, Fenster

    def beende(self) -> None:
        if self.beendet.is_set():
            return
        self.beendet.set()
        threading.Thread(target=self.shutdown, daemon=True).start()

    def beobachte(self) -> None:
        while not self.beendet.wait(TAKT_S):
            if time.monotonic() - self.letzte_anfrage > self.leerlauf:
                self.beende()
                return
            try:
                self.raeume_weiterleitungen()
                start_plattform.raeume_sitzungen(self.daten)  # r22-7
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def einmal_link(self) -> str:
        ticket = secrets.token_urlsafe(32)
        with self.sperre:
            jetzt = time.monotonic()
            self.tickets = [(t, e) for t, e in self.tickets if e > jetzt]
            self.tickets.append((ticket, jetzt + self.ticket_gueltig))
            self.tickets = self.tickets[-TICKETS_HOECHSTENS:]
        self.raeume_weiterleitungen()
        return f"http://127.0.0.1:{self.port}/#einmal={ticket}"

    def loese_ein(self, ticket: object) -> str | None:
        if not isinstance(ticket, str):
            return None
        with self.sperre:
            jetzt = time.monotonic()
            self.tickets = [(t, e) for t, e in self.tickets if e > jetzt]
            gueltig = next((t for t, _ in self.tickets
                            if hmac.compare_digest(t.encode(), ticket.encode("utf-8", "replace"))), None)
            if gueltig is None:
                return None
            self.tickets = [(t, e) for t, e in self.tickets if t != gueltig]
        self.raeume_weiterleitungen()
        return self.seiten_schluessel

    def raeume_weiterleitungen(self, alle: bool = False) -> None:
        with self.sperre:
            offen = {t for t, e in self.tickets if e > time.monotonic()}
            weg = [t for t in self.weiterleitungen if alle or t not in offen]
            dateien = [self.weiterleitungen.pop(t) for t in weg]
        for datei in dateien:
            try:
                datei.unlink()
            except OSError:
                pass

    def oeffne_fenster(self) -> dict:
        """SA-F1: Fenster ohne Adressleiste über eine Weiterleitungsdatei auf den Einmal-Link."""
        link = self.einmal_link()
        ticket = link.rsplit("=", 1)[1]
        datei = self.daten / f"oeffnen-{secrets.token_urlsafe(9)}.html"
        html = ('<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                f'<meta http-equiv="refresh" content="0;url={link}"><title>Atlas Werkstatt</title></head>'
                f'<body><a href="{link}">Atlas Werkstatt öffnen</a></body></html>\n')
        try:
            ergebnis.schreibe_atomar(datei, html.encode("utf-8"), modus=0o600)
        except (OSError, ergebnis.NichtSchreibbar):
            return {"geoeffnet": False}
        with self.sperre:
            self.weiterleitungen[ticket] = datei
        if self.plattform.fenster(datei):
            return {"geoeffnet": True}
        with self.sperre:
            self.weiterleitungen.pop(ticket, None)
        try:
            datei.unlink()
        except OSError:
            pass
        return {"geoeffnet": False}

    # ------------------------------------------------------------ Inhalt

    def anzeige(self, pfad: Path) -> str:
        return finden.tilde(pfad, self.home)

    def ort(self) -> Path:
        try:
            daten = json.loads((self.daten / ORT_DATEI).read_text(encoding="utf-8"))
            ort = Path(daten["ort"])
            if ort.is_absolute() and ort.is_dir():
                return ort
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return self.home / STANDARD_ORT

    def merke_ort(self, ort: Path) -> None:
        try:
            ergebnis.schreibe_atomar(self.daten / ORT_DATEI, json.dumps({"ort": str(ort)}).encode("utf-8"), modus=0o600)
        except (OSError, ergebnis.NichtSchreibbar):
            pass

    def status(self) -> dict:
        ort = self.ort()
        zuletzt = []
        for eintrag in start_anbindung.zuletzt(self.daten.parent, self.home):
            pfad = Path(eintrag["pfad"])
            zuletzt.append({"kit_id": eintrag["kit_id"], "pfad": str(pfad), "anzeige": self.anzeige(pfad)})
        return {"claude": self.plattform.claude() is not None, "plattform": self.plattform.name,
                "ort": {"pfad": str(ort), "anzeige": self.anzeige(ort)}, "zuletzt": zuletzt,
                "anlegen": start_anbindung.kann_anlegen()}

    def _claude(self) -> str:
        claude = self.plattform.claude()
        if claude is None:
            raise Fehler(409, CLAUDE_FEHLT)
        return claude

    def oeffne_kit(self, pfad: object) -> dict:
        """SA-A3: nur ein Kit; die Sitzung startet im Terminal-Fenster mit /atlas:werkstatt."""
        if not isinstance(pfad, str) or not pfad or "\x00" in pfad or not os.path.isabs(pfad):
            raise Fehler(400, "Der Ordner fehlt.")
        ordner = Path(os.path.abspath(pfad))
        try:
            kit = finden.pruefe_ordner(ordner, self.home, "/atlas:werkstatt")
        except finden.KeinKit:
            raise Fehler(400, KEIN_KIT.format(self.anzeige(ordner)))
        claude = self._claude()
        if not self.plattform.terminal(kit.ordner, claude, ["/atlas:werkstatt"], self.daten):
            raise Fehler(500, TERMINAL_NICHT)
        return {"ok": True, "kit_id": kit.kit_id, "anzeige": self.anzeige(kit.ordner)}

    def lege_an(self, name: object, ort_angabe: object) -> dict:
        """SA-A4: prüft Name und Ort, merkt den Ort und startet /atlas:neu im Terminal-Fenster."""
        if not start_anbindung.kann_anlegen():
            raise Fehler(501, NICHT_ANLEGEN)
        grund = start_anbindung.pruefe_name(name)
        if grund is not None:
            raise Fehler(400, grund)
        if not isinstance(ort_angabe, str) or not ort_angabe or "\x00" in ort_angabe:
            raise Fehler(400, "Der Ort fehlt.")
        ort_angabe = os.path.expanduser(ort_angabe) if ort_angabe.startswith("~") else ort_angabe
        if not os.path.isabs(ort_angabe):
            raise Fehler(400, ORT_FEHLT.format(ort_angabe))
        ort = Path(os.path.abspath(ort_angabe))
        standard = self.home / STANDARD_ORT
        if not ort.is_dir():
            if ort == standard:
                try:
                    ort.mkdir(parents=False)
                except OSError:
                    raise Fehler(409, ORT_FEHLT.format(self.anzeige(ort)))
            else:
                raise Fehler(409, ORT_FEHLT.format(self.anzeige(ort)))
        ziel = ort / str(name)
        if ziel.is_symlink() or ziel.exists() and (not ziel.is_dir() or any(ziel.iterdir())):
            raise Fehler(409, ORDNER_BELEGT.format(self.anzeige(ort), name))
        claude = self._claude()
        self.merke_ort(ort)
        if not self.plattform.terminal(ort, claude, [f"/atlas:neu {name}"], self.daten):
            raise Fehler(500, TERMINAL_NICHT)
        return {"ok": True, "anzeige": self.anzeige(ziel)}

    def waehle(self, zweck: object) -> dict:
        """SA-A2: Ordnerauswahl des Systems, eine zugleich."""
        if zweck not in ("kit", "ort"):
            raise Fehler(400, "zweck ist kit oder ort.")
        if not self.auswahl.acquire(blocking=False):
            raise Fehler(409, AUSWAHL_OFFEN)
        try:
            start = self.ort() if zweck == "ort" else self.home
            try:
                pfad = self.plattform.waehle_ordner(TITEL_KIT if zweck == "kit" else TITEL_ORT, start)
            except start_plattform.NichtMoeglich:
                raise Fehler(501, WAEHLEN_GEHT_NICHT)
        finally:
            self.auswahl.release()
        if pfad is None:
            return {"abgebrochen": True}
        if zweck == "kit":
            return self.oeffne_kit(pfad)
        return {"pfad": pfad, "anzeige": self.anzeige(Path(pfad))}


class Anfrage(BaseHTTPRequestHandler):
    server: Server
    server_version = "atlas-start"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = SOCKET_TIMEOUT_S

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pfad = urlsplit(self.path).path if isinstance(self.path, str) else "?"
        sys.stderr.write(f"{self.log_date_time_string()} {self.command} {pfad[:200]}\n")

    def log_request(self, code="-", size="-") -> None:
        pfad = urlsplit(self.path).path if isinstance(self.path, str) else "?"
        sys.stderr.write(f"{self.log_date_time_string()} {self.command} {pfad[:200]} {code}\n")

    def _koepfe(self, status: int, art: str, laenge: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", art)
        for name, wert in {**SICHERHEITS_KOEPFE, **(extra or {})}.items():
            self.send_header(name, wert)
        self.send_header("Content-Length", str(laenge))
        self.end_headers()

    def _json(self, status: int, daten: object) -> None:
        rumpf = json.dumps(daten, ensure_ascii=False).encode("utf-8")
        self._koepfe(status, "application/json; charset=utf-8", len(rumpf))
        self.wfile.write(rumpf)

    def _fehler(self, status: int, text: str) -> None:
        self.close_connection = True
        self._json(status, {"fehler": text})

    def _rolle(self) -> str | None:
        gegeben = self.headers.get(KOPF_SCHLUESSEL, "").encode("utf-8", "replace")
        if hmac.compare_digest(gegeben, self.server.seiten_schluessel.encode("utf-8")):
            return SEITE
        if hmac.compare_digest(gegeben, self.server.oeffnen_recht.encode("utf-8")):
            return OEFFNEN
        return None

    def _origin_passt(self, pflicht: bool) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return not pflicht
        return origin in self.server.urspruenge

    def _vorbereiten(self, post: bool) -> str | None:
        if self.headers.get("Host", "") not in self.server.hosts:
            self._fehler(403, "Host nicht erlaubt.")
            return None
        pfad = urlsplit(self.path).path
        if not pfad.startswith("/api/"):
            if post or not self._origin_passt(pflicht=False):
                self._fehler(403, "Origin nicht erlaubt.")
                return None
            return pfad
        if pfad in OHNE_SCHLUESSEL:
            if not post or not self._origin_passt(pflicht=True):
                self._fehler(403, "Origin nicht erlaubt.")
                return None
            return pfad
        rolle = self._rolle()
        if rolle is None:
            self._fehler(401, "Schlüssel fehlt oder ist falsch.")
            return None
        if not self._origin_passt(pflicht=post):
            self._fehler(403, "Origin nicht erlaubt.")
            return None
        if rolle == OEFFNEN and ("POST" if post else "GET", pfad) not in OEFFNEN_DARF:
            self._fehler(403, "Dieser Schlüssel darf das nicht.")
            return None
        self.server.letzte_anfrage = time.monotonic()
        return pfad

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

    def do_GET(self) -> None:  # noqa: N802
        pfad = self._vorbereiten(post=False)
        if pfad is None:
            return
        try:
            if pfad == "/":
                self._koepfe(200, "text/html; charset=utf-8", len(self.server.seite_html),
                             {"Content-Security-Policy": self.server.csp})
                self.wfile.write(self.server.seite_html)
            elif pfad == "/api/status":
                self._json(200, self.server.status())
            else:
                self._fehler(404, "Gibt es nicht.")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._fehler(500, "Interner Fehler.")

    def do_POST(self) -> None:  # noqa: N802
        pfad = self._vorbereiten(post=True)
        if pfad is None:
            return
        s = self.server
        aktionen = {
            "/api/einloesen": lambda d: self._einloesen(d),
            "/api/oeffnen": lambda d: self._json(200, s.oeffne_fenster()),
            "/api/oeffne-kit": lambda d: self._json(200, s.oeffne_kit(d.get("pfad"))),
            "/api/anlegen": lambda d: self._json(200, s.lege_an(d.get("name"), d.get("ort"))),
            "/api/waehle": lambda d: self._json(200, s.waehle(d.get("zweck"))),
            "/api/beenden": lambda d: self._beenden(),
        }
        aktion = aktionen.get(pfad)
        if aktion is None:
            self._fehler(404, "Gibt es nicht.")
            return
        daten = self._inhalt()
        if daten is None:
            return
        try:
            aktion(daten)
        except Fehler as fehler:
            self._fehler(fehler.status, fehler.text)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._fehler(500, "Interner Fehler.")

    def _einloesen(self, daten: dict) -> None:
        schluessel = self.server.loese_ein(daten.get("ticket"))
        if schluessel is None:
            self._fehler(410, LINK_VERBRAUCHT)
            return
        self.server.letzte_anfrage = time.monotonic()
        self._json(200, {"schluessel": schluessel})

    def _beenden(self) -> None:
        self._json(200, {"ok": True})
        self.server.beende()

    def _nicht_erlaubt(self) -> None:
        self._fehler(405, "Methode nicht erlaubt.")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _nicht_erlaubt


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
    parser = argparse.ArgumentParser(description="Startseiten-Server von Atlas Werkstatt")
    parser.add_argument("--daten", required=True, type=Path)
    parser.add_argument("--leerlauf", type=float, default=LEERLAUF_S)
    parser.add_argument("--ticket-gueltig", type=float, default=TICKET_GUELTIG_S)
    args = parser.parse_args(argv)
    for strom in (sys.stdout, sys.stderr):
        strom.reconfigure(encoding="utf-8", errors="replace")
    try:
        vorlage = VORLAGE.read_text(encoding="utf-8") if VORLAGE.is_file() else None
        seite_html, csp = baue_seite(SEITE_DATEI.read_text(encoding="utf-8"), vorlage)
    except (OSError, RuntimeError) as fehler:
        sys.stderr.write(f"Startseite unbrauchbar ({fehler})\n")
        return 1
    daten = args.daten
    if not daten.exists():
        daten.mkdir(parents=True, mode=0o700)
    if os.name == "posix":
        os.chmod(daten, 0o700)
    start_plattform.raeume_sitzungen(daten)
    home = Path(os.path.expanduser("~"))
    server = Server(daten, seite_html, csp, start_plattform.aktuelle(home=home), home, args.leerlauf,
                    args.ticket_gueltig)
    signal.signal(signal.SIGTERM, lambda *_: server.beende())
    signal.signal(signal.SIGINT, lambda *_: server.beende())
    threading.Thread(target=server.beobachte, daemon=True).start()
    lauf = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    lauf.start()
    zustand_datei = daten / ZUSTAND
    zustand = {"pid": os.getpid(), "eltern": os.getppid(), "port": server.port, "oeffnen": server.oeffnen_recht, "gestartet": time.time()}
    ergebnis.schreibe_atomar(zustand_datei, json.dumps(zustand).encode("utf-8"), modus=0o600)
    sys.stderr.write(f"Startseite bereit auf Port {server.port}\n")
    sys.stderr.flush()
    try:
        while lauf.is_alive():
            lauf.join(0.5)
    finally:
        entferne_zustand(zustand_datei, os.getpid())
        server.raeume_weiterleitungen(alle=True)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
