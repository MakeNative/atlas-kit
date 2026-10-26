"""Plattform-Schichten der Start-App (specs/start-app.yaml SA-P, SA-F1, SA-A2, SA-T): macOS und Windows.

Der Kern (start.py, start_server.py) kennt nur diese Schnittstelle: Fenster öffnen, Ordner wählen, Terminal-Fenster mit
Claude starten, claude finden, das Programm anlegen. Jeder Aufruf geht mit Argumentliste über `lauf` (in Tests ersetzt),
nie über eine Shell. Nur Standardbibliothek und was das System mitbringt (open, osascript, Terminal; PowerShell, cmd,
Edge, Windows Terminal).
"""
from __future__ import annotations

import json
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import browser
import ergebnis

PROGRAMM = "Atlas Werkstatt"
KENNUNG = "io.github.makenative.atlas-werkstatt"
AUSFUEHRBAR = "atlas-werkstatt"
SITZUNG_PREFIX = "sitzung-"
SITZUNG_ALT_S = 60 * 60
WAEHLEN_HOECHSTENS_S = 30 * 60
LAUF_HOECHSTENS_S = 60
MAC, WINDOWS, ANDERE = "mac", "windows", "andere"
ORT_KIT = "Der Ordner des Kits ist nicht erreichbar. Kopier das Kit auf ein Laufwerk dieses Rechners."
ORT_ARBEIT = "Der Arbeitsordner ist nicht erreichbar. Nimm einen Arbeitsordner auf einem Laufwerk dieses Rechners."
WINDOWS_ARGUMENTE_HOECHSTENS = 6

Lauf = Callable[..., subprocess.CompletedProcess]


def _lauf(befehl: list[str], *, timeout: float = LAUF_HOECHSTENS_S, env: dict | None = None,
          ohne_ausgabe: bool = False, **zusatz) -> subprocess.CompletedProcess:
    """ohne_ausgabe: keine Pipes. Für Starter wie `cmd /c start`: Das gestartete Programm (Edge) erbt sonst die Pipes,
    und subprocess.run wartet, bis es endet (unter Windows im CI gesehen, Ticket 23)."""
    if ohne_ausgabe:
        zusatz.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        zusatz.update(capture_output=True)
    try:
        return subprocess.run(befehl, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env, **zusatz)
    except (OSError, subprocess.SubprocessError) as fehler:
        return subprocess.CompletedProcess(befehl, 127, "", str(fehler))


