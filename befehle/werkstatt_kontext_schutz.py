"""Sicherheitskern für Dateien aus „Memory und Kontext“ (specs/kontext.yaml KX-S1, KX-S2, KX-S4, KX-S5, KX-WIN1).

Geschrieben, gelesen oder wiederhergestellt wird nur ein KontextZiel, das die Suche gebaut hat (bzw. das aus einer
beglaubigten Sicherung stammt), nie ein Pfad aus einer Anfrage. Die Prüfungen und das atomare Ersetzen kommen aus dem
Weg-Kern von werkstatt_gedaechtnis. Nur Standardbibliothek.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import ntpath
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

import plattform
import werkstatt_gedaechtnis as gd
import werkstatt_kontext_suche as suche
from werkstatt_pfade import PfadFehler

GRENZE_BYTES = suche.GRENZE_BYTES
PRAEFIX = "@kontext/"
KID = re.compile(r"^k[0-9a-f]{12}$")
SCHLUESSEL = re.compile(r"^@kontext/k[0-9a-f]{12}$")
LF, CRLF, GEMISCHT = "lf", "crlf", "gemischt"
BOM = gd.BOM

GRUND_GROSS = "sie ist größer als 256 KiB"
GRUND_VERSTECKT = "sie liegt in einem versteckten Ordner"
GRUND_ENDUNG = "sie ist kein Text-Dokument (.md, .txt)"
GRUND_DATEI_ANDERS = "die Datei ist nicht mehr dieselbe"
GRUND_UNGUELTIG = "das Ziel ist ungültig"
GRUND_NICHT_LESBAR = "sie lässt sich nicht lesen"
GRUND_FREMD = {gd.GRUND_PLUGINS: "sie liegt in einem Installationsordner von Plugins",
               gd.GRUND_ERGEBNIS: "sie liegt im Ergebnisordner der Werkstatt",
               gd.GRUND_GIT: "sie liegt unter .git"}
UNBEKANNT = "Diese Datei kennt die Werkstatt nicht mehr. Lad die Liste neu."


class KontextFehler(Exception):
    def __init__(self, text: str, status: int = 409):
        super().__init__(text)
        self.text = text
        self.status = status


@dataclass(frozen=True)
class KontextZiel:
    id: str
    anzeige: str
    reichweite: str
    memory: bool
    agent: str | None
    anker: Path
    anker_id: tuple[int, int]
    teile: tuple[str, ...]
    ordner: Path
    ordner_id: tuple[int, int]
    datei: str
    datei_id: tuple[int, int]
    einbindung: bool
    bereich: str | None = None
    begleit: str | None = None
    skill: str | None = None
    grund: str | None = None
    anker_anzeige: str | None = None
    datei_kennung: tuple[int, int] | None = None
    verweis_ziel_id: tuple[int, int] | None = None

    @property
    def pfad(self) -> Path:
        return Path(self.ordner) / self.datei

    @property
    def kennung(self) -> tuple[int, int]:
        return tuple(self.datei_kennung or self.datei_id)

    @property
    def bearbeitbar(self) -> bool:
        return self.grund is None


def schreibt_nicht(anzeige: str, grund: str) -> str:
    return f"In {anzeige} schreibt die Werkstatt nicht: {grund.rstrip('.')}."


def liest_nicht(grund: str) -> str:
    return f"Diese Datei liest die Werkstatt nicht: {grund.rstrip('.')}."


def seit_vorschau(anzeige: str) -> str:
    return f"{anzeige} hat sich seit der Vorschau geändert. Schau dir die Vorschau noch einmal an."


def gerade_geaendert(anzeige: str) -> str:
    return f"{anzeige} hat sich gerade geändert. Mach „Rückgängig“ noch einmal."


def ordner_weg(ordner: str) -> str:
    return f"Den Ordner {ordner} gibt es nicht mehr; die Werkstatt legt ihn nicht neu an."


def ordner_anders(ordner: str) -> str:
    return f"Der Ordner {ordner} ist nicht mehr derselbe; die Werkstatt stellt dort nichts wieder her."


def datei_weg(anzeige: str) -> str:
    return f"Die Datei {anzeige} gibt es nicht mehr; die Werkstatt legt sie nicht neu an."


_identitaet = gd.identitaet
_lstat = gd.lstat_oder_fehler


def _name_gueltig(name: object) -> bool:
    return isinstance(name, str) and name not in ("", ".", "..") and not any(z in name for z in ("/", "\\", "\x00")) \
        and not (plattform.ist_windows() and ":" in name)


def id_fuer(geheimnis: bytes, pfad: Path, ids: tuple) -> str:
    text = json.dumps([str(pfad), [list(i) for i in ids]], separators=(",", ":")).encode("utf-8")
    return "k" + hmac.new(geheimnis, text, hashlib.sha256).hexdigest()[:12]


def schluessel(kid: str) -> str:
    return f"{PRAEFIX}{kid}"


def ist_schluessel(wert: object) -> bool:
    return isinstance(wert, str) and bool(SCHLUESSEL.fullmatch(wert))


def kid_von(schluessel_: str) -> str:
    return schluessel_[len(PRAEFIX):]


def eigene_kennung(nachfolger: dict | None, pfad: Path, datei_id: tuple[int, int]) -> tuple[int, int]:
    return tuple((nachfolger or {}).get((suche.schluessel(pfad), tuple(datei_id)), datei_id))


def merke_nachfolger(nachfolger: dict, z: KontextZiel) -> None:
    info = _lstat(z.pfad)
    if info and stat.S_ISREG(info.st_mode) and not plattform.ist_verweis_info(info):
        nachfolger[(suche.schluessel(z.pfad), _identitaet(info))] = z.kennung


def ziel_aus(fund: suche.Fund, geheimnis: bytes, home: Path, skill: str | None = None,
             nachfolger: dict | None = None) -> KontextZiel | None:
    anker = Path(fund.anker)
    ordner = anker.joinpath(*fund.teile)
    infos = [_lstat(anker), _lstat(ordner), _lstat(ordner / fund.datei)]
    if not all(infos):
        return None
    anker_id, ordner_id, datei_id = (_identitaet(i) for i in infos)
    pfad = ordner / fund.datei
    kennung = eigene_kennung(nachfolger, pfad, datei_id)
    verweis_ziel_id = None
    if plattform.ist_verweis_info(infos[2]):
        try:
            verweis_ziel_id = _identitaet(os.stat(pfad))
        except OSError:
            verweis_ziel_id = None
    return KontextZiel(id=id_fuer(geheimnis, pfad, (anker_id, ordner_id, kennung)), anzeige=suche.anzeige(pfad, home),
                       reichweite=fund.reichweite, memory=fund.memory, agent=fund.agent, anker=anker,
                       anker_id=anker_id, teile=tuple(fund.teile), ordner=ordner, ordner_id=ordner_id,
                       datei=fund.datei, datei_id=datei_id, einbindung=fund.art == suche.EINGEBUNDEN,
                       bereich=fund.bereich, begleit=fund.begleit, skill=skill if fund.art == suche.BEGLEIT else None,
                       grund=fund.grund, anker_anzeige=suche.anzeige(anker, home), datei_kennung=kennung,
                       verweis_ziel_id=verweis_ziel_id)


def _statische_gruende(z: KontextZiel, verboten: gd.Verboten) -> str | None:
    if z.reichweite not in suche.REICHWEITEN or not _name_gueltig(z.datei) \
            or not all(_name_gueltig(t) for t in z.teile) \
            or suche.schluessel(Path(z.ordner)) != suche.schluessel(Path(z.anker).joinpath(*z.teile)) \
            or not os.path.isabs(str(z.anker)) and not ntpath.isabs(str(z.anker)):
        return GRUND_UNGUELTIG
    if any(t.startswith(".") and t != suche.CLAUDE for t in z.teile) \
            or any(t.casefold() == ".git" for t in Path(z.ordner).parts):
        return GRUND_VERSTECKT
    if z.einbindung and Path(z.datei).suffix.lower() not in suche.TEXT_ENDUNGEN:
        return GRUND_ENDUNG
    fremd = gd.ordner_verboten(Path(z.ordner), verboten)
    if fremd is not None:
        return GRUND_FREMD.get(fremd, fremd)
    return None


def pruefe(z: KontextZiel, verboten: gd.Verboten, *, nur_lesen: bool = False, wiederherstellen: bool = False) -> Path:
    def fehler(grund: str) -> KontextFehler:
        if nur_lesen and not wiederherstellen:
            return KontextFehler(liest_nicht(grund))
        return KontextFehler(schreibt_nicht(z.anzeige, grund))

    def auf_dem_weg(grund: str) -> KontextFehler:
        ordner = suche.anzeige(Path(z.ordner), verboten.home)
        if wiederherstellen and grund == gd.GRUND_FEHLT:
            return KontextFehler(ordner_weg(ordner))
        if wiederherstellen and grund == gd.GRUND_NICHT_DERSELBE:
            return KontextFehler(ordner_anders(ordner))
        return fehler(grund)

    grund = _statische_gruende(z, verboten)
    if grund is not None:
        raise fehler(grund)
    gd.pruefe_weg(Path(z.anker), z.anker_id, tuple(z.teile), z.ordner_id, auf_dem_weg)
    if not nur_lesen and not os.access(z.ordner, os.W_OK | os.X_OK):
        raise fehler(gd.GRUND_SCHREIBSCHUTZ)
    pfad = Path(z.ordner) / z.datei
    info = _lstat(pfad)
    if info is None:
        if wiederherstellen:
            raise KontextFehler(datei_weg(z.anzeige))
        raise fehler(gd.GRUND_DATEI_FEHLT)
    if info is False:
        raise fehler(gd.GRUND_DATEI)
    if plattform.ist_verweis_info(info):
        raise fehler(suche.symlink_grund(suche.anzeige(Path(os.path.realpath(pfad)), verboten.home)))
    if not stat.S_ISREG(info.st_mode):
        raise fehler(gd.GRUND_DATEI)
    if info.st_size > GRENZE_BYTES:
        if nur_lesen and not wiederherstellen:
            raise KontextFehler(suche.GROSS)
        raise fehler(GRUND_GROSS)
    if not wiederherstellen and _identitaet(info) != tuple(z.datei_id):
        raise fehler(GRUND_DATEI_ANDERS)
    return pfad


def _identitaet_pruefen(z: KontextZiel, wiederherstellen: bool):
    def pruefe_info(info: os.stat_result) -> None:
        if not wiederherstellen and _identitaet(info) != tuple(z.datei_id):
            raise ValueError(GRUND_DATEI_ANDERS)
    return pruefe_info


def _lies_ueber_weg(z: KontextZiel, ordner_fd: int | None, wiederherstellen: bool) -> bytes | None:
    try:
        return gd.lies_ueber_weg(ordner_fd, z.pfad, z.datei, GRENZE_BYTES, _identitaet_pruefen(z, wiederherstellen))
    except ValueError as grund:
        raise ValueError(GRUND_GROSS if str(grund) == gd.ZU_GROSS else str(grund))


def _weg_fehler(z: KontextZiel, verboten: gd.Verboten, nur_lesen: bool):
    def fehler(grund: str) -> KontextFehler:
        return KontextFehler(liest_nicht(grund) if nur_lesen else schreibt_nicht(z.anzeige, grund))
    return fehler


def lies(z: KontextZiel, verboten: gd.Verboten, *, wiederherstellen: bool = False) -> bytes:
    pruefe(z, verboten, nur_lesen=True, wiederherstellen=wiederherstellen)
    fehler = _weg_fehler(z, verboten, not wiederherstellen)
    ordner_fd = None if plattform.ist_windows() else gd.oeffne_weg(Path(z.anker), z.anker_id, tuple(z.teile),
                                                                    z.ordner_id, fehler)
    try:
        inhalt = _lies_ueber_weg(z, ordner_fd, wiederherstellen)
    except PermissionError:
        raise fehler(f"{GRUND_NICHT_LESBAR} (keine Berechtigung)")
    except ValueError as grund:
        raise fehler(str(grund))
    except OSError:
        raise fehler(gd.GRUND_DATEI)
    finally:
        if ordner_fd is not None:
            os.close(ordner_fd)
    if inhalt is None:
        raise fehler(gd.GRUND_DATEI_FEHLT)
    return inhalt


def lies_zur_anzeige(z: KontextZiel, verboten: gd.Verboten) -> bytes:
    pfad = z.pfad
    anker = _lstat(Path(z.anker))
    info = _lstat(pfad)
    if not anker or _identitaet(anker) != tuple(z.anker_id):
        raise KontextFehler(liest_nicht(gd.GRUND_NICHT_DERSELBE))
    if info is None:
        raise KontextFehler(liest_nicht(gd.GRUND_DATEI_FEHLT))
    if info is False or _identitaet(info) != tuple(z.datei_id):
        raise KontextFehler(liest_nicht(GRUND_DATEI_ANDERS))
    try:
        fd = os.open(pfad, os.O_RDONLY | plattform.BINAER | getattr(os, "O_NONBLOCK", 0))
    except PermissionError:
        raise KontextFehler(liest_nicht(f"{GRUND_NICHT_LESBAR} (keine Berechtigung)"))
    except OSError:
        raise KontextFehler(liest_nicht(gd.GRUND_DATEI))
    try:
        ziel = os.fstat(fd)
        if not stat.S_ISREG(ziel.st_mode):
            raise KontextFehler(liest_nicht(gd.GRUND_DATEI))
        if plattform.ist_verweis_info(info):
            if z.verweis_ziel_id is None or _identitaet(ziel) != tuple(z.verweis_ziel_id):
                raise KontextFehler(liest_nicht(GRUND_DATEI_ANDERS))
        elif not os.path.samestat(ziel, info):
            raise KontextFehler(liest_nicht(GRUND_DATEI_ANDERS))
        if ziel.st_size > GRENZE_BYTES:
            raise KontextFehler(suche.GROSS)
        return gd.lies_fd(fd, GRENZE_BYTES)
    except ValueError:
        raise KontextFehler(suche.GROSS)
    except PermissionError:
        raise KontextFehler(liest_nicht(f"{GRUND_NICHT_LESBAR} (keine Berechtigung)"))
    except OSError:
        raise KontextFehler(liest_nicht(gd.GRUND_DATEI))
    finally:
        os.close(fd)


def _identitaeten_jetzt(z: KontextZiel, verboten: gd.Verboten, wiederherstellen: bool, mit_datei: bool) -> None:
    fehler = _weg_fehler(z, verboten, False)
    gd.weg_unveraendert(Path(z.anker), z.anker_id, tuple(z.teile), z.ordner_id, fehler)
    grund = _statische_gruende(z, verboten)
    if grund is not None:
        raise fehler(grund)
    if not mit_datei:
        return
    info = _lstat(Path(z.ordner) / z.datei)
    if not info or plattform.ist_verweis_info(info) or not stat.S_ISREG(info.st_mode):
        raise fehler(gd.GRUND_DATEI)
    if info.st_size > GRENZE_BYTES:
        raise fehler(GRUND_GROSS)
    if not wiederherstellen and _identitaet(info) != tuple(z.datei_id):
        raise fehler(GRUND_DATEI_ANDERS)


def _aktuell(z: KontextZiel, ordner_fd: int | None, wiederherstellen: bool) -> bytes | None:
    try:
        return _lies_ueber_weg(z, ordner_fd, wiederherstellen)
    except (OSError, ValueError):
        return None


def schreibe(z: KontextZiel, inhalt: bytes, verboten: gd.Verboten, modus: int | None = None, *,
             erwartet: bytes, wiederherstellen: bool = False) -> None:
    pfad = pruefe(z, verboten, wiederherstellen=wiederherstellen)
    if len(inhalt) > GRENZE_BYTES:
        raise KontextFehler(schreibt_nicht(z.anzeige, GRUND_GROSS))
    vorhanden = os.lstat(pfad)
    modus = stat.S_IMODE(vorhanden.st_mode) if modus is None else modus
    fehler = _weg_fehler(z, verboten, False)
    ordner_fd = None if plattform.ist_windows() else gd.oeffne_weg(Path(z.anker), z.anker_id, tuple(z.teile),
                                                                    z.ordner_id, fehler)

    def davor() -> None:
        _identitaeten_jetzt(z, verboten, wiederherstellen, mit_datei=True)
        if _aktuell(z, ordner_fd, wiederherstellen) != erwartet:
            raise KontextFehler(gerade_geaendert(z.anzeige) if wiederherstellen else seit_vorschau(z.anzeige))

    try:
        gd.ersetze_atomar(Path(z.ordner), ordner_fd, z.datei, inhalt, modus, davor,
                          lambda: KontextFehler(gd.geoeffnet(pfad, verboten.home)))
    finally:
        if ordner_fd is not None:
            os.close(ordner_fd)
    if ordner_fd is None:
        _identitaeten_jetzt(z, verboten, True, mit_datei=False)


def modus_von(z: KontextZiel) -> int | None:
    info = _lstat(Path(z.ordner) / z.datei)
    return stat.S_IMODE(info.st_mode) if info and stat.S_ISREG(info.st_mode) else None


def sicherungsfelder(z: KontextZiel, modus: int | None) -> dict:
    return {"ziel": "kontext", "reichweite": z.reichweite, "anzeige": z.anzeige, "anker": str(z.anker),
            "anker_id": list(z.anker_id), "teile": list(z.teile), "ordner": str(z.ordner),
            "ordner_id": list(z.ordner_id), "datei": z.datei, "datei_id": list(z.kennung), "modus": modus,
            "memory": z.memory, "agent": z.agent, "bereich": z.bereich, "einbindung": z.einbindung,
            "begleit": z.begleit, "skill": z.skill, "anker_anzeige": z.anker_anzeige}


def _paar(wert: object) -> tuple[int, int]:
    if not isinstance(wert, list) or len(wert) != 2 or not all(isinstance(x, int) and not isinstance(x, bool)
                                                               for x in wert):
        raise ValueError("Identität")
    return (wert[0], wert[1])


def _text_oder_none(wert: object) -> str | None:
    if wert is None or isinstance(wert, str):
        return wert
    raise ValueError("Text")


def aus_sicherung(werte: dict, schluessel_: str, geheimnis: bytes) -> KontextZiel:
    try:
        if not ist_schluessel(schluessel_) or werte.get("ziel") != "kontext":
            raise ValueError("Schlüssel")
        kid = kid_von(schluessel_)
        anker = Path(werte["anker"])
        teile = werte["teile"]
        if not isinstance(werte["anker"], str) or not isinstance(teile, list) \
                or not all(_name_gueltig(t) for t in teile) or not _name_gueltig(werte["datei"]):
            raise ValueError("Pfad")
        ordner = anker.joinpath(*teile)
        if not isinstance(werte["ordner"], str) or suche.schluessel(Path(werte["ordner"])) != suche.schluessel(ordner):
            raise ValueError("Ordner")
        ziel = KontextZiel(id=kid, anzeige=str(werte["anzeige"]), reichweite=str(werte["reichweite"]),
                           memory=werte.get("memory") is True, agent=_text_oder_none(werte.get("agent")),
                           anker=anker, anker_id=_paar(werte["anker_id"]), teile=tuple(teile), ordner=ordner,
                           ordner_id=_paar(werte["ordner_id"]), datei=werte["datei"],
                           datei_id=_paar(werte["datei_id"]), einbindung=werte.get("einbindung") is True,
                           bereich=_text_oder_none(werte.get("bereich")), begleit=_text_oder_none(werte.get("begleit")),
                           skill=_text_oder_none(werte.get("skill")),
                           anker_anzeige=_text_oder_none(werte.get("anker_anzeige")),
                           datei_kennung=_paar(werte["datei_id"]))
    except (KeyError, TypeError, ValueError):
        raise KontextFehler(UNBEKANNT)
    erwartet = id_fuer(geheimnis, ordner / ziel.datei, (ziel.anker_id, ziel.ordner_id, ziel.datei_id))
    if ziel.reichweite not in suche.REICHWEITEN or not hmac.compare_digest(erwartet, kid):
        raise KontextFehler(UNBEKANNT)
    return ziel


def pfad_im_kit(kit: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or "\\" in rel or ntpath.isabs(rel):
        raise PfadFehler(f"{rel} liegt nicht im Kit.")
    teile = rel.split("/")
    if not all(_name_gueltig(t) for t in teile) or any(t.startswith(".") and t != suche.CLAUDE for t in teile[:-1]) \
            or Path(teile[-1]).suffix.lower() not in suche.TEXT_ENDUNGEN:
        raise PfadFehler(f"{rel} ist keine Datei, die die Werkstatt aus „Memory und Kontext“ speichert.")
    pfad = Path(kit)
    for teil in teile:
        pfad = pfad / teil
        info = _lstat(pfad)
        if info and plattform.ist_verweis_info(info):
            raise PfadFehler(f"{rel} liegt hinter einem Symlink.")
    return pfad


def zeilenende_von(text: bytes) -> str:
    crlf = text.count(b"\r\n")
    lf = text.count(b"\n")
    if crlf == 0:
        return LF
    return CRLF if crlf == lf else GEMISCHT


def text_von(roh: bytes) -> tuple[str, bool, str]:
    bom = roh.startswith(BOM)
    rumpf = roh[len(BOM):] if bom else roh
    return rumpf.decode("utf-8").replace("\r\n", "\n"), bom, zeilenende_von(rumpf)


def bytes_von(text: str, bom: bool, zeilenende: str) -> bytes:
    rumpf = text.replace("\r\n", "\n")
    if zeilenende == CRLF:
        rumpf = rumpf.replace("\n", "\r\n")
    return (BOM if bom else b"") + rumpf.encode("utf-8")
