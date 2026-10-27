"""Suche für das Panel „Memory und Kontext“ (specs/kontext.yaml KX-F1 bis KX-F5, KX-W1).

Rein lesend: Memory je Agent, CLAUDE.md im Arbeitsordner, darüber und im Claude-Ordner, ihre @-Einbindungen und die
Begleitdateien der Skill. Inhalte liest die Suche nur, um Einbindungen zu finden und Text zu erkennen; sie gibt keine
Inhalte heraus. Nur Standardbibliothek.
"""
from __future__ import annotations

import ntpath
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePath

import plattform
import werkstatt_gedaechtnis as gd

GRENZE_BYTES = 256 * 1024
DATEIEN_HOECHSTENS = 200
GEFUNDEN_HOECHSTENS = 20 * DATEIEN_HOECHSTENS
VERWEISE_HOECHSTENS = 200
LESEN_NACH_GRENZE_BYTES = 4 * 1024 * 1024
VERWEISE_DURCHSUCHT_HOECHSTENS = 20_000
VERWEISE_JE_DATEI = 1000
EINBINDUNG_LESEN_BYTES = 8 * 1024 * 1024
TIEFE_HOECHSTENS = 5
CLAUDE = ".claude"
REFERENCES = "references"
SKILL_MD = "skill.md"
CLAUDE_MD_TEILE = (("CLAUDE.md",), (CLAUDE, "CLAUDE.md"), ("CLAUDE.local.md",))
TEXT_ENDUNGEN = (".md", ".markdown", ".txt")
BEGLEIT_ENDUNGEN = (".md", ".txt")
REICHWEITEN = ("kit", "ort", "ueber_ort", "alle_projekte", "ausserhalb")

GRUPPE_ALLE = "Für alle Projekte"
GRUPPE_UEBER = "Über dem Arbeitsordner"
GRUPPE_ORT = "Im Arbeitsordner"

GROSS = "Größer als 256 KiB; die Werkstatt zeigt und ändert sie nicht."
KEIN_TEXT = "Lässt sich nicht als Text lesen."
ENDUNG = "Nur Text-Dokumente (.md, .txt) ändert die Werkstatt."
VERSTECKT = "In versteckten Ordnern schreibt die Werkstatt nicht."
PLUGIN = "Diese Datei gehört zu einem installierten Plugin bzw. zur Werkstatt."
FORMULAR = "Diese Datei änderst du über das Agent-Formular bzw. den Ablauf."
KEINE_RECHTE = "keine Berechtigung"
KEINE_DATEI = "keine reguläre Datei"

MEMORY, CLAUDE_MD, EINGEBUNDEN, BEGLEIT = "memory", "claude_md", "eingebunden", "begleit"

ZAUN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CODE_SPANNE = re.compile(r"(`+)(?:(?!\1).)+?\1")
VERWEIS = re.compile(r"(?:(?<=\s)|^)@(\S+)")
SATZZEICHEN_AM_ENDE = ".,;:)"


def gruppe_begleit(skill: str) -> str:
    return f"Begleitdateien der Skill {skill}"


def symlink_grund(ziel: str) -> str:
    return f"Verweist auf {ziel}; über einen Symlink schreibt die Werkstatt nicht."


def nicht_lesbar(grund: str) -> str:
    return f"Lässt sich nicht lesen: {grund.rstrip('.')}."


@dataclass
class Fund:
    pfad: Path
    anker: Path
    teile: tuple[str, ...]
    datei: str
    reichweite: str
    gruppe: str | None
    agent: str | None
    memory: bool
    art: str = EINGEBUNDEN
    bereich: str | None = None
    begleit: str | None = None
    ebene: int = 0
    bytes: int = 0
    geaendert: str = ""
    bearbeitbar: bool = False
    grund: str | None = None
    eingebunden_von: Path | None = None
    einbindung: str | None = None
    auch_von: list[Path] = field(default_factory=list)
    schluessel: str = ""
    kein_text: bool = False
    lesefehler: str | None = None
    gelesen: bool = False
    gelesene_bytes: int = 0


