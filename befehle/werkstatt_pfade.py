"""Pfadschutz der Werkstatt (specs/werkstatt.yaml WS-X5), an einer Stelle; Standardbibliothek und atlas/vokabular.py.

Jeder Pfad, der geschrieben, gelöscht oder aus verlauf.json bzw. sicherung.json gelesen wird, geht durch
diese Funktionen: relativ, ohne '..', ohne Symlink auf dem Weg, unter dem Kit. Ordner werden über ihre
Identität (st_dev, st_ino) verglichen, nicht über die Schreibweise, damit Groß-/Kleinschreibung auf macOS
nichts umgeht.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
from pathlib import Path

import finden
import plattform
from vokabular import AGENT_MUSTER, SKILL_DATEI

ABLEITUNG = "@ableitung"
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # fehlt unter Windows


class PfadFehler(Exception):
    pass


class LeseFehler(Exception):
    """grund: FEHLT (gibt es nicht), NICHT_OFFEN (ließ sich nicht öffnen, z. B. ein Symlink), NICHT_REGULAER, ZU_GROSS."""

    FEHLT, NICHT_OFFEN, NICHT_REGULAER, ZU_GROSS = "fehlt", "nicht_offen", "nicht_regulaer", "zu_gross"

    def __init__(self, grund: str):
        super().__init__(grund)
        self.grund = grund


def lies_regulaer(pfad: Path, hoechstens: int) -> bytes:
    """Liest eine Datei, ohne einem Symlink zu folgen, nur als reguläre Datei und höchstens hoechstens Bytes; sonst
    LeseFehler. Ohne O_NOFOLLOW (Windows) prüft lstat vorher auf Symlink bzw. Junction und fstat danach, dass die
    geöffnete Datei dieselbe ist (kein Tausch dazwischen)."""
    vorher = None
    if not NOFOLLOW:
        try:
            vorher = os.lstat(pfad)
        except FileNotFoundError:
            raise LeseFehler(LeseFehler.FEHLT)
        except OSError:
            raise LeseFehler(LeseFehler.NICHT_OFFEN)
        if plattform.ist_verweis_info(vorher):
            raise LeseFehler(LeseFehler.NICHT_OFFEN)
    try:
        fd = os.open(pfad, os.O_RDONLY | plattform.BINAER | NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        raise LeseFehler(LeseFehler.FEHLT)
    except OSError:
        raise LeseFehler(LeseFehler.NICHT_OFFEN)
    if vorher is not None and not os.path.samestat(os.fstat(fd), vorher):
        os.close(fd)
        raise LeseFehler(LeseFehler.NICHT_OFFEN)
    if not stat.S_ISREG(os.fstat(fd).st_mode):  # vor fdopen: das scheitert an einem Ordner
        os.close(fd)
        raise LeseFehler(LeseFehler.NICHT_REGULAER)
    with os.fdopen(fd, "rb") as datei:
        inhalt = datei.read(hoechstens + 1)
    if len(inhalt) > hoechstens:
        raise LeseFehler(LeseFehler.ZU_GROSS)
    return inhalt


def sha(inhalt: bytes | None) -> str | None:
    return None if inhalt is None else hashlib.sha256(inhalt).hexdigest()


def pruefe_rel(rel: object) -> str:
    """Form eines Pfads relativ zum Kit: Text, mit '/', ohne leere Teile, '.', '..' oder Anfang '/'."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        raise PfadFehler(f"{rel!r} ist kein erlaubter Pfad im Kit.")
    if any(teil in ("", ".", "..") for teil in rel.split("/")):
        raise PfadFehler(f"{rel} ist kein erlaubter Pfad im Kit.")
    return rel


def gleich(a: Path, b: Path) -> bool:
    try:
        return os.path.samestat(os.stat(a), os.stat(b))
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# Identität statt Schreibweise (st_dev, st_ino); eine Fassung für Server und Befehle (Review 21, r21-1).
liegt_unter = finden.liegt_unter


