"""Was unter Windows anders läuft als unter macOS und Linux, an einer Stelle (docs/windows.md).

Prozesse prüfen und beenden, abgelöst starten, die Befehlszeile eines Prozesses lesen, Dateien ersetzen, die gerade
ein anderes Programm offen hat, Verweise (Symlink, Junction) erkennen. Die Prozess- und Datei-Fragen von Ticket 23
stehen hier; ist_windows() liest os.name bei jedem Aufruf, damit Tests die Windows-Zweige auf jedem Rechner durchlaufen
können. (Die Start-App und die Doppelklick-Datei aus Ticket 21/22 haben ihre eigenen Plattform-Schichten.)

Unter Windows ist os.kill(pid, 0) kein Test, ob ein Prozess lebt: 0 ist CTRL_C_EVENT und geht an die ganze
Konsolengruppe, jedes andere Signal beendet den Prozess mit TerminateProcess. Deshalb fragt lebt() dort über
OpenProcess/WaitForSingleObject. Nur Standardbibliothek.
"""
from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePath

# Werte aus der Windows-API; subprocess kennt die Namen nur unter Windows.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
# FILETIME zählt 100-ns-Schritte seit 1601; bis 1970 sind es so viele.
FILETIME_1970 = 116444736000000000
# Spielraum zwischen dem Anlegen des Prozesses und „gestartet“ in der Zustand-Datei (Uhr, Rundung).
STARTZEIT_SPIEL_S = 2.0
BEFEHLSZEILE_WARTEN_S = 20
ERSETZEN_VERSUCHE = 20
ERSETZEN_TAKT_S = 0.05
ERSETZEN_GEDULDIG_S = 10.0
# Vorsorglich: os.open ohne O_BINARY öffnet unter Windows im Textmodus der C-Laufzeit (über os.fdopen liest Python
# trotzdem binär; das Flag hält das unabhängig davon fest).
BINAER = getattr(os, "O_BINARY", 0)


def ist_windows() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------- Prozesse

def _kernel32():
    """kernel32 mit Signaturen: Ohne restype schnitte ctypes ein HANDLE (64 Bit) auf int ab."""
    import ctypes  # noqa: PLC0415 (nur unter Windows gebraucht)
    from ctypes import wintypes  # noqa: PLC0415
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _letzter_fehler() -> int:
    import ctypes  # noqa: PLC0415
    return ctypes.get_last_error()


def _startzeit(kernel, handle) -> float | None:
    """Zeitpunkt, zu dem der Prozess angelegt wurde (Unix-Sekunden), oder None."""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415
    zeiten = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(z) for z in zeiten)):
        return None
    angelegt = (zeiten[0].dwHighDateTime << 32) | zeiten[0].dwLowDateTime
    return (angelegt - FILETIME_1970) / 1e7


def _lebt_windows(pid: int) -> bool:
    try:
        kernel = _kernel32()
    except (ImportError, OSError, AttributeError):
        return True  # unbekannt heißt nie „weg“
    handle = kernel.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Zugriff verweigert: den Prozess gibt es, er gehört einem anderen Nutzer. Ungültiger Parameter: keine pid.
        return _letzter_fehler() != ERROR_INVALID_PARAMETER
    try:
        # Signalisiert heißt beendet; anders als der Exit-Code 259 (STILL_ACTIVE) ist das eindeutig.
        return kernel.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
    finally:
        kernel.CloseHandle(handle)