@dataclass(frozen=True)
class Umgebung:
    home: Path
    claude_ordner: Path
    kit: Path
    kit_art: str
    kit_id: str
    ergebnis: Path
    schreibbar: bool
    skill_ordner: Path | None
    agent_dateien: frozenset[str]
    skill_dateien: frozenset[str]
    nur_gelesen: str

    @property
    def verboten(self) -> gd.Verboten:
        return gd.Verboten(self.home, self.claude_ordner, self.ergebnis)


@dataclass
class Suche:
    memory: list[dict] = field(default_factory=list)
    gruppen: list[tuple[str, list[Fund], int]] = field(default_factory=list)
    verweise: list[tuple[Path, str, str]] = field(default_factory=list)
    weitere: int = 0
    gekuerzt: bool = False


# ---------------------------------------------------------------- Pfade

def _ist_windows_pfad(pfad: PurePath | str) -> bool:
    return plattform.ist_windows() or isinstance(pfad, PurePath) and pfad.__class__.__name__.endswith("WindowsPath")


def schluessel(pfad: PurePath | str) -> str:
    text = str(pfad)
    if _ist_windows_pfad(pfad):
        return ntpath.normcase(ntpath.normpath(text.replace("/", "\\")))
    return os.path.normcase(os.path.normpath(text))


def _normalisiert(pfad: PurePath) -> PurePath:
    if _ist_windows_pfad(pfad):
        return type(pfad)(ntpath.normpath(str(pfad).replace("/", "\\")))
    return type(pfad)(os.path.normpath(str(pfad)))


def ist_wurzel(pfad: PurePath) -> bool:
    return pfad.parent == pfad


def ordner_ueber(ort: PurePath) -> list[PurePath]:
    return [p for p in ort.parents if not ist_wurzel(p)]


def _absolut(roh: str) -> bool:
    if plattform.ist_windows():
        return ntpath.isabs(roh)
    return os.path.isabs(roh)


def verweis_ziel(roh: str, ordner: PurePath, home: PurePath) -> PurePath:
    art = type(ordner)
    if roh == "~":
        return _normalisiert(art(home))
    if roh.startswith(("~/", "~\\")):
        return _normalisiert(art(home) / roh[2:])
    if _absolut(roh):
        return _normalisiert(art(roh))
    return _normalisiert(art(ordner) / roh)


def unter(pfad: PurePath, ordner: PurePath) -> bool:
    p, o = schluessel(pfad), schluessel(ordner)
    trenner = "\\" if _ist_windows_pfad(pfad) else os.sep
    return p == o or p.startswith(o.rstrip(trenner) + trenner)


def anzeige(pfad: PurePath, home: PurePath) -> str:
    text = str(pfad)
    h = str(home)
    if h and not ist_wurzel(PurePath(h)) and unter(pfad, home):
        rest = text[len(h):].replace("\\", "/").lstrip("/")
        return "~/" + rest if rest else "~"
    return text


def _relativ(pfad: PurePath, anker: PurePath) -> tuple[str, ...]:
    rest = schluessel(pfad)[len(schluessel(anker)):]
    anzahl = len([t for t in re.split(r"[\\/]+", rest) if t])
    return tuple(pfad.parts[len(pfad.parts) - anzahl:]) if anzahl else ()


# ---------------------------------------------------------------- Einbindungen

def einbindungen(text: str) -> list[str]:
    return list(_einbindungen(text))


def _einbindungen(text: str):
    zaun: str | None = None
    for zeile in text.splitlines():
        treffer = ZAUN.match(zeile)
        if zaun is not None:
            if treffer and treffer.group(1)[0] == zaun[0] and len(treffer.group(1)) >= len(zaun):
                zaun = None
            continue
        if treffer:
            zaun = treffer.group(1)
            continue
        ohne_code = CODE_SPANNE.sub(" ", zeile)
        for verweis in VERWEIS.finditer(ohne_code):
            roh = verweis.group(1).rstrip(SATZZEICHEN_AM_ENDE)
            if roh:
                yield roh