# ---------------------------------------------------------------- Freigabeliste

# Zusätzlich zu den Regeln des Scanners: kein Name beginnt mit '.', höchstens 100 Zeichen. So bleibt die Werkstatt
# aus .git/ und anderen Punkt-Ordnern heraus, auch wenn der Scanner dort einmal mitläse.
DATEINAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")


def freigegeben(rel: object, skills: str, agenten: str) -> bool:
    """Nur was der Schreiber erzeugen kann: <agenten>/<name>.md, <skills>/<name>/SKILL.md (die Dateien, die der
    Scanner als Agent bzw. Skill liest). Kein Teil beginnt mit '.', außer den Ordnern selbst (.claude im Projekt)."""
    try:
        rel = pruefe_rel(rel)
    except PfadFehler:
        return False
    if rel.startswith(agenten + "/"):
        rest = rel[len(agenten) + 1:].split("/")
        return len(rest) == 1 and fnmatch.fnmatchcase(rest[0], AGENT_MUSTER) and bool(DATEINAME.fullmatch(rest[0]))
    if rel.startswith(skills + "/"):
        rest = rel[len(skills) + 1:].split("/")
        return len(rest) == 2 and bool(DATEINAME.fullmatch(rest[0])) and rest[1] == SKILL_DATEI
    return False


def pruefe_freigabe(rel: object, skills: str, agenten: str) -> str:
    if not freigegeben(rel, skills, agenten):
        raise PfadFehler(f"{rel} ist keine Skill- oder Agent-Datei; die Werkstatt schreibt dort nicht.")
    return rel


# ---------------------------------------------------------------- Probe-Kopie

class KopieFehler(Exception):
    pass


KOPIE_HOECHSTENS = 1024 * 1024  # je Datei in der Arbeitskopie der KI
# Probe-Kopie: eine gelesene Datei hinter einem Symlink kommt mit, aber zusammen höchstens so viel.
KOPIE_LINK_DATEIEN = 5000
KOPIE_LINK_BYTES = 64 * 1024 * 1024
SYMLINK, GROESSE, GRENZE, NICHT_LESBAR = "symlink", "groesse", "grenze", "nicht_lesbar"
DATEI, ORDNER = "datei", "ordner"
# Begleitdateien der Skills (nur Arbeitskopie der KI): Markdown und Text direkt im Skill-Ordner und in BEGLEIT_ORDNER.
BEGLEIT_ENDUNGEN = (".md", ".txt")
BEGLEIT_ORDNER = "references"
BEGLEIT_HOECHSTENS = 256 * 1024  # je Datei
BEGLEIT_DATEIEN = 200  # zusammen
BEGLEIT_BYTES = 4 * 1024 * 1024  # zusammen


def kopiere_quellen(wurzel: Path, quellen: list[str], ziel: Path, *, fuer_ki: bool = False) -> list[dict]:
    """Kopiert genau die Dateien, die der Scanner liest (quellen: Ordner.dateien() und die Hook-Skripte), nach ziel.

    Nur reguläre Dateien, keine Sonderdateien (FIFO, Gerät, Socket); was sonst in den Ordnern liegt (tools/.venv,
    node_modules, Bilder einer Skill), bleibt draußen. Die Probe-Kopie folgt Symlinks wie der Scanner (Datei und Ordner
    auf dem Weg) und nimmt den Inhalt mit, damit der Probe-Scan dem Scan des Kits gleicht; gelesene Dateien hinter
    Symlinks höchstens KOPIE_LINK_DATEIEN und KOPIE_LINK_BYTES Bytes, sonst KopieFehler. Für die Arbeitskopie der KI
    (fuer_ki) bleibt jede Datei mit einem Symlink auf dem Weg draußen, jede über KOPIE_HOECHSTENS und jede, die sich nicht
    lesen lässt; die Rückgabe nennt sie als {datei, grund: symlink | groesse | nicht_lesbar, typ: datei | ordner}, einen
    Symlink auf einen Ordner einmal (der oberste auf dem Weg). Fehler kommen als KopieFehler mit einem Satz.
    """
    uebersprungen: list[dict] = []
    hinter_symlink = {"dateien": 0, "bytes": 0}
    try:
        for rel in quellen:
            pruefe_rel(rel)
            quelle = wurzel / rel
            weg = symlink_auf_dem_weg(quelle, wurzel)
            if fuer_ki and weg is not None:
                eintrag = _vermerk(weg, wurzel, SYMLINK, ORDNER if weg.is_dir() else DATEI)
                if eintrag not in uebersprungen:
                    uebersprungen.append(eintrag)
                continue
            _kopiere(quelle, ziel / rel, wurzel, uebersprungen if fuer_ki else None,
                     hinter_symlink if weg is not None else None)
    except (OSError, PfadFehler) as fehler:
        raise KopieFehler(f"Die Probe-Kopie des Kits ließ sich nicht anlegen ({fehler}).")
    return uebersprungen


