from __future__ import annotations

import hashlib
import hmac
import json
import ntpath
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import plattform

BEREICHE = ("project", "local", "user")
SCHLUESSEL = re.compile(r"^@gedaechtnis/g[0-9a-f]{12}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$")
PRAEFIX = "@gedaechtnis/"
GID = re.compile(r"^g[0-9a-f]{12}$")
DATEI = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$")
MEMORY_MD = "memory.md"
CLAUDE = ".claude"
UNTERORDNER = {"project": (CLAUDE, "agent-memory"), "local": (CLAUDE, "agent-memory-local"), "user": ("agent-memory",)}
DATEI_HOECHSTENS = 256 * 1024
ORDNER_HOECHSTENS = 20
DATEIEN_HOECHSTENS = 200
BYTES_HOECHSTENS = 4 * 1024 * 1024
LESEN_HOECHSTENS = 64 * 1024 * 1024
ORTE_HOECHSTENS = 5
VERSUCHE = 5
VERSUCH_ABSTAND_S = 0.1
BINAER = plattform.BINAER
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
BOM = b"\xef\xbb\xbf"

ORDNER_UNBEKANNT = "Dieser Memory-Ordner ist der Werkstatt nicht bekannt."
GRUND_VERWEIS = "auf dem Weg liegt ein Symlink oder eine Verbindung"
GRUND_NICHT_DERSELBE = "der Ordner ist nicht mehr derselbe wie bei der Auswertung"
GRUND_FEHLT = "es gibt ihn nicht mehr"
GRUND_KEIN_ORDNER = "auf dem Weg liegt etwas, das kein Ordner ist"
GRUND_DATEI = "die Datei ist keine reguläre Datei"
GRUND_DATEI_FEHLT = "die Datei gibt es nicht mehr"
GRUND_NAME = "der Dateiname ist nicht erlaubt"
GRUND_PLUGINS = "er liegt in einem Installationsordner von Plugins"
GRUND_ERGEBNIS = "er liegt im Ergebnisordner der Werkstatt"
GRUND_GIT = "er liegt unter .git"
GRUND_HOME = "der Anker ist dein Home-Ordner"
GRUND_WURZEL = "der Anker ist die Wurzel eines Laufwerks"
GRUND_FORM = "der Ordner passt nicht zu Agent und Bereich"
GRUND_SCHREIBSCHUTZ = "der Ordner ist schreibgeschützt"