def claude_mds(ort: Path, claude_ordner: Path) -> tuple[list[Path], list[Path], list[Path]]:
    alle = [Path(claude_ordner) / "CLAUDE.md"]
    ueber = [Path(o).joinpath(*teile) for o in ordner_ueber(Path(ort)) for teile in CLAUDE_MD_TEILE]
    im_ort = [Path(ort).joinpath(*teile) for teile in CLAUDE_MD_TEILE]
    return alle, ueber, im_ort


def _ist_datei_oder_verweis(pfad: Path) -> bool:
    try:
        info = os.lstat(pfad)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) or plattform.ist_verweis_info(info)


def begleitdateien(skill_ordner: Path) -> list[Path]:
    gefunden: list[Path] = []
    for ordner in (Path(skill_ordner), Path(skill_ordner) / REFERENCES):
        try:
            namen = sorted(os.listdir(ordner))
        except OSError:
            continue
        for name in namen:
            if name.startswith(".") or name.casefold() == SKILL_MD or Path(name).suffix.lower() not in BEGLEIT_ENDUNGEN:
                continue
            if _ist_datei_oder_verweis(ordner / name):
                gefunden.append(ordner / name)
    return gefunden


def _memory_namen(ordner: Path) -> tuple[list[str], int]:
    try:
        namen = sorted(os.listdir(ordner))
    except OSError:
        return [], 0
    dateien, weitere = [], 0
    for name in namen:
        if gd.DATEI.fullmatch(name) and _ist_datei_oder_verweis(ordner / name):
            dateien.append(name)
        else:
            weitere += 1
    dateien.sort(key=lambda n: (n != "MEMORY.md", n))
    return dateien, weitere


# ---------------------------------------------------------------- Lesen

def _lies(pfad: Path) -> tuple[bytes | None, str | None]:
    try:
        info = os.stat(pfad)
    except PermissionError:
        return None, nicht_lesbar(KEINE_RECHTE)
    except OSError as fehler:
        return None, nicht_lesbar(fehler.strerror or KEINE_DATEI)
    if not stat.S_ISREG(info.st_mode):
        return None, nicht_lesbar(KEINE_DATEI)
    if info.st_size > GRENZE_BYTES:
        return None, GROSS
    try:
        fd = os.open(pfad, os.O_RDONLY | plattform.BINAER | getattr(os, "O_NONBLOCK", 0))
    except PermissionError:
        return None, nicht_lesbar(KEINE_RECHTE)
    except OSError as fehler:
        return None, nicht_lesbar(fehler.strerror or KEINE_DATEI)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, nicht_lesbar(KEINE_DATEI)
        return gd.lies_fd(fd, GRENZE_BYTES), None
    except ValueError:
        return None, GROSS
    except OSError as fehler:
        return None, nicht_lesbar(fehler.strerror or KEINE_DATEI)
    finally:
        os.close(fd)


def als_text(roh: bytes) -> str | None:
    if b"\x00" in roh:
        return None
    try:
        return roh.removeprefix(gd.BOM).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _lies_einmal(fund: Fund) -> str | None:
    if fund.gelesen:
        return None
    roh, fund.lesefehler = _lies(fund.pfad)
    fund.gelesen = True
    fund.gelesene_bytes = len(roh) if roh is not None else 0
    text = als_text(roh) if roh is not None else None
    fund.kein_text = roh is not None and text is None
    return text


# ---------------------------------------------------------------- Lage

def _anker_und_reichweite(pfad: Path, art: str, ort: Path | None, u: Umgebung,
                          ueber_ordner: Path | None = None) -> tuple[str, Path]:
    if unter(pfad, u.kit):
        return "kit", u.kit
    if art == CLAUDE_MD and ueber_ordner is not None:
        return "ueber_ort", ueber_ordner
    if ort is not None and unter(pfad, ort):
        return "ort", ort
    if unter(pfad, u.claude_ordner):
        return "alle_projekte", u.claude_ordner
    if unter(pfad, u.home):
        return "ausserhalb", u.home
    return "ausserhalb", type(pfad)(pfad.anchor)