def symlink_auf_dem_weg(pfad: Path, wurzel: Path) -> Path | None:
    """Der oberste Symlink zwischen wurzel (ausschließlich) und pfad (einschließlich), nach der Schreibweise; sonst None.
    Unter Windows zählt eine Junction wie ein Symlink (plattform.ist_verweis)."""
    return next((p for p in [*reversed(pfad.parents), pfad]
                 if p != wurzel and wurzel in p.parents and plattform.ist_verweis(p)), None)


def _vermerk(pfad: Path, wurzel: Path, grund: str, typ: str) -> dict:
    return {"datei": pfad.relative_to(wurzel).as_posix(), "grund": grund, "typ": typ}


def _typ_des_ziels(eintrag: os.DirEntry | Path) -> str:
    """ORDNER, wenn der Eintrag (einem Symlink folgend) ein Ordner ist, sonst DATEI, auch wenn sich das nicht
    feststellen lässt."""
    try:
        return ORDNER if eintrag.is_dir() else DATEI
    except OSError:
        return DATEI


def _kopiere(quelle: Path, ziel: Path, wurzel: Path, uebersprungen: list[dict] | None,
             hinter_symlink: dict | None) -> None:
    """Mit uebersprungen (Arbeitskopie der KI, kein Symlink auf dem Weg): höchstens KOPIE_HOECHSTENS, ohne einem
    Symlink zu folgen. Mit hinter_symlink (Probe-Kopie, Datei hinter einem Symlink): zählt Dateien und Bytes gegen die
    Grenze."""
    if uebersprungen is not None:
        inhalt = _lies_fuer_ki(quelle, wurzel, KOPIE_HOECHSTENS, uebersprungen)
        if inhalt is None:
            return
    else:
        try:
            info = os.stat(quelle)
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode):
            return
        if hinter_symlink is not None:
            hinter_symlink["dateien"] += 1
            hinter_symlink["bytes"] += info.st_size
            if hinter_symlink["dateien"] > KOPIE_LINK_DATEIEN or hinter_symlink["bytes"] > KOPIE_LINK_BYTES:
                raise KopieFehler(zu_viel_hinter_symlink())
        try:
            inhalt = quelle.read_bytes()
        except PermissionError:
            return
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_bytes(inhalt)