def _konsole(befehlszeile: str, env: dict) -> bool:
    """Windows: startet befehlszeile wörtlich in einer neuen Konsole, ohne zu warten."""
    try:
        subprocess.Popen(befehlszeile, env=env, stdin=subprocess.DEVNULL, close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    except (OSError, ValueError):
        return False
    return True


def name() -> str:
    if sys.platform == "darwin":
        return MAC
    if sys.platform.startswith("win") or os.name == "nt":
        return WINDOWS
    return ANDERE


class ProgrammFremd(Exception):
    """Am Ziel liegt ein anderes Programm gleichen Namens."""

    def __init__(self, pfad: Path):
        super().__init__(str(pfad))
        self.pfad = pfad


def sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


PS_EINFACHE_ANFUEHRUNG = "'\u2018\u2019\u201a\u201b"


def ps_quote(text: str) -> str:
    """PowerShell behandelt auch ‘ ’ ‚ ‛ als einfaches Anführungszeichen; jedes wird verdoppelt (r22-9)."""
    return "'" + "".join(z * 2 if z in PS_EINFACHE_ANFUEHRUNG else z for z in text) + "'"


def cmd_quote(text: str) -> str:
    """Für eine .cmd-Datei: in Anführungszeichen, % verdoppelt (Windows-Pfade enthalten kein ")."""
    return '"' + text.replace('"', "").replace("%", "%%") + '"'


def befehlszeile(ordner: Path, claude: str, argumente: list[str]) -> str:
    return " ".join(["cd", sh_quote(str(ordner)), "&&", sh_quote(claude), *(sh_quote(a) for a in argumente)])


def raeume_sitzungen(daten: Path, jetzt: float | None = None) -> None:
    """SA-T1: alte Sitzungsdateien (älter als 1 Stunde) entfernen."""
    jetzt = time.time() if jetzt is None else jetzt
    for datei in daten.glob(f"{SITZUNG_PREFIX}*") if daten.is_dir() else []:
        try:
            if jetzt - datei.lstat().st_mtime > SITZUNG_ALT_S:
                datei.unlink()
        except OSError:
            pass


def _claude_in(orte: list[Path], namen: tuple[str, ...]) -> str | None:
    for ort in orte:
        for n in namen:
            kandidat = ort / n
            if kandidat.is_file() and os.access(kandidat, os.X_OK):
                return str(kandidat)
    return None


def _versionsschluessel(ordner: Path) -> tuple:
    teile = ordner.parent.name.lstrip("v").split(".")
    return tuple(int(t) if t.isdigit() else -1 for t in teile)


@dataclass
class Installiert:
    """Wo Claude Code das Plugin führt: installed_plugins.json und der Schlüssel <name>@<marktplatz> (r22-3)."""
    konfiguration: Path
    schluessel: str
    scope: str = "user"
    projekt: str = ""  # projectPath bei scope project/local


def installiert_als(plugin: Path, home: Path, env: dict | None = None) -> Installiert | None:
    """Der Eintrag in installed_plugins.json, dessen installPath dieser Plugin-Ordner ist; None bei einer Fassung, die
    nicht über /plugin installiert ist (etwa beim Entwickeln)."""
    env = os.environ if env is None else env
    datei = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "plugins" / "installed_plugins.json"
    try:
        daten = json.loads(datei.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    plugins = daten.get("plugins") if isinstance(daten, dict) else None
    for schluessel, eintraege in (plugins or {}).items() if isinstance(plugins, dict) else []:
        for eintrag in eintraege if isinstance(eintraege, list) else []:
            pfad = eintrag.get("installPath") if isinstance(eintrag, dict) else None
            if isinstance(pfad, str) and _gleich(Path(pfad), plugin):
                scope = eintrag.get("scope") if isinstance(eintrag.get("scope"), str) else "user"
                projekt = eintrag.get("projectPath") if isinstance(eintrag.get("projectPath"), str) else ""
                return Installiert(datei, schluessel, scope, projekt)
    return None


def _gleich(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class Plattform:
    name = ANDERE

    def __init__(self, lauf: Lauf = _lauf, home: Path | None = None, env: dict | None = None):
        self.lauf = lauf
        self.konsole = getattr(lauf, "konsole", _konsole)  # eine Attrappe für lauf bringt in Tests ihre Konsole mit
        self.home = home if home is not None else Path(os.path.expanduser("~"))
        self.env = dict(os.environ if env is None else env)

    # Schnittstelle; ANDERE kann nichts davon.
    def fenster(self, datei: Path) -> bool:
        return browser.oeffne(datei) is not None

    def waehle_ordner(self, titel: str, start: Path) -> str | None:
        raise NichtMoeglich()

    def terminal(self, ordner: Path, claude: str, argumente: list[str], daten: Path, ort: str = ORT_KIT) -> bool:
        return False

    def claude(self) -> str | None:
        return shutil.which("claude", path=self.env.get("PATH"))

    def programm_anlegen(self, plugin: Path, daten: Path | None, version: str, desktop: bool,
                         installiert: Installiert | None = None) -> list[Path]:
        raise NichtMoeglich()

    def _sitzungsdatei(self, daten: Path, endung: str, inhalt: str, kodierung: str = "utf-8") -> Path:
        daten.mkdir(parents=True, exist_ok=True)
        datei = daten / f"{SITZUNG_PREFIX}{secrets.token_hex(6)}{endung}"
        ergebnis.schreibe_atomar(datei, inhalt.encode(kodierung), modus=0o700)
        return datei

    def _starte_sitzung(self, datei: Path, befehle: list[list[str]], env: dict | None = None) -> bool:
        """Versucht die Befehle der Reihe nach; geht keiner, fällt die Sitzungsdatei gleich weg (r22-7)."""
        for befehl in befehle:
            if (self.lauf(befehl, env=env) if env is not None else self.lauf(befehl)).returncode == 0:
                return True
        try:
            datei.unlink()
        except OSError:
            pass
        return False


class NichtMoeglich(Exception):
    pass


class Mac(Plattform):
    name = MAC
    CHROME = "Google Chrome"
    WAEHLEN = (
        "on run argv",
        "tell me to activate",
        "set ort to POSIX file (item 2 of argv)",
        "try",
        "set f to choose folder with prompt (item 1 of argv) default location ort",
        "on error number -128",
        'return ""',
        "end try",
        "return POSIX path of f",
        "end run",
    )

    def fenster(self, datei: Path) -> bool:
        """SA-F1: Chrome als Fenster ohne Adressleiste, wenn vorhanden; sonst der Standardbrowser."""
        if self.lauf(["open", "-Ra", self.CHROME]).returncode == 0:
            start = self.lauf(["open", "-na", self.CHROME, "--args", f"--app={datei.as_uri()}"])
            if start.returncode == 0:
                return True
        return browser.oeffne(datei) == browser.SICHER

    def waehle_ordner(self, titel: str, start: Path) -> str | None:
        if not start.is_dir():
            start = self.home
        befehl = ["osascript"]
        for zeile in self.WAEHLEN:
            befehl += ["-e", zeile]
        lauf = self.lauf([*befehl, titel, str(start)], timeout=WAEHLEN_HOECHSTENS_S)
        pfad = lauf.stdout.strip()
        if lauf.returncode != 0 or not pfad:
            return None
        return pfad.rstrip("/") or "/"

    def terminal(self, ordner: Path, claude: str, argumente: list[str], daten: Path, ort: str = ORT_KIT) -> bool:
        inhalt = ("#!/bin/sh\n"
                  'rm -f -- "$0"\n'
                  f"cd -- {sh_quote(str(ordner))} || exit 1\n"
                  f"exec {' '.join(sh_quote(a) for a in [claude, *argumente])}\n")
        datei = self._sitzungsdatei(daten, ".command", inhalt)
        return self._starte_sitzung(datei, [["open", "-a", "Terminal", str(datei)]])

    def claude(self) -> str | None:
        gefunden = super().claude()
        if gefunden:
            return gefunden
        orte = [self.home / ".claude" / "local", self.home / ".local" / "bin", self.home / ".npm-global" / "bin",
                self.home / ".volta" / "bin", self.home / ".bun" / "bin"]
        # nvm: die neueste Node-Fassung zuerst (r22-5)
        orte += sorted(self.home.glob(".nvm/versions/node/*/bin"), key=_versionsschluessel, reverse=True)
        return _claude_in(orte, ("claude",))

    def programm_anlegen(self, plugin: Path, daten: Path | None, version: str, desktop: bool,
                         installiert: Installiert | None = None) -> list[Path]:
        ordner = self.home / "Applications"
        ziel = ordner / f"{PROGRAMM}.app"
        if ziel.exists() or ziel.is_symlink():
            if not _ist_eigenes_programm(ziel):
                raise ProgrammFremd(ziel)
        ordner.mkdir(parents=True, exist_ok=True)
        neu = ordner / f".{PROGRAMM}.app.neu-{os.getpid()}"
        shutil.rmtree(neu, ignore_errors=True)
        (neu / "Contents" / "MacOS").mkdir(parents=True)
        info = {
            "CFBundleName": PROGRAMM,
            "CFBundleDisplayName": PROGRAMM,
            "CFBundleIdentifier": KENNUNG,
            "CFBundlePackageType": "APPL",
            "CFBundleExecutable": AUSFUEHRBAR,
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "CFBundleInfoDictionaryVersion": "6.0",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        }
        (neu / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
        skript = neu / "Contents" / "MacOS" / AUSFUEHRBAR
        skript.write_text(mac_startskript(plugin, daten, installiert), encoding="utf-8")
        skript.chmod(0o755)
        if ziel.exists() or ziel.is_symlink():
            if ziel.is_symlink() or not ziel.is_dir():
                ziel.unlink()
            else:
                shutil.rmtree(ziel)
        os.replace(neu, ziel)
        return [ziel]


def _ist_eigenes_programm(ziel: Path) -> bool:
    if ziel.is_symlink() or not ziel.is_dir():
        return False
    try:
        with open(ziel / "Contents" / "Info.plist", "rb") as datei:
            info = plistlib.load(datei)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    return isinstance(info, dict) and info.get("CFBundleIdentifier") == KENNUNG


def fehlt_satz(plugin: str) -> str:
    return (f"Atlas Werkstatt findet atlas-kit nicht mehr an {plugin}. Das passiert, wenn das Plugin aktualisiert oder "
            f"verschoben wurde. Öffne Claude Code und ruf /atlas:app auf, dann ist Atlas Werkstatt wieder aktuell.")


PYTHON_FEHLT = ("Atlas Werkstatt braucht Python 3.9 oder neuer. Installier es von https://www.python.org/downloads/ "
                "und öffne Atlas Werkstatt danach noch einmal.")


NICHT_INSTALLIERT = ("Atlas Werkstatt findet atlas-kit in Claude Code nicht mehr. Ist das Plugin noch installiert? Öffne "
                     "Claude Code, installier atlas-kit bei Bedarf mit /plugin neu und ruf dann /atlas:app auf.")

# JXA (osascript -l JavaScript, auf jedem Mac da): installPath des Eintrags argv[1] aus installed_plugins.json
# (argv[0]); bei mehreren der mit scope user, sonst der erste. Leer, wenn es ihn nicht gibt.
# JXA (osascript -l JavaScript, auf jedem Mac da): installPath des Eintrags argv[1] aus installed_plugins.json
# (argv[0]), bevorzugt mit scope argv[2] (und projectPath argv[3]), sonst scope user, sonst der erste. Leer, wenn es
# den Eintrag nicht gibt; "?", wenn sich die Datei nicht lesen oder parsen lässt (r22-11).
JXA_INSTALLIERT = (
    "function run(a){var d;try{var t=$.NSString.stringWithContentsOfFileEncodingError(a[0],$.NSUTF8StringEncoding,"
    "null);if(t.isNil())return \"?\";d=JSON.parse(ObjC.unwrap(t));}catch(f){return \"?\";}"
    "if(!d||typeof d!==\"object\")return \"?\";var p=d.plugins&&Object.prototype.hasOwnProperty.call(d.plugins,a[1])"
    "?d.plugins[a[1]]:null;var e=Array.isArray(p)?p:[];"
    "var g=e.filter(function(x){return x&&x.scope===a[2]&&(a[2]===\"user\"||!a[3]||x.projectPath===a[3])});"
    "var u=e.filter(function(x){return x&&x.scope===\"user\"});var w=(g.length?g:(u.length?u:e))[0];"
    "return (w&&typeof w.installPath===\"string\")?w.installPath:\"\";}"
)


def mac_startskript(plugin: Path, daten: Path | None, installiert: Installiert | None = None) -> str:
    """SA-P2, SA-P3: Plugin-Ort, Datenordner und (bei einem über /plugin installierten Paket) der Eintrag in
    installed_plugins.json dieses Rechners stehen fest darin. Den aktuellen Plugin-Ort liest das Skript zur Laufzeit
    aus installed_plugins.json, damit nach einem Update die neue Fassung startet (r22-3)."""
    zeilen = [
        "#!/bin/sh",
        "# Atlas Werkstatt: angelegt von /atlas:app (atlas-kit).",
        f"PLUGIN={sh_quote(str(plugin))}",
    ]
    if installiert is not None:
        zeilen += [f"KONFIGURATION={sh_quote(str(installiert.konfiguration))}",
                   f"EINTRAG={sh_quote(installiert.schluessel)}",
                   f"SCOPE={sh_quote(installiert.scope)}",
                   f"PROJEKT={sh_quote(installiert.projekt)}"]
    if daten is not None:
        zeilen += [f"CLAUDE_PLUGIN_DATA={sh_quote(str(daten))}", "export CLAUDE_PLUGIN_DATA"]
    zeilen += [
        # Bekannte Orte vor /usr/bin: aus dem Finder ist der PATH nur /usr/bin:/bin:/usr/sbin:/sbin (r22-5).
        'PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"',
        "export PATH",
        "melde() {",
        "  osascript -e 'on run argv' -e 'display dialog (item 1 of argv) with title \"Atlas Werkstatt\" "
        "buttons {\"OK\"} default button 1' -e 'end run' \"$1\" >/dev/null 2>&1",
        "}",
    ]
    if installiert is not None:
        zeilen += [
            f"ort=$(osascript -l JavaScript -e {sh_quote(JXA_INSTALLIERT)} \"$KONFIGURATION\" \"$EINTRAG\" "
            "\"$SCOPE\" \"$PROJEKT\" 2>/dev/null) || ort='?'",
            "# Nicht lesbar (Claude Code schreibt sie gerade, r22-11): beim festen Ort bleiben, den prüft SA-P2 unten.",
            'if [ "$ort" = "?" ]; then',
            '  ort=$PLUGIN',
            "fi",
            'if [ -z "$ort" ]; then',
            f'  melde {sh_quote(NICHT_INSTALLIERT)}',
            "  exit 1",
            "fi",
            'PLUGIN=$ort',
        ]
    zeilen += [
        'if [ ! -f "$PLUGIN/bin/atlas-kit" ] || [ ! -f "$PLUGIN/befehle/start_server.py" ] '
        '|| [ -e "$PLUGIN/.orphaned_at" ]; then',
        f'  melde "{fehlt_satz("$PLUGIN")}"',
        "  exit 1",
        "fi",
        'if ! ausgabe=$(sh "$PLUGIN/bin/atlas-kit" start 2>&1); then',
        '  melde "$ausgabe"',
        "  exit 1",
        "fi",
        "",
    ]
    return "\n".join(zeilen)


class Windows(Plattform):
    name = WINDOWS
    WAEHLEN = "\n".join((
        "Add-Type -AssemblyName System.Windows.Forms",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog",
        "$d.Description = $env:ATLAS_TITEL",
        "$d.SelectedPath = $env:ATLAS_START",
        "$d.ShowNewFolderButton = $true",
        "$vorn = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}",
        "if ($d.ShowDialog($vorn) -eq [System.Windows.Forms.DialogResult]::OK) { $d.SelectedPath }",
    ))
    VERKNUEPFEN = "\n".join((
        "$shell = New-Object -ComObject WScript.Shell",
        "$orte = @($env:ATLAS_STARTMENUE)",
        "if ($env:ATLAS_DESKTOP -eq '1') { $orte += [Environment]::GetFolderPath('Desktop') }",
        "foreach ($ort in $orte) {",
        "  New-Item -ItemType Directory -Force -Path $ort | Out-Null",
        "  $pfad = Join-Path $ort 'Atlas Werkstatt.lnk'",
        "  $v = $shell.CreateShortcut($pfad)",
        "  $v.TargetPath = $env:ATLAS_ZIEL",
        "  $v.Arguments = $env:ATLAS_ARGUMENTE",
        "  $v.WorkingDirectory = $env:ATLAS_ORDNER",
        "  $v.Description = 'Atlas Werkstatt'",
        "  $v.Save()",
        "  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "  Write-Output $pfad",
        "}",
    ))

    # Neue Konsole ohne äußeres cmd /c start: auch der Pfad der Sitzungsdatei steht nicht auf der Befehlszeile, sondern
    # in ATLAS_SITZUNG (r22-12; ein & oder ^ im Benutzernamen). /d ohne AutoRun, /v:off ohne verzögerte Auswertung von
    # ! (r22-13). Die doppelten Anführungszeichen braucht die Regel von cmd /k zum Entfernen der äußeren.
    SITZUNG_START = 'cmd.exe /d /v:off /k ""%ATLAS_SITZUNG%""'

    def _powershell(self) -> str:
        wurzel = self.env.get("SystemRoot") or self.env.get("SYSTEMROOT")
        if wurzel:
            kandidat = Path(wurzel) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if kandidat.is_file():
                return str(kandidat)
        return "powershell.exe"

    def _mit(self, **werte: str) -> dict:
        return {**self.env, **werte}

    def fenster(self, datei: Path) -> bool:
        """SA-F1: Edge als Fenster ohne Adressleiste; sonst der Standardbrowser."""
        start = self.lauf(["cmd", "/c", "start", "", "msedge", f"--app={datei.as_uri()}"], ohne_ausgabe=True)
        if start.returncode == 0:
            return True
        # Nur ein sicher geöffneter Browser zählt; die Dateizuordnung für .html ist oft ein Editor (r22-8).
        return browser.oeffne(datei) == browser.SICHER

    def waehle_ordner(self, titel: str, start: Path) -> str | None:
        if not start.is_dir():
            start = self.home
        lauf = self.lauf([self._powershell(), "-NoProfile", "-STA", "-Command", self.WAEHLEN],
                         timeout=WAEHLEN_HOECHSTENS_S, env=self._mit(ATLAS_TITEL=titel, ATLAS_START=str(start)))
        # [Console]::OutputEncoding = UTF8 schreibt unter PowerShell 5.1 ein BOM vor die Ausgabe (r23-302).
        pfad = lauf.stdout.lstrip("\ufeff").strip()
        return pfad if lauf.returncode == 0 and pfad else None

    @staticmethod
    def sitzung_cmd(anzahl: int, ort: str = ORT_KIT) -> str:
        platzhalter = " ".join(f'"%ATLAS_ARG_{k}%"' for k in range(1, anzahl + 1))
        return "\r\n".join((
            "@echo off",
            "rem Sitzung von Atlas Werkstatt. Ordner, claude und Argumente kommen aus der Umgebung (ATLAS_ORDNER,",
            "rem ATLAS_CLAUDE, ATLAS_ARG_1 ...), nie aus diesem Text: so wertet cmd ein Prozent oder Dach darin genau",
            "rem einmal aus. Anfuehrungszeichen in den Werten lehnt der Server vorher ab.",
            "chcp 65001 >nul",
            'pushd "%ATLAS_ORDNER%"',
            "if errorlevel 1 goto ordner",
            "rem Eine Zeile: cmd hat sie ganz gelesen, bevor die Datei sich loescht; danach startet claude.",
            f'(goto) 2>nul & del "%~f0" & "%ATLAS_CLAUDE%" {platzhalter}'.rstrip(),
            ":ordner",
            f"echo {ort}",
            "pause",
            '(goto) 2>nul & del "%~f0"',
            "",
        ))

    def terminal(self, ordner: Path, claude: str, argumente: list[str], daten: Path, ort: str = ORT_KIT) -> bool:
        """SA-T1: Kein Ordnerpfad auf einer Befehlszeile (r22-2: wt trennt an ';') und keine Werte im Text der
        Sitzungsdatei: Ordner, claude und die Argumente (LA-S6: ATLAS_ARG_1 … ATLAS_ARG_n, n höchstens 6) gehen als
        Umgebungsvariablen mit, die cmd start an das neue Fenster vererbt. So wertet cmd ein % oder ^ darin genau
        einmal aus und braucht kein call (r21-13). Ein " in einem Wert schlösse die Anführungszeichen um den
        Platzhalter und ließe cmd ein & dahinter als zweiten Befehl ausführen (code-1): solche Werte lehnt terminal()
        ab. Backslashes am Ende eines Arguments stehen in der .cmd direkt vor dem schließenden ": claude liest seine
        Befehlszeile nach den Regeln der C-Laufzeit, dort hieße \" ein Anführungszeichen; deshalb sind sie verdoppelt
        (code-9). Die Datei löscht sich selbst, bevor claude startet; gestartet wird mit cmd start im Standard-Terminal."""
        if len(argumente) > WINDOWS_ARGUMENTE_HOECHSTENS:
            raise ValueError(f"höchstens {WINDOWS_ARGUMENTE_HOECHSTENS} Argumente")
        if any('"' in wert for wert in (str(ordner), claude, *argumente)):
            raise ValueError('ein " in einem Wert der Sitzungsdatei')
        datei = self._sitzungsdatei(daten, ".cmd", self.sitzung_cmd(len(argumente), ort), "ascii")
        werte = {f"ATLAS_ARG_{k}": wert + "\\" * (len(wert) - len(wert.rstrip("\\")))
                 for k, wert in enumerate(argumente, start=1)}
        env = self._mit(ATLAS_ORDNER=str(ordner), ATLAS_CLAUDE=claude, ATLAS_SITZUNG=str(datei), **werte)
        if self.konsole(self.SITZUNG_START, env):
            return True
        try:
            datei.unlink()
        except OSError:
            pass
        return False

    def claude(self) -> str | None:
        gefunden = super().claude()
        if gefunden:
            return gefunden
        orte = [self.home / ".local" / "bin"]
        if self.env.get("APPDATA"):
            orte.append(Path(self.env["APPDATA"]) / "npm")
        return _claude_in(orte, ("claude.exe", "claude.cmd"))

    def startmenue(self) -> Path:
        appdata = self.env.get("APPDATA") or str(self.home / "AppData" / "Roaming")
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"

    def programm_anlegen(self, plugin: Path, daten: Path | None, version: str, desktop: bool,
                         installiert: Installiert | None = None) -> list[Path]:
        if daten is None:
            raise NichtMoeglich()
        daten.mkdir(parents=True, exist_ok=True)
        skript = daten / "atlas-werkstatt.ps1"
        # Die Werte stehen nicht im Skripttext, sondern in einer JSON-Datei daneben (r22-9).
        werte = {"plugin": str(plugin), "daten": str(daten),
                 "konfiguration": str(installiert.konfiguration) if installiert else None,
                 "eintrag": installiert.schluessel if installiert else None,
                 "scope": installiert.scope if installiert else None,
                 "projekt": installiert.projekt if installiert else None}
        ergebnis.schreibe_atomar(daten / WINDOWS_WERTE, json.dumps(werte, ensure_ascii=False, indent=2).encode("utf-8"))
        # PowerShell 5.1 liest UTF-8 nur mit BOM richtig.
        ergebnis.schreibe_atomar(skript, windows_startskript().encode("utf-8-sig"))
        argumente = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{skript}"'
        lauf = self.lauf([self._powershell(), "-NoProfile", "-Command", self.VERKNUEPFEN],
                         env=self._mit(ATLAS_STARTMENUE=str(self.startmenue()), ATLAS_DESKTOP="1" if desktop else "0",
                                       ATLAS_ZIEL=self._powershell(), ATLAS_ARGUMENTE=argumente,
                                       ATLAS_ORDNER=str(daten)))
        if lauf.returncode != 0:
            raise NichtMoeglich(lauf.stderr.strip().splitlines()[-1] if lauf.stderr.strip() else "")
        # [Console]::OutputEncoding = UTF8 schreibt unter PowerShell 5.1 ein BOM vor die erste Zeile.
        return [Path(z.strip()) for z in lauf.stdout.lstrip("\ufeff").splitlines() if z.strip()]


WINDOWS_WERTE = "atlas-werkstatt.json"


def windows_startskript() -> str:
    """SA-P2, SA-P3 für Windows: PowerShell-Skript im Datenordner, Meldungen als MessageBox. Plugin-Ort, Datenordner
    und Eintrag liest es aus atlas-werkstatt.json daneben; der aktuelle Plugin-Ort kommt zur Laufzeit aus
    installed_plugins.json (r22-3)."""
    return "\r\n".join((
        "# Atlas Werkstatt: angelegt von /atlas:app (atlas-kit).",
        "Add-Type -AssemblyName System.Windows.Forms",
        "# Ticket 23: Python schreibt UTF-8 (-X utf8); ohne diese Zeile läse PowerShell die Ausgabe in der OEM-Codepage.",
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false",
        "# ATLAS_START_MELDUNG (nur für die Prüfung unter Windows): Meldung in diese Datei statt in ein Fenster.",
        "function Melde($text) {",
        "  if ($env:ATLAS_START_MELDUNG) { [System.IO.File]::AppendAllText($env:ATLAS_START_MELDUNG, \"$text`n\", (New-Object System.Text.UTF8Encoding $false)); return }",
        "  [System.Windows.Forms.MessageBox]::Show($text, 'Atlas Werkstatt') | Out-Null",
        "}",
        f"$werte = Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot '{WINDOWS_WERTE}') | ConvertFrom-Json",
        "$plugin = $werte.plugin",
        "$env:CLAUDE_PLUGIN_DATA = $werte.daten",
        "if ($werte.eintrag) {",
        "  $ort = $null",
        "  $daten = $null",
        "  # Nicht lesbar (Claude Code schreibt sie gerade, r22-11): beim festen Ort bleiben, den prüft SA-P2 unten.",
        "  try { $daten = Get-Content -Raw -Encoding UTF8 -ErrorAction Stop $werte.konfiguration | ConvertFrom-Json } catch { $daten = $null }",
        "  if ($null -eq $daten) { $ort = $plugin } else {",
        "    $eigenschaft = $null",
        "    if ($daten.plugins) { $eigenschaft = $daten.plugins.PSObject.Properties[$werte.eintrag] }",
        "    $liste = @(if ($eigenschaft) { $eigenschaft.Value })",
        "    $gleich = @($liste | Where-Object { $_.scope -eq $werte.scope -and ($werte.scope -eq 'user' -or -not $werte.projekt -or $_.projectPath -eq $werte.projekt) })",
        "    $nutzer = @($liste | Where-Object { $_.scope -eq 'user' })",
        "    if ($gleich.Count -gt 0) { $ort = $gleich[0].installPath } elseif ($nutzer.Count -gt 0) { $ort = $nutzer[0].installPath } elseif ($liste.Count -gt 0) { $ort = $liste[0].installPath }",
        "  }",
        f"  if (-not $ort) {{ Melde {ps_quote(NICHT_INSTALLIERT)}; exit 1 }}",
        "  $plugin = $ort",
        "}",
        "if (-not (Test-Path (Join-Path $plugin 'bin\\atlas-kit')) -or "
        "-not (Test-Path (Join-Path $plugin 'befehle\\start_server.py')) -or "
        "(Test-Path (Join-Path $plugin '.orphaned_at'))) {",
        f"  Melde ({ps_quote(fehlt_satz('{0}'))} -f $plugin)",
        "  exit 1",
        "}",
        "# Wie bin/atlas-kit (AK-W2): ein Kandidat zählt nur, wenn er läuft und 3.9 oder neuer meldet; der Platzhalter",
        "# des Microsoft Store (python3/python ohne installiertes Python) meldet keine Version.",
        "$py = $null; $vorn = @()",
        "# Ohne doppelte Anführungszeichen im Argument: PowerShell 5.1 gibt sie an native Programme nicht weiter.",
        "foreach ($k in @('py -3', 'python3', 'python')) {",
        "  $teile = $k -split ' '",
        "  if (-not (Get-Command $teile[0] -ErrorAction SilentlyContinue)) { continue }",
        "  $a = @($teile | Select-Object -Skip 1)",
        "  $v = (& $teile[0] @a -c 'import sys; print(''%d.%d'' % sys.version_info[:2])' 2>$null | Out-String).Trim()",
        "  if ($LASTEXITCODE -eq 0 -and $v -match '^3\\.(9|[1-9][0-9])$') { $py = $teile[0]; $vorn = $a; break }",
        "}",
        f"if (-not $py) {{ Melde {ps_quote(PYTHON_FEHLT)}; exit 1 }}",
        "$ausgabe = & $py @vorn -X utf8 -B -E -s (Join-Path $plugin 'befehle\\atlas_kit.py') start 2>&1 | Out-String",
        "if ($LASTEXITCODE -ne 0) { Melde $ausgabe; exit 1 }",
        "",
    ))


def aktuelle(lauf: Lauf = _lauf, home: Path | None = None, env: dict | None = None) -> Plattform:
    art = {MAC: Mac, WINDOWS: Windows}.get(name(), Plattform)
    return art(lauf, home, env)