def _neuer_fund(pfad: Path, art: str, ort: Path | None, u: Umgebung, *, gruppe: str | None = None,
                ueber_ordner: Path | None = None, **mehr) -> Fund:
    reichweite, anker = _anker_und_reichweite(pfad, art, ort, u, ueber_ordner)
    teile = _relativ(pfad.parent, anker)
    return Fund(pfad=pfad, anker=anker, teile=teile, datei=pfad.name, reichweite=reichweite, gruppe=gruppe,
                agent=mehr.pop("agent", None), memory=art == MEMORY, art=art, schluessel=schluessel(pfad), **mehr)


def _verweis_auf_dem_weg(fund: Fund) -> bool:
    pfad = Path(fund.anker)
    for teil in ("", *fund.teile, fund.datei):
        pfad = pfad / teil if teil else pfad
        try:
            if plattform.ist_verweis_info(os.lstat(pfad)):
                return True
        except OSError:
            return False
    return False


def _grund(fund: Fund, u: Umgebung) -> str | None:
    _lies_einmal(fund)
    if fund.lesefehler is not None:
        return fund.lesefehler
    if fund.kein_text:
        return KEIN_TEXT
    if _verweis_auf_dem_weg(fund):
        return symlink_grund(anzeige(Path(os.path.realpath(fund.pfad)), u.home))
    if fund.art == EINGEBUNDEN and fund.pfad.suffix.lower() not in TEXT_ENDUNGEN:
        return ENDUNG
    if any(t.startswith(".") and t != CLAUDE for t in fund.teile) \
            or any(t.casefold() == ".git" for t in fund.pfad.parts):
        return VERSTECKT
    verboten = gd.ordner_verboten(fund.pfad.parent, u.verboten)
    if verboten == gd.GRUND_GIT:
        return VERSTECKT
    if verboten is not None:
        return PLUGIN
    if fund.schluessel in u.agent_dateien or fund.schluessel in u.skill_dateien:
        return FORMULAR
    if not u.schreibbar:
        return u.nur_gelesen
    return None


def _metadaten(fund: Fund, u: Umgebung) -> None:
    try:
        info = os.stat(fund.pfad)
    except OSError:
        try:
            info = os.lstat(fund.pfad)
        except OSError:
            info = None
    if info is not None:
        fund.bytes = int(info.st_size)
        fund.geaendert = datetime.fromtimestamp(info.st_mtime).astimezone().isoformat(timespec="seconds")
    fund.grund = _grund(fund, u)
    fund.bearbeitbar = fund.grund is None


# ---------------------------------------------------------------- Suche

def memory(agenten: list[tuple[str, str | None, str | None]], ort: Path | None, u: Umgebung) -> tuple[list[dict], list[Fund]]:
    eintraege: list[dict] = []
    funde: list[Fund] = []
    for name, rolle, bereich in agenten:
        eintrag = {"agent": name, "rolle": rolle, "bereich": bereich, "ordner": None, "gibt_es": False, "dateien": [],
                   "weitere": 0}
        eintraege.append(eintrag)
        if bereich not in gd.BEREICHE:
            continue
        anker = Path(u.claude_ordner) if bereich == "user" else ort
        if anker is None:
            continue
        teile = (*gd.UNTERORDNER[bereich], gd.ordnername(u.kit_art, u.kit_id, name))
        ordner = Path(anker).joinpath(*teile)
        eintrag["ordner"] = ordner
        if not os.path.isdir(ordner):
            continue
        eintrag["gibt_es"] = True
        namen, eintrag["weitere"] = _memory_namen(ordner)
        reichweite = "alle_projekte" if bereich == "user" else "kit" if unter(ordner, u.kit) else "ort"
        for datei in namen:
            fund = Fund(pfad=ordner / datei, anker=Path(anker), teile=teile, datei=datei, reichweite=reichweite,
                        gruppe=None, agent=name, memory=True, art=MEMORY, bereich=bereich,
                        schluessel=schluessel(ordner / datei))
            eintrag["dateien"].append(fund)
            funde.append(fund)
    return eintraege, funde