def _lies_fuer_ki(pfad: Path, wurzel: Path, hoechstens: int, uebersprungen: list[dict]) -> bytes | None:
    """Liest eine Datei für die Arbeitskopie der KI (ohne Symlink zu folgen); was nicht geht, steht danach in
    uebersprungen (eine fehlende Datei nicht), dann None."""
    try:
        return lies_regulaer(pfad, hoechstens)
    except LeseFehler as fehler:
        if fehler.grund == LeseFehler.ZU_GROSS:
            uebersprungen.append(_vermerk(pfad, wurzel, GROESSE, DATEI))
        elif fehler.grund == LeseFehler.NICHT_OFFEN and plattform.ist_verweis(pfad):
            # Schutz gegen einen Wettlauf: Der Weg war beim Prüfen ohne Symlink, die Datei ist erst bis zum Öffnen
            # einer geworden.
            uebersprungen.append(_vermerk(pfad, wurzel, SYMLINK, _typ_des_ziels(pfad)))
        elif fehler.grund != LeseFehler.FEHLT:
            uebersprungen.append(_vermerk(pfad, wurzel, NICHT_LESBAR, DATEI))
    except OSError:
        uebersprungen.append(_vermerk(pfad, wurzel, NICHT_LESBAR, DATEI))
    return None


def kopiere_begleitdateien(wurzel: Path, skill_ordner: list[Path], ziel: Path, uebersprungen: list[dict]) -> None:
    """Nur für die Arbeitskopie der KI, damit sie die Referenzen einer Skill lesen kann: *.md und *.txt direkt in jedem
    Skill-Ordner mit SKILL.md (ohne Symlink auf dem Weg) und direkt in seinem Ordner references/, nichts tiefer. Nie
    SKILL.md (in keiner Schreibweise, auch nicht in references/), versteckte Einträge oder Symlinks. Je Datei höchstens
    BEGLEIT_HOECHSTENS, zusammen BEGLEIT_DATEIEN und BEGLEIT_BYTES, geprüft vor dem Lesen. In uebersprungen kommt
    {datei, grund: symlink | groesse | nicht_lesbar, typ} (typ eines Symlinks nach seinem Ziel), für einen verlinkten
    oder nicht lesbaren Ordner references bzw. Skill-Ordner einmal; eine zu große Datei fällt einzeln weg, danach geht
    es weiter, auch mit kleineren Dateien, die noch ins Byte-Budget passen. Gültige Dateien, für die das Budget nicht
    mehr reicht, zählt am Ende ein Eintrag {grund: grenze, typ: datei, anzahl}. Scanner-Liste und Probe-Kopie bleiben
    ohne sie; eine nicht lesbare Quelle lässt den Auftrag nie scheitern."""
    kandidaten: list[os.DirEntry] = []
    for ordner in skill_ordner:
        if symlink_auf_dem_weg(ordner, wurzel) is None and (ordner / SKILL_DATEI).is_file():
            for e in _begleit_eintraege(ordner, wurzel, uebersprungen, mit_ordner=True):
                if e.name != BEGLEIT_ORDNER:
                    kandidaten.append(e)
                elif plattform.ist_verweis_eintrag(e):
                    uebersprungen.append(_vermerk(Path(e.path), wurzel, SYMLINK, _typ_des_ziels(e)))
                elif e.is_dir(follow_symlinks=False):
                    kandidaten += _begleit_eintraege(Path(e.path), wurzel, uebersprungen)
    kandidaten = [e for e in kandidaten if e.name.lower() != SKILL_DATEI.lower()]
    dateien = byte = ausgelassen = 0
    for e in kandidaten:
        pfad = Path(e.path)
        if plattform.ist_verweis_eintrag(e):
            uebersprungen.append(_vermerk(pfad, wurzel, SYMLINK, _typ_des_ziels(e)))
            continue
        try:
            info = e.stat(follow_symlinks=False)
        except OSError:
            uebersprungen.append(_vermerk(pfad, wurzel, NICHT_LESBAR, DATEI))
            continue
        if not stat.S_ISREG(info.st_mode):
            uebersprungen.append(_vermerk(pfad, wurzel, NICHT_LESBAR, DATEI))
            continue
        if info.st_size > BEGLEIT_HOECHSTENS:
            uebersprungen.append(_vermerk(pfad, wurzel, GROESSE, DATEI))
            continue
        if dateien + 1 > BEGLEIT_DATEIEN or byte + info.st_size > BEGLEIT_BYTES:
            ausgelassen += 1
            continue
        inhalt = _lies_fuer_ki(pfad, wurzel, BEGLEIT_HOECHSTENS, uebersprungen)
        if inhalt is None:
            continue
        if byte + len(inhalt) > BEGLEIT_BYTES:  # seit dem Prüfen gewachsen
            ausgelassen += 1
            continue
        dateien, byte = dateien + 1, byte + len(inhalt)
        rel = pfad.relative_to(wurzel)
        try:
            (ziel / rel).parent.mkdir(parents=True, exist_ok=True)
            (ziel / rel).write_bytes(inhalt)
        except OSError as fehler:
            raise KopieFehler(f"Die Arbeitskopie für die KI ließ sich nicht anlegen ({fehler}).")
    if ausgelassen:
        uebersprungen.append({"grund": GRENZE, "typ": DATEI, "anzahl": ausgelassen})