class GedaechtnisFehler(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text
        self.status = 409


@dataclass(frozen=True)
class Ziel:
    gid: str
    agent: str
    bereich: str
    anker: Path
    anker_id: tuple[int, int]
    ordner: Path
    ordner_id: tuple[int, int]
    ort: int | None


@dataclass(frozen=True)
class Verboten:
    home: Path
    claude_ordner: Path
    ergebnis: Path


@dataclass(frozen=True)
class Stand:
    sha256: str
    zeilen: list[str] = field(default_factory=list)


def tilde(pfad: Path, home: Path | None) -> str:
    text = str(pfad)
    if home is None:
        return text
    for p, h in ((os.path.abspath(pfad), os.path.abspath(home)), (os.path.realpath(pfad), os.path.realpath(home))):
        if h and h != os.sep and (os.path.normcase(p) == os.path.normcase(h)
                                  or os.path.normcase(p).startswith(os.path.normcase(h + os.sep))):
            return "~" + p[len(h):].replace(os.sep, "/")
    return text


def schreibt_nicht(ordner: Path, verboten: Verboten | None, grund: str) -> str:
    home = verboten.home if verboten is not None else None
    return f"Im Memory-Ordner {tilde(ordner, home)} schreibt die Werkstatt nicht: {grund}."


def nicht_derselbe(ordner: Path, home: Path | None) -> str:
    return f"Der Memory-Ordner {tilde(ordner, home)} ist nicht mehr derselbe; die Werkstatt stellt dort nichts wieder her."


def gibt_es_nicht_mehr(ordner: Path, home: Path | None) -> str:
    return f"Den Memory-Ordner {tilde(ordner, home)} gibt es nicht mehr; die Werkstatt legt ihn nicht neu an."


def geoeffnet(pfad: Path, home: Path | None) -> str:
    return (f"Die Datei {tilde(pfad, home)} ist gerade geöffnet. Schließ das Programm, das sie benutzt, und übernimm "
            f"noch einmal.")


def ordnername(kit_art: str, kit_id: str, agent: str) -> str:
    return f"{kit_id}-{agent}" if kit_art == "plugin" else agent


def ordner_fuer(bereich: str, anker: Path, name: str) -> Path:
    return Path(anker).joinpath(*UNTERORDNER[bereich], name)


def schluessel(gid: str, datei: str) -> str:
    return f"{PRAEFIX}{gid}/{datei}"


def teile_von(schluessel_: str) -> tuple[str, str]:
    if not SCHLUESSEL.fullmatch(schluessel_):
        raise ValueError(schluessel_)
    _, gid, datei = schluessel_.split("/")
    return gid, datei


def ist_schluessel(schluessel_: object) -> bool:
    return isinstance(schluessel_, str) and bool(SCHLUESSEL.fullmatch(schluessel_))


def gid_fuer(geheimnis: bytes, ordner: Path, anker_id: tuple[int, int], ordner_id: tuple[int, int]) -> str:
    text = json.dumps([str(ordner), list(anker_id), list(ordner_id)], separators=(",", ":")).encode("utf-8")
    return "g" + hmac.new(geheimnis, text, hashlib.sha256).hexdigest()[:12]


def identitaet(info) -> tuple[int, int]:
    return (int(info.st_dev), int(info.st_ino))


_identitaet = identitaet


def _normal(pfad: Path) -> str:
    text = str(pfad)
    if plattform.ist_windows():
        return ntpath.normcase(ntpath.normpath(text.replace("/", "\\")))
    return os.path.normcase(os.path.normpath(text))


def _unter(pfad: Path, ordner: Path) -> bool:
    p, o = _normal(pfad), _normal(ordner)
    trenner = "\\" if plattform.ist_windows() else os.sep
    return p == o or p.startswith(o.rstrip(trenner) + trenner)


def _ist_wurzel(anker: Path) -> bool:
    text = str(anker)
    if plattform.ist_windows() or "\\" in text or re.match(r"^[A-Za-z]:", text):
        laufwerk, rest = ntpath.splitdrive(text.replace("/", "\\"))
        return bool(laufwerk) and rest.strip("\\") == ""
    return Path(anker).parent == Path(anker)


def _ist_absolut(anker: Path) -> bool:
    text = str(anker)
    return os.path.isabs(text) or (ntpath.isabs(text) and bool(ntpath.splitdrive(text)[0]))


def anker_verboten(anker: Path, verboten: Verboten) -> str | None:
    if not _ist_absolut(anker):
        return GRUND_FORM
    if _ist_wurzel(anker):
        return GRUND_WURZEL
    if _normal(anker) == _normal(verboten.home):
        return GRUND_HOME
    try:
        if os.path.samestat(os.stat(anker), os.stat(verboten.home)):
            return GRUND_HOME
    except (OSError, ValueError):
        pass
    return ordner_verboten(anker, verboten)


def ordner_verboten(ordner: Path, verboten: Verboten) -> str | None:
    plugins = [Path(verboten.claude_ordner) / "plugins", Path(verboten.home) / CLAUDE / "plugins"]
    for ort in (ordner, Path(os.path.realpath(ordner))):
        if any(_unter(ort, p) or _unter(ort, Path(os.path.realpath(p))) for p in plugins):
            return GRUND_PLUGINS
        if _unter(ort, verboten.ergebnis) or _unter(ort, Path(os.path.realpath(verboten.ergebnis))):
            return GRUND_ERGEBNIS
        if any(teil.casefold() == ".git" for teil in Path(ort).parts):
            return GRUND_GIT
    return None


def lstat_oder_fehler(pfad: Path):
    try:
        return os.lstat(pfad)
    except FileNotFoundError:
        return None
    except OSError:
        return False


_lstat = lstat_oder_fehler


def pruefe_weg(anker: Path, anker_id: tuple, teile: tuple[str, ...], ordner_id: tuple,
               fehler: Callable[[str], Exception]) -> Path:
    info = _lstat(Path(anker))
    if info is None or info is False:
        raise fehler(GRUND_FEHLT)
    if plattform.ist_verweis_info(info):
        raise fehler(GRUND_VERWEIS)
    if not stat.S_ISDIR(info.st_mode):
        raise fehler(GRUND_KEIN_ORDNER)
    if _identitaet(info) != tuple(anker_id):
        raise fehler(GRUND_NICHT_DERSELBE)
    pfad = Path(anker)
    for teil in teile:
        pfad = pfad / teil
        info = _lstat(pfad)
        if info is None or info is False:
            raise fehler(GRUND_FEHLT)
        if plattform.ist_verweis_info(info):
            raise fehler(GRUND_VERWEIS)
        if not stat.S_ISDIR(info.st_mode):
            raise fehler(GRUND_KEIN_ORDNER)
    if _identitaet(info) != tuple(ordner_id):
        raise fehler(GRUND_NICHT_DERSELBE)
    return pfad


def _teile(ziel: Ziel) -> tuple[str, ...]:
    return (*UNTERORDNER[ziel.bereich], Path(ziel.ordner).name)


def pruefe(ziel: Ziel, datei: str, verboten: Verboten, *, wiederherstellen: bool = False,
           fehlt_erlaubt: bool | None = None, nur_lesen: bool = False) -> Path:
    ordner = Path(ziel.ordner)
    home = verboten.home
    fehlt_erlaubt = wiederherstellen if fehlt_erlaubt is None else fehlt_erlaubt

    def fehler(grund: str) -> GedaechtnisFehler:
        if wiederherstellen and grund == GRUND_FEHLT:
            return GedaechtnisFehler(gibt_es_nicht_mehr(ordner, home))
        if wiederherstellen and grund == GRUND_NICHT_DERSELBE:
            return GedaechtnisFehler(nicht_derselbe(ordner, home))
        return GedaechtnisFehler(schreibt_nicht(ordner, verboten, grund))

    if ziel.bereich not in BEREICHE or ordner.parent != Path(ziel.anker).joinpath(*UNTERORDNER[ziel.bereich]):
        raise fehler(GRUND_FORM)
    if not isinstance(datei, str) or not DATEI.fullmatch(datei):
        raise fehler(GRUND_NAME)
    grund = anker_verboten(Path(ziel.anker), verboten) or ordner_verboten(ordner, verboten)
    if grund is not None:
        raise fehler(grund)
    pruefe_weg(Path(ziel.anker), ziel.anker_id, _teile(ziel), ziel.ordner_id, fehler)
    if not nur_lesen and not os.access(ordner, os.W_OK | os.X_OK):
        raise fehler(GRUND_SCHREIBSCHUTZ)
    pfad = ordner / datei
    info = _lstat(pfad)
    if info is False:
        raise fehler(GRUND_DATEI)
    if info is None:
        if not fehlt_erlaubt:
            raise fehler(GRUND_DATEI_FEHLT)
        return pfad
    if plattform.ist_verweis_info(info):
        raise fehler(GRUND_VERWEIS)
    if not stat.S_ISREG(info.st_mode):
        raise fehler(GRUND_DATEI)
    return pfad


def seit_vorschau(datei: str, agent: str) -> str:
    return f"{datei} im Memory von {agent} hat sich seit der Vorschau geändert. Schau dir die Vorschau noch einmal an."


def gerade_geaendert(datei: str, agent: str) -> str:
    return f"{datei} im Memory von {agent} hat sich gerade geändert. Mach „Rückgängig“ noch einmal."


def weg_unveraendert(anker: Path, anker_id: tuple, teile: tuple[str, ...], ordner_id: tuple,
                     fehler: Callable[[str], Exception]) -> None:
    pfad = Path(anker)
    for teil in teile:
        pfad = pfad / teil
        info = _lstat(pfad)
        if not info or plattform.ist_verweis_info(info) or not stat.S_ISDIR(info.st_mode):
            raise fehler(GRUND_VERWEIS)
    for ort, soll in ((Path(anker), anker_id), (pfad, ordner_id)):
        info = _lstat(ort)
        if info is None or info is False or plattform.ist_verweis_info(info) or not stat.S_ISDIR(info.st_mode) \
                or _identitaet(info) != tuple(soll):
            raise fehler(GRUND_NICHT_DERSELBE)
        try:
            echt = os.stat(ort)
        except OSError:
            raise fehler(GRUND_FEHLT)
        if _identitaet(echt) != tuple(soll):
            raise fehler(GRUND_NICHT_DERSELBE)


def _identitaeten_gleich(ziel: Ziel, verboten: Verboten) -> None:
    def fehler(grund: str) -> GedaechtnisFehler:
        return GedaechtnisFehler(schreibt_nicht(Path(ziel.ordner), verboten, grund))

    weg_unveraendert(Path(ziel.anker), ziel.anker_id, _teile(ziel), ziel.ordner_id, fehler)
    grund = ordner_verboten(Path(ziel.ordner), verboten)
    if grund is not None:
        raise fehler(grund)


def lies_datei(pfad: Path, hoechstens: int = DATEI_HOECHSTENS) -> bytes:
    vorher = os.lstat(pfad)
    if plattform.ist_verweis_info(vorher) or not stat.S_ISREG(vorher.st_mode):
        raise ValueError("keine reguläre Datei")
    fd = os.open(pfad, os.O_RDONLY | BINAER | NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not os.path.samestat(info, vorher) or not stat.S_ISREG(info.st_mode):
            raise ValueError("keine reguläre Datei")
        return lies_fd(fd, hoechstens)
    finally:
        os.close(fd)


ZU_GROSS = "zu groß"


def lies_fd(fd: int, hoechstens: int) -> bytes:
    teile = []
    gelesen = 0
    while gelesen <= hoechstens:
        stueck = os.read(fd, min(65536, hoechstens + 1 - gelesen))
        if not stueck:
            break
        teile.append(stueck)
        gelesen += len(stueck)
    inhalt = b"".join(teile)
    if len(inhalt) > hoechstens:
        raise ValueError(ZU_GROSS)
    return inhalt


def modus(ziel: Ziel, datei: str, verboten: Verboten) -> int | None:
    pfad = pruefe(ziel, datei, verboten, fehlt_erlaubt=True, nur_lesen=True)
    info = _lstat(pfad)
    return stat.S_IMODE(info.st_mode) if info and stat.S_ISREG(info.st_mode) else None


def lies(ziel: Ziel, datei: str, verboten: Verboten) -> bytes | None:
    pfad = pruefe(ziel, datei, verboten, fehlt_erlaubt=True, nur_lesen=True)
    try:
        return lies_datei(pfad, LESEN_HOECHSTENS)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise GedaechtnisFehler(schreibt_nicht(Path(ziel.ordner), verboten, GRUND_DATEI))


def stand_von(inhalt: bytes) -> Stand:
    return Stand(hashlib.sha256(inhalt).hexdigest(), zeilen_von(inhalt))


def zeilen_von(inhalt: bytes) -> list[str]:
    text = inhalt[len(BOM):] if inhalt.startswith(BOM) else inhalt
    return [z.removesuffix(b"\r").decode("utf-8", "replace") for z in _rohzeilen(text)]


def _rohzeilen(text: bytes) -> list[bytes]:
    if not text:
        return []
    zeilen = text.split(b"\n")
    if zeilen[-1] == b"":
        zeilen.pop()
    return zeilen


def treffer(inhalt: bytes, zeilentext: str) -> int:
    gesucht = zeilentext.encode("utf-8")
    text = inhalt[len(BOM):] if inhalt.startswith(BOM) else inhalt
    return sum(1 for z in _rohzeilen(text) if z.removesuffix(b"\r") == gesucht)


def ohne_zeile(inhalt: bytes, zeilentext: str) -> bytes:
    gesucht = zeilentext.encode("utf-8")
    bom = BOM if inhalt.startswith(BOM) else b""
    teile = inhalt[len(bom):].split(b"\n")
    zeilen = [t + b"\n" for t in teile[:-1]] + ([teile[-1]] if teile[-1] else [])
    stellen = [i for i, z in enumerate(zeilen) if z.removesuffix(b"\n").removesuffix(b"\r") == gesucht]
    if len(stellen) != 1:
        raise ValueError(f"{len(stellen)} Treffer")
    del zeilen[stellen[0]]
    return bom + b"".join(zeilen)


def mit_wiederholung(aktion: Callable[[], None], geoeffnet_: Callable[[], Exception]) -> None:
    versuche = VERSUCHE if plattform.ist_windows() else 1
    for versuch in range(1, versuche + 1):
        try:
            aktion()
            return
        except PermissionError:
            if versuch >= versuche:
                if plattform.ist_windows():
                    raise geoeffnet_()
                raise
            time.sleep(VERSUCH_ABSTAND_S)


def _mit_wiederholung(aktion, pfad: Path, verboten: Verboten) -> None:
    mit_wiederholung(aktion, lambda: GedaechtnisFehler(geoeffnet(pfad, verboten.home)))


OHNE_PRUEFUNG = object()


ORDNER_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | NOFOLLOW


def oeffne_weg(anker: Path, anker_id: tuple, teile: tuple[str, ...], ordner_id: tuple,
               fehler: Callable[[str], Exception]) -> int:
    try:
        fd = os.open(Path(anker), ORDNER_FLAGS)
    except OSError:
        raise fehler(GRUND_NICHT_DERSELBE)
    try:
        if _identitaet(os.fstat(fd)) != tuple(anker_id):
            raise fehler(GRUND_NICHT_DERSELBE)
        for teil in teile:
            try:
                neu = os.open(teil, ORDNER_FLAGS, dir_fd=fd)
            except OSError:
                raise fehler(GRUND_VERWEIS)
            os.close(fd)
            fd = neu
        if _identitaet(os.fstat(fd)) != tuple(ordner_id):
            raise fehler(GRUND_NICHT_DERSELBE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _oeffne_ordner(ziel: Ziel, verboten: Verboten) -> int:
    return oeffne_weg(Path(ziel.anker), ziel.anker_id, _teile(ziel), ziel.ordner_id,
                      lambda grund: GedaechtnisFehler(schreibt_nicht(Path(ziel.ordner), verboten, grund)))


LESE_FLAGS = os.O_RDONLY | BINAER | NOFOLLOW | getattr(os, "O_NONBLOCK", 0)


def lies_ueber_weg(ordner_fd: int | None, pfad: Path, datei: str, hoechstens: int,
                   pruefe_info: Callable[[os.stat_result], None] | None = None) -> bytes | None:
    vorher = None
    try:
        if ordner_fd is None:
            vorher = os.lstat(pfad)
            if plattform.ist_verweis_info(vorher) or not stat.S_ISREG(vorher.st_mode):
                raise ValueError(GRUND_DATEI)
            fd = os.open(pfad, LESE_FLAGS)
        else:
            fd = os.open(datei, LESE_FLAGS, dir_fd=ordner_fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (vorher is not None and not os.path.samestat(info, vorher)):
            raise ValueError(GRUND_DATEI)
        if pruefe_info is not None:
            pruefe_info(info)
        return lies_fd(fd, hoechstens)
    finally:
        os.close(fd)


def _aktuell(datei: str, ordner_fd: int | None, pfad: Path) -> bytes | None:
    return lies_ueber_weg(ordner_fd, pfad, datei, LESEN_HOECHSTENS)


def _pruefe_stand(ziel: Ziel, datei: str, erwartet: object, ordner_fd: int | None, pfad: Path,
                  verboten: Verboten, wiederherstellen: bool) -> None:
    if erwartet is OHNE_PRUEFUNG:
        return
    try:
        jetzt = _aktuell(datei, ordner_fd, pfad)
    except (OSError, ValueError):
        raise GedaechtnisFehler(schreibt_nicht(Path(ziel.ordner), verboten, GRUND_DATEI))
    if jetzt != erwartet:
        raise GedaechtnisFehler((gerade_geaendert if wiederherstellen else seit_vorschau)(datei, ziel.agent))


def schreibe(ziel: Ziel, datei: str, inhalt: bytes | None, verboten: Verboten, modus: int | None = None, *,
             wiederherstellen: bool = False, erwartet: object = OHNE_PRUEFUNG) -> None:
    pfad = pruefe(ziel, datei, verboten, wiederherstellen=wiederherstellen, fehlt_erlaubt=True)
    vorhanden = _lstat(pfad)
    ordner_fd = None if plattform.ist_windows() else _oeffne_ordner(ziel, verboten)
    try:
        if inhalt is None:
            _loesche(ziel, datei, pfad, vorhanden, ordner_fd, erwartet, verboten, wiederherstellen)
        else:
            _ersetze(ziel, datei, pfad, inhalt, modus, vorhanden, ordner_fd, erwartet, verboten, wiederherstellen)
    finally:
        if ordner_fd is not None:
            os.close(ordner_fd)


def _loesche(ziel: Ziel, datei: str, pfad: Path, vorhanden, ordner_fd: int | None, erwartet: object,
             verboten: Verboten, wiederherstellen: bool) -> None:
    if not vorhanden:
        return
    _identitaeten_gleich(ziel, verboten)
    _pruefe_stand(ziel, datei, erwartet, ordner_fd, pfad, verboten, wiederherstellen)
    if ordner_fd is None:
        _mit_wiederholung(lambda: os.unlink(pfad), pfad, verboten)
        _nachher_am_ort(ziel, verboten)
    else:
        _mit_wiederholung(lambda: os.unlink(datei, dir_fd=ordner_fd), pfad, verboten)


def ersetze_atomar(ordner: Path, ordner_fd: int | None, datei: str, inhalt: bytes, modus: int,
                   davor: Callable[[], None], geoeffnet_: Callable[[], Exception]) -> None:
    name = f".{datei}.{secrets.token_hex(6)}.tmp"
    temp = Path(ordner) / name
    pfad = Path(ordner) / datei
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | BINAER | NOFOLLOW
    fd = os.open(temp, flags, 0o600) if ordner_fd is None else os.open(name, flags, 0o600, dir_fd=ordner_fd)
    try:
        try:
            geschrieben = 0
            while geschrieben < len(inhalt):
                geschrieben += os.write(fd, inhalt[geschrieben:])
            os.fsync(fd)
            if ordner_fd is not None:
                os.fchmod(fd, modus)
        finally:
            os.close(fd)
        if ordner_fd is None:
            os.chmod(temp, modus)
        davor()
        if ordner_fd is None:
            mit_wiederholung(lambda: os.replace(temp, pfad), geoeffnet_)
        else:
            mit_wiederholung(lambda: os.replace(name, datei, src_dir_fd=ordner_fd, dst_dir_fd=ordner_fd), geoeffnet_)
    except BaseException:
        try:
            if ordner_fd is None:
                os.unlink(temp)
            else:
                os.unlink(name, dir_fd=ordner_fd)
        except OSError:
            pass
        raise


def _ersetze(ziel: Ziel, datei: str, pfad: Path, inhalt: bytes, modus: int | None, vorhanden,
             ordner_fd: int | None, erwartet: object, verboten: Verboten, wiederherstellen: bool) -> None:
    if modus is None:
        modus = stat.S_IMODE(vorhanden.st_mode) if vorhanden else 0o600

    def davor() -> None:
        _identitaeten_gleich(ziel, verboten)
        danach = _lstat(pfad)
        if danach is False or (danach is not None and (plattform.ist_verweis_info(danach)
                                                       or not stat.S_ISREG(danach.st_mode))):
            raise GedaechtnisFehler(schreibt_nicht(Path(ziel.ordner), verboten, GRUND_DATEI))
        _pruefe_stand(ziel, datei, erwartet, ordner_fd, pfad, verboten, wiederherstellen)

    ersetze_atomar(Path(ziel.ordner), ordner_fd, datei, inhalt, modus, davor,
                   lambda: GedaechtnisFehler(geoeffnet(pfad, verboten.home)))
    if ordner_fd is None:
        _nachher_am_ort(ziel, verboten)


def _nachher_am_ort(ziel: Ziel, verboten: Verboten) -> None:
    _identitaeten_gleich(ziel, verboten)


def orte(arbeitsordner: list[Path], verboten: Verboten, hoechstens: int = ORTE_HOECHSTENS) -> list[Path]:
    gesehen: set[tuple[int, int]] = set()
    ergebnis: list[Path] = []
    for ort in arbeitsordner:
        if len(ergebnis) >= hoechstens:
            break
        ort = Path(ort)
        if not _ist_absolut(ort) or ".." in ort.parts:
            continue
        try:
            echt = Path(os.path.realpath(ort))
            info = os.stat(echt)
        except (OSError, ValueError):
            continue
        if not stat.S_ISDIR(info.st_mode) or anker_verboten(echt, verboten) is not None:
            continue
        if _identitaet(info) in gesehen:
            continue
        gesehen.add(_identitaet(info))
        ergebnis.append(echt)
    return ergebnis


class Register:
    def __init__(self, geheimnis: bytes, home: Path | None = None, kit_art: str = "plugin", kit_id: str = "") -> None:
        self.geheimnis = geheimnis
        self.home = home
        self.kit_art = kit_art
        self.kit_id = kit_id
        self._ziele: dict[str, Ziel] = {}

    def ziel(self, agent: str, bereich: str, anker: Path, ort: int | None) -> Ziel | None:
        if bereich not in BEREICHE:
            return None
        ordner = ordner_fuer(bereich, Path(anker), ordnername(self.kit_art, self.kit_id, agent))
        anker_info = _lstat(Path(anker))
        if not anker_info or plattform.ist_verweis_info(anker_info) or not stat.S_ISDIR(anker_info.st_mode):
            return None
        info = None
        pfad = Path(anker)
        for teil in (*UNTERORDNER[bereich], ordner.name):
            pfad = pfad / teil
            info = _lstat(pfad)
            if not info or plattform.ist_verweis_info(info) or not stat.S_ISDIR(info.st_mode):
                return None
        anker_id, ordner_id = _identitaet(anker_info), _identitaet(info)
        gid = gid_fuer(self.geheimnis, ordner, anker_id, ordner_id)
        ziel = Ziel(gid=gid, agent=agent, bereich=bereich, anker=Path(anker), anker_id=anker_id, ordner=ordner,
                    ordner_id=ordner_id, ort=ort)
        self._ziele[gid] = ziel
        return ziel

    def merke(self, ziel: Ziel) -> None:
        self._ziele[ziel.gid] = ziel

    def hole(self, gid: object) -> Ziel:
        if not isinstance(gid, str) or gid not in self._ziele:
            raise GedaechtnisFehler(ORDNER_UNBEKANNT)
        return self._ziele[gid]

    def kennt(self, gid: object) -> bool:
        return isinstance(gid, str) and gid in self._ziele

    def aus_sicherung(self, eintrag: dict, gid: str) -> Ziel:
        try:
            bereich, agent = eintrag["bereich"], eintrag["agent"]
            ordner = Path(eintrag["ordner"])
            anker_id = tuple(int(x) for x in eintrag["anker_id"])
            ordner_id = tuple(int(x) for x in eintrag["ordner_id"])
        except (KeyError, TypeError, ValueError):
            raise GedaechtnisFehler(ORDNER_UNBEKANNT)
        if bereich not in BEREICHE or not isinstance(agent, str) or len(anker_id) != 2 or len(ordner_id) != 2:
            raise GedaechtnisFehler(ORDNER_UNBEKANNT)
        anker = ordner.parent
        for _ in UNTERORDNER[bereich]:
            anker = anker.parent
        if ordner_fuer(bereich, anker, ordner.name) != ordner \
                or ordner.name != ordnername(self.kit_art, self.kit_id, agent) \
                or not hmac.compare_digest(gid_fuer(self.geheimnis, ordner, anker_id, ordner_id), gid):
            raise GedaechtnisFehler(ORDNER_UNBEKANNT)
        ziel = Ziel(gid=gid, agent=agent, bereich=bereich, anker=anker, anker_id=anker_id, ordner=ordner,
                    ordner_id=ordner_id, ort=None)
        self._ziele.setdefault(gid, ziel)
        return ziel


def _grenze(anzeige: str, typ: str) -> dict:
    return {"datei": anzeige, "grund": "grenze", "typ": typ}


def kopiere(ziele: list[Ziel], nach: Path, register: Register) -> tuple[list[dict], list[dict]]:
    gedaechtnis: list[dict] = []
    uebersprungen: list[dict] = []
    dateien_gesamt = 0
    bytes_gesamt = 0
    voll = False
    for ziel in ziele:
        anzeige = tilde(Path(ziel.ordner), register.home)
        info = _lstat(Path(ziel.ordner))
        if not info or plattform.ist_verweis_info(info) or not stat.S_ISDIR(info.st_mode) \
                or _identitaet(info) != tuple(ziel.ordner_id):
            uebersprungen.append({"datei": anzeige, "grund": "symlink" if info and plattform.ist_verweis_info(info)
                                  else "nicht_lesbar", "typ": "ordner"})
            continue
        if len(gedaechtnis) >= ORDNER_HOECHSTENS:
            uebersprungen.append(_grenze(anzeige, "ordner"))
            continue
        try:
            namen = sorted(os.listdir(ziel.ordner))
        except OSError:
            uebersprungen.append({"datei": anzeige, "grund": "nicht_lesbar", "typ": "ordner"})
            continue
        nr = len(gedaechtnis) + 1
        register.merke(ziel)
        kopie = Path(nach) / str(nr)
        kopiert: list[dict] = []
        staende: dict[str, Stand] = {}
        for name in namen:
            if name.startswith(".") or not DATEI.fullmatch(name):
                continue
            pfad = Path(ziel.ordner) / name
            eintrag = _lstat(pfad)
            if eintrag is None:
                continue
            if eintrag and stat.S_ISDIR(eintrag.st_mode) and not plattform.ist_verweis_info(eintrag):
                continue
            rel = f"{anzeige}/{name}"
            if eintrag is False:
                uebersprungen.append({"datei": rel, "grund": "nicht_lesbar", "typ": "datei"})
                continue
            if plattform.ist_verweis_info(eintrag):
                uebersprungen.append({"datei": rel, "grund": "symlink", "typ": "datei"})
                continue
            if not stat.S_ISREG(eintrag.st_mode):
                uebersprungen.append({"datei": rel, "grund": "nicht_lesbar", "typ": "datei"})
                continue
            if eintrag.st_size > DATEI_HOECHSTENS:
                uebersprungen.append({"datei": rel, "grund": "groesse", "typ": "datei"})
                continue
            if voll or dateien_gesamt >= DATEIEN_HOECHSTENS or bytes_gesamt + eintrag.st_size > BYTES_HOECHSTENS:
                voll = True
                uebersprungen.append(_grenze(rel, "datei"))
                continue
            try:
                inhalt = lies_datei(pfad)
            except (OSError, ValueError):
                uebersprungen.append({"datei": rel, "grund": "nicht_lesbar", "typ": "datei"})
                continue
            if bytes_gesamt + len(inhalt) > BYTES_HOECHSTENS:
                voll = True
                uebersprungen.append(_grenze(rel, "datei"))
                continue
            kopie.mkdir(parents=True, exist_ok=True, mode=0o700)
            ziel_datei = kopie / name
            fd = os.open(ziel_datei, os.O_WRONLY | os.O_CREAT | os.O_EXCL | BINAER | NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as ausgabe:
                ausgabe.write(inhalt)
            staende[name] = stand_von(inhalt)
            dateien_gesamt += 1
            bytes_gesamt += len(inhalt)
            kopiert.append({"datei": name, "bytes": len(inhalt)})
        kopie.mkdir(parents=True, exist_ok=True, mode=0o700)
        gedaechtnis.append({"nr": nr, "agent": ziel.agent, "bereich": ziel.bereich, "ort": ziel.ort,
                            "ordner": str(kopie), "anzeige": anzeige, "dateien": kopiert, "gid": ziel.gid,
                            "staende": staende})
    return gedaechtnis, uebersprungen