class _Sammler:
    def __init__(self, ort: Path | None, u: Umgebung, gesehen: dict[str, Fund], anzahl: int):
        self.ort = ort
        self.u = u
        self.gesehen = gesehen
        self.identitaeten: dict[tuple[int, int], Fund] = {}
        self.anzahl = anzahl
        self.verweise: list[tuple[Fund, str, str]] = []
        self.verweise_gesehen: set[tuple[str, str, str]] = set()
        self.ueberzaehlig = 0
        self.budget = LESEN_NACH_GRENZE_BYTES
        self.lesen = EINBINDUNG_LESEN_BYTES
        self.verweise_frei = VERWEISE_DURCHSUCHT_HOECHSTENS
        self.gekuerzt = False
        for fund in gesehen.values():
            self._merke_identitaet(fund)

    def _merke_identitaet(self, fund: Fund) -> tuple[int, int] | None:
        try:
            ident = gd.identitaet(os.lstat(fund.pfad))
        except OSError:
            return None
        self.identitaeten.setdefault(ident, fund)
        return ident

    def _frueher(self, pfad: Path, key: str) -> Fund | None:
        frueher = self.gesehen.get(key)
        if frueher is not None:
            return frueher
        try:
            return self.identitaeten.get(gd.identitaet(os.lstat(pfad)))
        except OSError:
            return None

    def _zaehle(self, fund: Fund) -> bool:
        self.gesehen[fund.schluessel] = fund
        self._merke_identitaet(fund)
        self.anzahl += 1
        if self.anzahl <= DATEIEN_HOECHSTENS:
            return True
        try:
            groesse = min(os.stat(fund.pfad).st_size, GRENZE_BYTES)
        except OSError:
            return False
        if groesse > self.budget:
            self.budget = 0
            return False
        self.budget -= groesse
        return True

    def _verweis(self, fund: Fund, roh: str, grund: str) -> None:
        if len(self.verweise) >= VERWEISE_HOECHSTENS:
            return
        merkmal = (fund.schluessel, roh, grund)
        if merkmal in self.verweise_gesehen:
            return
        self.verweise_gesehen.add(merkmal)
        self.verweise.append((fund, roh, grund))

    def voll(self) -> bool:
        return len(self.gesehen) >= GEFUNDEN_HOECHSTENS

    def oben(self, pfad: Path, gruppe: str, ueber_ordner: Path | None = None) -> list[Fund]:
        if not _ist_datei_oder_verweis(pfad) or os.path.isdir(pfad):
            return []
        key = schluessel(pfad)
        if self._frueher(pfad, key) is not None:
            return []
        if self.voll():
            self.ueberzaehlig += 1
            return []
        fund = _neuer_fund(pfad, CLAUDE_MD, self.ort, self.u, gruppe=gruppe, ueber_ordner=ueber_ordner)
        liste = [fund]
        if self._zaehle(fund):
            self._folge(fund, (key,), liste)
        return liste

    def _folge(self, fund: Fund, kette: tuple[str, ...], liste: list[Fund]) -> None:
        if self.lesen <= 0 or self.verweise_frei <= 0:
            self.gekuerzt = True
            return
        text = _lies_einmal(fund)
        if text is None:
            return
        self.lesen -= fund.gelesene_bytes
        je_datei = VERWEISE_JE_DATEI
        for roh in _einbindungen(text):
            if self.verweise_frei <= 0 or je_datei <= 0:
                self.gekuerzt = True
                return
            self.verweise_frei -= 1
            je_datei -= 1
            ziel = Path(verweis_ziel(roh, fund.pfad.parent, self.u.home))
            key = schluessel(ziel)
            if key in kette:
                continue
            frueher = self._frueher(ziel, key)
            if frueher is not None:
                if frueher.schluessel in kette:
                    continue
                if frueher.eingebunden_von is not None and schluessel(frueher.eingebunden_von) != fund.schluessel \
                        and fund.pfad not in frueher.auch_von:
                    frueher.auch_von.append(fund.pfad)
                continue
            if fund.ebene + 1 > TIEFE_HOECHSTENS:
                self._verweis(fund, roh, "zu_tief")
                continue
            if not os.path.lexists(ziel) or os.path.isdir(ziel):
                self._verweis(fund, roh, "fehlt")
                continue
            if self.voll():
                self.ueberzaehlig += 1
                continue
            kind = _neuer_fund(ziel, EINGEBUNDEN, self.ort, self.u, gruppe=fund.gruppe, ebene=fund.ebene + 1,
                               eingebunden_von=fund.pfad, einbindung=roh)
            liste.append(kind)
            if self._zaehle(kind):
                self._folge(kind, (*kette, key), liste)