def _begleit_eintraege(ordner: Path, wurzel: Path, uebersprungen: list[dict], mit_ordner: bool = False) \
        -> list[os.DirEntry]:
    """Die Einträge eines Ordners nach Namen, die als Begleitdatei in Frage kommen (Endung, nicht versteckt, kein
    Ordner), mit_ordner auch einen Eintrag BEGLEIT_ORDNER; lässt sich der Ordner nicht lesen, steht er in uebersprungen."""
    try:
        with os.scandir(ordner) as eintraege:
            liste = [e for e in eintraege if mit_ordner and e.name == BEGLEIT_ORDNER
                     or not e.name.startswith(".") and e.name.endswith(BEGLEIT_ENDUNGEN)
                     and (plattform.ist_verweis_eintrag(e) or not e.is_dir(follow_symlinks=False))]
    except OSError:
        uebersprungen.append(_vermerk(ordner, wurzel, NICHT_LESBAR, ORDNER))
        return []
    return sorted(liste, key=lambda e: e.name)


def zu_viel_hinter_symlink() -> str:
    return (f"Die Probe-Kopie des Kits ließ sich nicht anlegen: Hinter Symlinks im Kit liegen mehr als "
            f"{KOPIE_LINK_DATEIEN} Dateien oder {KOPIE_LINK_BYTES // (1024 * 1024)} MiB.")


def pruefe_ziel(wurzel: Path, rel: object) -> Path:
    """WS-X5: Zielpfad unter dem Kit, kein Symlink auf dem Weg und als Ziel. Gibt den Pfad zurück."""
    rel = pruefe_rel(rel)
    pfad = Path(wurzel)
    if plattform.ist_verweis(pfad):
        raise PfadFehler(f"{wurzel} ist ein Symlink; die Werkstatt schreibt dort nicht.")
    for teil in rel.split("/"):
        pfad = pfad / teil
        if plattform.ist_verweis(pfad):
            weg = pfad.relative_to(wurzel).as_posix()
            raise PfadFehler(f"{weg} ist ein Symlink; die Werkstatt schreibt dort nicht.")
    if not liegt_unter(Path(os.path.realpath(pfad)), Path(os.path.realpath(wurzel))):
        raise PfadFehler(f"{rel} liegt nicht im Kit.")
    return pfad


def sicherer_ordner(basis: Path, *teile: str) -> Path:
    """Legt basis/teile… an; weder basis noch ein Teil darf ein Symlink sein."""
    if plattform.ist_verweis(basis):
        raise PfadFehler(f"{basis} ist ein Symlink; die Werkstatt legt dort nichts ab.")
    basis.mkdir(parents=True, exist_ok=True)
    pfad = basis
    for teil in teile:
        pruefe_rel(teil)
        pfad = pfad / teil
        if plattform.ist_verweis(pfad):
            raise PfadFehler(f"{pfad} ist ein Symlink; die Werkstatt legt dort nichts ab.")
        pfad.mkdir(exist_ok=True)
    return pfad