def lebt(pid: object) -> bool:
    """Läuft ein Prozess mit dieser pid (auch eines anderen Nutzers)? Sendet unter Windows nie ein Signal."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if ist_windows():
        return _lebt_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # gibt es, gehört aber einem anderen Nutzer
    return True


def _powershell() -> str:
    wurzel = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    pfad = Path(wurzel) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(pfad) if pfad.is_file() else "powershell"


ZEICHENCODES = re.compile(r"\d{1,5}(?:,\d{1,5})*")


def befehlszeile_skript(pid: int) -> str:
    """PowerShell-Skript für Win32_Process.CommandLine, die Befehlszeile als UTF-16-Einheiten in Dezimalzahlen: nur
    Kerntypen (string, char, int), damit es auch im Constrained Language Mode läuft (dort sind .NET-Methoden wie
    [Convert]::ToBase64String gesperrt), und reines ASCII, damit Codepage und BOM der Ausgabe nichts ändern."""
    return (f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}'; "
            "if (-not $p) { 'X' } elseif (-not $p.CommandLine) { 'P:' } "
            "else { 'P:' + (($p.CommandLine.ToCharArray() | ForEach-Object { [int]$_ }) -join ',') }")


def befehlszeile_deuten(ausgabe: str) -> str | None:
    """Antwort von befehlszeile_skript: "" (keine pid), die Befehlszeile oder None (nicht eindeutig)."""
    ausgabe = ausgabe.lstrip("\ufeff").strip()
    if ausgabe == "X":
        return ""
    codes = ausgabe[2:].strip() if ausgabe.startswith("P:") else ""
    if not ZEICHENCODES.fullmatch(codes):
        # Ohne Befehlszeile (Prozess eines anderen Nutzers, Systemprozess) oder kaputt: unbekannt, nicht „weg“.
        return None
    einheiten = [int(c) for c in codes.split(",")]
    if any(e > 0xFFFF for e in einheiten):
        return None
    try:
        return b"".join(e.to_bytes(2, "little") for e in einheiten).decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def befehlszeile(pid: int) -> str | None:
    """Die Befehlszeile des Prozesses; "" gibt es diese pid nicht; None: unbekannt (das Werkzeug ließ sich nicht
    starten, lief zu lange, meldete einen Fehler oder die Antwort ist nicht eindeutig lesbar).
    macOS/Linux: ps. Windows: Win32_Process über PowerShell (befehlszeile_skript), ohne Fenster."""
    if ist_windows():
        befehl = [_powershell(), "-NoProfile", "-NonInteractive", "-Command", befehlszeile_skript(pid)]
        extra = {"creationflags": CREATE_NO_WINDOW}
    else:
        befehl = ["ps", "-p", str(pid), "-o", "command="]
        extra = {}
    try:
        lauf = subprocess.run(befehl, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=BEFEHLSZEILE_WARTEN_S, **extra)
    except (OSError, subprocess.SubprocessError):
        return None
    if ist_windows():
        return befehlszeile_deuten(lauf.stdout) if lauf.returncode == 0 else None
    if lauf.returncode != 0:
        # Ohne Ausgabe: diese pid gibt es nicht (mehr). Mit Fehlermeldung: ps selbst ist gescheitert.
        return None if lauf.stderr.strip() else ""
    return lauf.stdout.strip()


def _beende_windows(pid: int, gestartet: float | None) -> None:
    """Ein Handle für Prüfung und Beenden: Hat Windows die pid inzwischen einem neueren Prozess gegeben (angelegt nach
    „gestartet“ der Zustand-Datei), bleibt er unberührt."""
    try:
        kernel = _kernel32()
    except (ImportError, OSError, AttributeError):
        return
    handle = kernel.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return
    try:
        if gestartet is not None:
            angelegt = _startzeit(kernel, handle)
            if angelegt is None or angelegt > gestartet + STARTZEIT_SPIEL_S:
                return
        kernel.TerminateProcess(handle, 1)
    finally:
        kernel.CloseHandle(handle)


def beende(pid: int, gestartet: float | None = None) -> None:
    """Beendet einen Prozess, dessen Befehlszeile vorher geprüft wurde. Windows: TerminateProcess auf dem Handle, über
    das auch die Startzeit geprüft wird; der Server räumt dann nicht selbst auf, das Stoppen entfernt seine
    Zustand-Datei."""
    if ist_windows():
        _beende_windows(pid, gestartet)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def ernte(pid: int) -> None:
    """Holt ein beendetes Kind ab (POSIX: sonst bleibt es als Zombie und gilt als lebend). Windows: nichts zu tun."""
    if ist_windows():
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def starte_abgeloest(befehl: list[str], **optionen) -> subprocess.Popen:
    """Startet einen Prozess, der den Aufruf überlebt. POSIX: eigene Sitzung. Windows: eigene, unsichtbare Konsole
    (CREATE_NO_WINDOW; die Kinder wie git und der Scanner erben sie, statt je ein Fenster aufzumachen), eigene
    Prozessgruppe und, wenn der Job des Aufrufers es erlaubt, außerhalb dieses Jobs (sonst endet der Server mit dem
    Bash-Aufruf von Claude Code); erlaubt der Job das nicht, ein zweiter Versuch ohne."""
    if not ist_windows():
        return subprocess.Popen(befehl, start_new_session=True, **optionen)
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(befehl, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **optionen)
    except OSError:
        return subprocess.Popen(befehl, creationflags=flags, **optionen)


# ---------------------------------------------------------------- Dateien

def ersetze(quelle: str | Path, ziel: str | Path, geduldig: bool = False) -> None:
    """os.replace; unter Windows mit Wiederholungen, solange ein anderes Programm (Virenscanner, Indexer, Editor) das
    Ziel gerade offen hat (PermissionError): knapp 1 s, mit geduldig (ein ganzer Ordner wie die frische Umgebung,
    den der Virenscanner durchsucht) bis ERSETZEN_GEDULDIG_S mit wachsendem Abstand. Danach der letzte Fehler."""
    if not ist_windows():
        os.replace(quelle, ziel)
        return
    ende = time.monotonic() + ERSETZEN_GEDULDIG_S
    takt = ERSETZEN_TAKT_S
    versuch = 0
    while True:
        versuch += 1
        try:
            os.replace(quelle, ziel)
            return
        except PermissionError:
            if (versuch >= ERSETZEN_VERSUCHE and not geduldig) or (geduldig and time.monotonic() >= ende):
                raise
        time.sleep(takt)
        if geduldig:
            takt = min(takt * 1.5, 1.0)


def msys_zurueck(argumente: list[str], wurzel: str | None = None) -> list[str]:
    """Nimmt zurück, was Git Bash (MSYS) beim Start von python.exe aus einem Argument macht, das mit ~/ beginnt:
    '~/mein kit' kommt als '~C:/Program Files/Git/mein kit' an. Nur am Anfang des Arguments, auch hinter einem
    Anführungszeichen ('"~/mein kit"', im CI belegt), nur in dieser Form (mit '/', wie cygpath -m und MSYS sie
    schreiben). bin/atlas-kit gibt die Wurzel von MSYS in ATLAS_MSYS_WURZEL mit (cygpath -m /); ohne sie bleibt alles,
    wie es ist."""
    wurzel = os.environ.get("ATLAS_MSYS_WURZEL", "") if wurzel is None else wurzel
    vorn = "~" + wurzel.rstrip("/") + "/"
    if len(vorn) <= 2:
        return list(argumente)
    ergebnis = []
    for a in argumente:
        zeichen = a[:1] if a[:1] in "\"'" else ""
        rest = a[len(zeichen):]
        ergebnis.append(zeichen + "~/" + rest[len(vorn):] if rest.startswith(vorn) else a)
    return ergebnis


def vom_start(zustand: dict, pid: int) -> bool:
    """Schrieb der eben gestartete Prozess (pid) diese Zustand-Datei? Unter Windows startet Scripts\\python.exe einer
    venv das echte Python als Kind; dann ist der Gestartete dessen Elternprozess (WS-PW3). Gilt für die Werkstatt und
    den Startseiten-Server."""
    def zahl(wert: object) -> bool:
        return isinstance(wert, int) and not isinstance(wert, bool)
    return any(zahl(zustand.get(feld)) and zustand[feld] == pid for feld in ("pid", "eltern"))


# Eine Junction (Ordner-Verweis unter Windows, ohne Adminrechte anlegbar) ist für is_symlink() kein Symlink.
JUNCTION = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def ist_verweis_info(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == JUNCTION


def ist_verweis(pfad: str | Path) -> bool:
    """Symlink oder (Windows) Junction, ohne ihm zu folgen; was sich nicht lesen lässt, ist keiner (wie is_symlink)."""
    try:
        return ist_verweis_info(os.lstat(pfad))
    except (OSError, ValueError):
        return False


def ist_verweis_eintrag(eintrag: os.DirEntry) -> bool:
    """Wie ist_verweis für einen Eintrag aus os.scandir (DirEntry.is_symlink() kennt keine Junction)."""
    if eintrag.is_symlink():
        return True
    try:
        return ist_verweis_info(eintrag.stat(follow_symlinks=False))
    except OSError:
        return False


def python_in(umgebung: Path) -> Path:
    """Das Python einer venv: Scripts\\python.exe unter Windows, sonst bin/python."""
    if ist_windows() or sys.platform.startswith("win"):
        return umgebung / "Scripts" / "python.exe"
    return umgebung / "bin" / "python"


def hook_pfad(pfad: PurePath) -> str:
    return pfad.as_posix()
