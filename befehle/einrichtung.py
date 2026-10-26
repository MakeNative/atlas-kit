from __future__ import annotations

import errno
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import plattform
import texte
from finden import tilde

UMGEBUNG = "umgebung"
SPERRE = "umgebung.sperre"
DATEN_OHNE_CLAUDE = ".umgebung"
WARTEN_HOECHSTENS_S = 150
WARTEN_TAKT_S = 0.5
VERWAIST_NACH_S = 600
UNLESBAR_VERWAIST_NACH_S = 10
LAUFZEIT_HOECHSTENS_S = 300
PIP_ZEILEN_HOECHSTENS = 20
NETZ_ZEICHEN = (
    "connection error", "NewConnectionError", "Failed to establish a new connection", "Connection refused", "Max retries exceeded",
    "Temporary failure in name resolution", "nodename nor servname", "Name or service not known",
    "Network is unreachable", "ConnectTimeoutError", "Read timed out", "ProxyError", "SSLError",
)
KEIN_SCHREIBRECHT = (errno.EACCES, errno.EPERM, errno.EROFS)
VENV_FEHLT_ZEICHEN = ("ensurepip", "No module named venv")


class EinrichtungFehler(Exception):
    def __init__(self, zeilen: list[str]):
        super().__init__("\n".join(zeilen))
        self.zeilen = zeilen


def datenordner(paket: Path, daten: Path | None) -> Path:
    return daten if daten is not None else paket / DATEN_OHNE_CLAUDE


def _python_in(umgebung: Path) -> Path:
    return plattform.python_in(umgebung)


def _laeuft(python: Path) -> bool:
    try:
        lauf = subprocess.run([str(python), "-B", "-E", "-s", "-c", "import yaml"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return lauf.returncode == 0


def _entferne(pfad: Path) -> None:
    if pfad.is_symlink() or pfad.is_file():
        pfad.unlink()
    elif pfad.is_dir():
        shutil.rmtree(pfad, ignore_errors=True)


def _gekuerzt(ausgabe: str) -> list[str]:
    zeilen = [z.rstrip() for z in ausgabe.splitlines() if z.strip()]
    return zeilen[-PIP_ZEILEN_HOECHSTENS:]


def _meldungen_von_pip(ausgabe: str) -> str:
    meldungen = [z for z in ausgabe.splitlines() if z.startswith(("ERROR:", "WARNING:"))]
    return "\n".join(meldungen) if meldungen else ausgabe


def _ausfuehren(befehl: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(befehl, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=LAUFZEIT_HOECHSTENS_S)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(befehl, 1, "", f"Abbruch nach {LAUFZEIT_HOECHSTENS_S} s")
    except OSError as fehler:
        return subprocess.CompletedProcess(befehl, 1, "", str(fehler))


def _lege_umgebung_an(neu: Path, requirements: Path) -> None:
    venv = _ausfuehren([sys.executable, "-m", "venv", str(neu)])
    if venv.returncode != 0:
        ausgabe = venv.stdout + venv.stderr
        if any(zeichen in ausgabe for zeichen in VENV_FEHLT_ZEICHEN):
            raise EinrichtungFehler([texte.KEIN_VENV])
        raise EinrichtungFehler([texte.UMGEBUNG_FEHLER, *_gekuerzt(ausgabe)])
    python = _python_in(neu)
    pip = _ausfuehren([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
                       "-vv", "-r", str(requirements)])
    if pip.returncode != 0:
        ausgabe = pip.stdout + pip.stderr
        if any(zeichen in ausgabe for zeichen in NETZ_ZEICHEN):
            raise EinrichtungFehler([texte.KEIN_INTERNET])
        raise EinrichtungFehler([texte.PIP_FEHLER, *_gekuerzt(_meldungen_von_pip(ausgabe))])
    if not _laeuft(python):
        raise EinrichtungFehler([texte.PIP_FEHLER])


def _inhaber() -> str:
    return f"{os.getpid()} {socket.gethostname()} {time.time():.0f}"


def _lebt(pid: int) -> bool:
    return plattform.lebt(pid)


def _verwaist(sperre: Path) -> bool:
    try:
        alter = time.time() - sperre.stat().st_mtime
        teile = sperre.read_text(encoding="utf-8").split()
    except OSError:
        return False
    if alter > VERWAIST_NACH_S:
        return True
    if len(teile) < 2 or not teile[0].isdigit():
        return alter > UNLESBAR_VERWAIST_NACH_S
    return teile[1] == socket.gethostname() and not _lebt(int(teile[0]))


def _raeume_ab(sperre: Path) -> None:
    beiseite = sperre.with_name(f"{sperre.name}.{os.getpid()}.alt")
    try:
        os.replace(sperre, beiseite)
        beiseite.unlink()
    except OSError:
        pass


def _nimm_sperre(daten: Path, python: Path) -> bool:
    sperre = daten / SPERRE
    ende = time.monotonic() + WARTEN_HOECHSTENS_S
    while True:
        try:
            datei = os.open(sperre, os.O_CREAT | os.O_EXCL | os.O_WRONLY | plattform.BINAER, 0o644)
        except FileExistsError:
            if _laeuft(python):
                return False
            if _verwaist(sperre):
                _raeume_ab(sperre)
                continue
            if time.monotonic() > ende:
                raise EinrichtungFehler([texte.ANDERE_SITZUNG])
            time.sleep(WARTEN_TAKT_S)
            continue
        with os.fdopen(datei, "w", encoding="utf-8") as f:
            f.write(_inhaber())
        return True


def _schreibfehler(fehler: OSError, daten: Path, home: Path) -> EinrichtungFehler:
    if fehler.errno in KEIN_SCHREIBRECHT:
        return EinrichtungFehler([texte.daten_nicht_schreibbar(tilde(daten, home))])
    return EinrichtungFehler([texte.UMGEBUNG_FEHLER, fehler.strerror or str(fehler)])


def python_der_umgebung(paket: Path, daten: Path | None, home: Path, sage: Callable[[str], None]) -> Path:
    daten = datenordner(paket, daten)
    umgebung = daten / UMGEBUNG
    python = _python_in(umgebung)
    if _laeuft(python):
        return python
    sage(texte.ANKUENDIGUNG)
    try:
        daten.mkdir(parents=True, exist_ok=True)
        if not _nimm_sperre(daten, python):
            return python
    except OSError as fehler:
        raise _schreibfehler(fehler, daten, home)
    neu = daten / f"{UMGEBUNG}.neu-{os.getpid()}"
    try:
        if _laeuft(python):
            return python
        _entferne(neu)
        _lege_umgebung_an(neu, paket / "requirements.txt")
        _entferne(umgebung)
        plattform.ersetze(neu, umgebung, geduldig=True)
        if not _laeuft(python):
            _entferne(umgebung)
            raise EinrichtungFehler([texte.UMGEBUNG_FEHLER])
        return python
    except OSError as fehler:
        raise _schreibfehler(fehler, daten, home)
    finally:
        _entferne(neu)
        try:
            (daten / SPERRE).unlink()
        except OSError:
            pass