def _gekuerzt(funde: list[Fund], frei: int) -> tuple[list[Fund], int]:
    behalten = funde[:max(0, frei)]
    return behalten, len(funde) - len(behalten)


def suche(skill: str, ort: Path | None, agenten: list[tuple[str, str | None, str | None]], u: Umgebung) -> Suche:
    ergebnis = Suche()
    ergebnis.memory, memory_funde = memory(agenten, ort, u)
    gesehen: dict[str, Fund] = {f.schluessel: f for f in memory_funde}
    sammler = _Sammler(ort, u, gesehen, len(memory_funde))
    gruppen: list[tuple[str, list[Fund]]] = []
    alle, ueber, im_ort = claude_mds(ort, u.claude_ordner) if ort is not None else ([Path(u.claude_ordner) / "CLAUDE.md"], [], [])
    gruppen.append((GRUPPE_ALLE, [f for p in alle for f in sammler.oben(p, GRUPPE_ALLE)]))
    if ort is not None:
        oben: list[Fund] = []
        for pfad in ueber:
            ordner = pfad.parent.parent if pfad.parent.name == CLAUDE else pfad.parent
            oben += sammler.oben(pfad, GRUPPE_UEBER, ordner)
        if oben:
            gruppen.append((GRUPPE_UEBER, oben))
        gruppen.append((GRUPPE_ORT, [f for p in im_ort for f in sammler.oben(p, GRUPPE_ORT)]))
    begleit_name = gruppe_begleit(skill)
    begleit: list[Fund] = []
    if u.skill_ordner is not None:
        for pfad in begleitdateien(u.skill_ordner):
            key = schluessel(pfad)
            if key in gesehen:
                continue
            fund = _neuer_fund(pfad, BEGLEIT, ort, u, gruppe=begleit_name,
                               begleit=Path(os.path.relpath(pfad, u.skill_ordner)).as_posix())
            gesehen[key] = fund
            begleit.append(fund)
    gruppen.append((begleit_name, begleit))

    frei = DATEIEN_HOECHSTENS
    for eintrag in ergebnis.memory:
        eintrag["dateien"], eintrag["weitere_dateien"] = _gekuerzt(eintrag["dateien"], frei)
        frei -= len(eintrag["dateien"])
        ergebnis.weitere += eintrag["weitere_dateien"]
    for name, funde in gruppen:
        behalten, weg = _gekuerzt(funde, frei)
        frei -= len(behalten)
        ergebnis.weitere += weg
        ergebnis.gruppen.append((name, behalten, weg))
    ergebnis.weitere += sammler.ueberzaehlig
    for eintrag in ergebnis.memory:
        for fund in eintrag["dateien"]:
            _metadaten(fund, u)
    for _, funde, _ in ergebnis.gruppen:
        for fund in funde:
            _metadaten(fund, u)
    ergebnis.verweise = [(fund.pfad, roh, grund) for fund, roh, grund in sammler.verweise]
    ergebnis.gekuerzt = sammler.gekuerzt or sammler.ueberzaehlig > 0
    return ergebnis
