"""Frontmatter trennen und sicher mit PyYAML lesen (specs/yaml-lesen.yaml).

Gelesen wird mit yaml.compose und SafeLoader: Das liefert Knoten mit Positionen,
aber erzeugt noch keine Python-Objekte. Die Umwandlung in Werte macht dieses
Modul selbst. So werden doppelte Schlüssel (SC-Y5) und unbekannte Tags (SC-Y1)
mit Zeile erkannt, und jede Zeile kommt vom Parser, nicht aus einer Schätzung
(SC-Y7).
"""
from __future__ import annotations

import datetime
from pathlib import Path

import yaml

from befunde import Befunde
from textdatei import lies_zeilen

_STD = "tag:yaml.org,2002:"
SKALAR_TAGS = {_STD + t for t in ("str", "int", "float", "bool", "null", "timestamp")}
MAP_TAG = _STD + "map"
SEQ_TAG = _STD + "seq"


class _NichtLesbar(Exception):
    def __init__(self, text: str, zeile: int | None):
        super().__init__(text)
        self.text = text
        self.zeile = zeile


def _tag_fehler(tag: str, zeile: int) -> _NichtLesbar:
    return _NichtLesbar(f"Frontmatter nicht lesbar: der YAML-Tag {tag} wird nicht gelesen "
                        f"(nur sicheres YAML)", zeile)


class Frontmatter:
    """Gelesenes Frontmatter einer Datei mit Zeilen je Schlüsselpfad."""

    def __init__(self, datei: str, werte: dict, positionen: dict, start: int, text_ab: int,
                 zeilen: list[str], schluessel_oben: set, ids: dict | None = None):
        self.datei = datei
        self.werte = werte
        self._positionen = positionen      # Pfad-Tupel -> Zeile in der Datei
        self._ids = ids or {}              # Pfad eines Listeneintrags -> seine id (pfad_kennung)
        self.start = start                 # Zeile der ersten "---"
        self.text_ab = text_ab             # erste Zeile unterhalb des Frontmatters
        self.zeilen = zeilen               # alle Zeilen der Datei (textdatei.zerlege)
        # Schlüssel der obersten Ebene, wie sie im Text stehen, auch doppelte (SC-Y5),
        # die in werte fehlen. Dient kit.konvention ("auch ein fehlerhaftes atlas-Feld").
        self.schluessel_oben = schluessel_oben

    def zeile(self, *schluessel) -> int:
        """Zeile des Schlüssels (bei Listeneinträgen: des Eintrags) in der Datei.

        Gibt es den Pfad nicht, die Zeile des nächsten vorhandenen Elternpfads.
        """
        pfad = tuple(schluessel)
        while pfad:
            if pfad in self._positionen:
                return self._positionen[pfad]
            pfad = pfad[:-1]
        return self._positionen.get((), self.start)

    def hole(self, *schluessel):
        """Wert unter dem Pfad oder None, wenn er fehlt."""
        wert = self.werte
        for s in schluessel:
            if isinstance(wert, dict) and s in wert:
                wert = wert[s]
            elif isinstance(wert, list) and isinstance(s, int) and 0 <= s < len(wert):
                wert = wert[s]
            else:
                return None
        return wert

    def hat(self, *schluessel) -> bool:
        """True, wenn der Pfad existiert und sein Wert nicht null ist."""
        return self.hole(*schluessel) is not None

    def kennung(self, pfad: tuple) -> str:
        """Der Pfad als aussage eines Befunds (pfad_kennung)."""
        return pfad_kennung(pfad, self._ids)


def pfad_text(pfad: tuple) -> str:
    """("atlas", "schritte", 0, "titel") -> "atlas.schritte[1].titel" (Listen ab 1)."""
    teile = ""
    for s in pfad:
        if isinstance(s, int):
            teile += f"[{s + 1}]"
        else:
            teile += ("." if teile else "") + str(s)
    return teile


def pfad_kennung(pfad: tuple, ids: dict) -> str:
    """Wie pfad_text, aber ohne Index, der sich verschiebt, wenn davor ein Eintrag dazukommt: ein Listeneintrag mit id
    heißt [id=<id>], einer ohne []. ("atlas", "schritte", 1, "titel") -> "atlas.schritte[id=s2].titel"."""
    teile = ""
    for n, s in enumerate(pfad):
        if isinstance(s, int):
            sid = ids.get(pfad[:n + 1])
            teile += "[]" if sid is None else f"[id={sid}]"
        else:
            teile += ("." if teile else "") + str(s)
    return teile


def _id_von(node) -> str | None:
    """Die id eines Listeneintrags: der Text unter dem einzigen Schlüssel id, sonst None."""
    if not isinstance(node, yaml.MappingNode):
        return None
    werte = [v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.tag == _STD + "str" and k.value == "id"]
    if len(werte) != 1 or not isinstance(werte[0], yaml.ScalarNode) or werte[0].tag != _STD + "str":
        return None
    return werte[0].value


def wert_text(wert) -> str:
    """Kurze deutsche Beschreibung eines Werts für Befund-Texte."""
    if isinstance(wert, bool):
        return f"Wahrheitswert {'true' if wert else 'false'}"
    if isinstance(wert, int):
        return f"Zahl {wert}"
    if isinstance(wert, float):
        return f"Kommazahl {wert}"
    if isinstance(wert, (datetime.date, datetime.datetime)):
        return f"Datum {wert.isoformat()}"
    if isinstance(wert, str):
        return f"Text '{wert}'"
    if isinstance(wert, list):
        return f"Liste {wert}"
    if isinstance(wert, dict):
        return f"Abbildung {wert}"
    return repr(wert)


# ---------------------------------------------------------------- lesen

def _trenne(zeilen: list[str]) -> tuple[int, int] | None:
    """Index der öffnenden und der schließenden '---'-Zeile (0-basiert), sonst None."""
    if not zeilen or zeilen[0] != "---":
        return None
    for i in range(1, len(zeilen)):
        if zeilen[i] == "---":
            return 0, i
    return None


class _Umwandler:
    """Wandelt YAML-Knoten in Python-Werte und merkt sich die Zeile je Schlüsselpfad."""

    def __init__(self, versatz: int, befunde: Befunde, datei: str):
        self.versatz = versatz              # YAML-Zeile 0 = Dateizeile versatz
        self.befunde = befunde
        self.datei = datei
        self.positionen: dict = {}
        self.schluessel_oben: set = set()
        self.ids: dict = {}                 # Pfad eines Listeneintrags -> seine id (pfad_kennung)
        self._konstr = yaml.SafeLoader("")

    def _zeile(self, node) -> int:
        return node.start_mark.line + self.versatz

    def wandle(self, node, pfad: tuple = (), eintrag: bool = True):
        zeile = self._zeile(node)
        if eintrag:
            self.positionen[pfad] = zeile
        if isinstance(node, yaml.ScalarNode):
            if node.tag not in SKALAR_TAGS:
                raise _tag_fehler(node.tag, zeile)
            return self._konstr.construct_object(node)
        if isinstance(node, yaml.SequenceNode):
            if node.tag != SEQ_TAG:
                raise _tag_fehler(node.tag, zeile)
            for i, kind in enumerate(node.value):
                sid = _id_von(kind)
                if sid is not None:
                    self.ids[pfad + (i,)] = sid
            return [self.wandle(kind, pfad + (i,)) for i, kind in enumerate(node.value)]
        if isinstance(node, yaml.MappingNode):
            if node.tag != MAP_TAG:
                raise _tag_fehler(node.tag, zeile)
            return self._abbildung(node, pfad)
        raise _NichtLesbar("Frontmatter nicht lesbar: unbekannte YAML-Form", zeile)

    def _abbildung(self, node, pfad: tuple) -> dict:
        ergebnis: dict = {}
        erste_zeile: dict = {}
        doppelt: set = set()
        for k_node, v_node in node.value:
            k_zeile = self._zeile(k_node)
            if not isinstance(k_node, yaml.ScalarNode):
                raise _NichtLesbar("Frontmatter nicht lesbar: ein Schlüssel ist keine "
                                   "einfache Angabe", k_zeile)
            if k_node.tag not in SKALAR_TAGS:
                raise _tag_fehler(k_node.tag, k_zeile)
            schluessel = self._konstr.construct_object(k_node)
            if not pfad:
                self.schluessel_oben.add(schluessel)
            if schluessel in erste_zeile:
                self.befunde.melde(
                    "widerspruch",
                    f"Der Schlüssel {pfad_text(pfad + (schluessel,))} steht doppelt (Zeilen "
                    f"{erste_zeile[schluessel]} und {k_zeile}); keiner der Werte wird übernommen",
                    self.datei, k_zeile, aussage=pfad_kennung(pfad + (schluessel,), self.ids))
                doppelt.add(schluessel)
                continue
            erste_zeile[schluessel] = k_zeile
            self.positionen[pfad + (schluessel,)] = k_zeile
            ergebnis[schluessel] = self.wandle(v_node, pfad + (schluessel,), eintrag=False)
        for s in doppelt:
            ergebnis.pop(s, None)
        return ergebnis


def lies_frontmatter(kit: Path, datei: str, befunde: Befunde) -> Frontmatter | None:
    """Liest das Frontmatter von kit/datei. Bei jedem Lesefehler: Befund nicht_lesbar, None."""
    try:
        zeilen = lies_zeilen(kit, datei)
    except UnicodeDecodeError:
        befunde.melde("nicht_lesbar", f"{datei} ist nicht als UTF-8 lesbar; "
                                      f"das Frontmatter wird nicht gelesen", datei, None, aussage="datei")
        return None
    except OSError as e:
        befunde.melde("nicht_lesbar", f"{datei} lässt sich nicht öffnen ({e.strerror})",
                      datei, None, aussage="datei")
        return None
    grenzen = _trenne(zeilen)
    if grenzen is None:
        if not zeilen or zeilen[0] != "---":
            grund = "die erste Zeile ist nicht '---'"
        else:
            grund = "die schließende Zeile '---' fehlt"
        befunde.melde("nicht_lesbar", f"Frontmatter nicht lesbar: {grund}", datei, 1, aussage="frontmatter")
        return None
    anfang, ende = grenzen
    versatz = anfang + 2      # YAML-Zeile 0 = Dateizeile anfang + 2
    yaml_text = "\n".join(zeilen[anfang + 1:ende])
    try:
        wurzel = yaml.compose(yaml_text, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as e:
        marke = e.problem_mark or e.context_mark
        zeile = marke.line + versatz if marke is not None else None
        grund = e.problem or e.context or "YAML-Fehler"
        befunde.melde("nicht_lesbar", f"Frontmatter nicht lesbar: YAML-Fehler '{grund}'",
                      datei, zeile, aussage="frontmatter")
        return None
    except yaml.YAMLError as e:
        befunde.melde("nicht_lesbar", f"Frontmatter nicht lesbar: YAML-Fehler '{e}'",
                      datei, None, aussage="frontmatter")
        return None

    umwandler = _Umwandler(versatz, befunde, datei)
    if wurzel is None:
        werte = {}
    else:
        try:
            werte = umwandler.wandle(wurzel)
        except _NichtLesbar as e:
            befunde.melde("nicht_lesbar", e.text, datei, e.zeile, aussage="frontmatter")
            return None
        except yaml.YAMLError as e:
            befunde.melde("nicht_lesbar", f"Frontmatter nicht lesbar: YAML-Fehler '{e}'",
                          datei, wurzel.start_mark.line + versatz, aussage="frontmatter")
            return None
        if not isinstance(werte, dict):
            befunde.melde("nicht_lesbar",
                          f"Frontmatter nicht lesbar: es ist keine Abbildung, sondern "
                          f"{'eine Liste' if isinstance(werte, list) else 'ein einzelner Wert'}",
                          datei, wurzel.start_mark.line + versatz, aussage="frontmatter")
            return None
    return Frontmatter(datei, werte, umwandler.positionen, anfang + 1, ende + 2,
                       zeilen, umwandler.schluessel_oben, umwandler.ids)


# ---------------------------------------------------------------- Typprüfung (SC-Y6)

def _form_falsch(fm: Frontmatter, pfad: tuple, erwartet: str, wert, befunde: Befunde,
                 folge: str) -> None:
    befunde.melde("nicht_unterstuetzt",
                  f"{pfad_text(pfad)}: erwartet {erwartet}, gefunden {wert_text(wert)}; {folge}",
                  fm.datei, fm.zeile(*pfad), aussage=fm.kennung(pfad))


def text_feld(fm: Frontmatter, pfad: tuple, befunde: Befunde) -> str | None:
    """Wert als Text. Fehlt er oder ist er null: None ohne Befund. Andere Form: Befund, None."""
    wert = fm.hole(*pfad)
    if wert is None or isinstance(wert, str):
        return wert
    _form_falsch(fm, pfad, "Text", wert, befunde, "der Wert wird nicht übernommen")
    return None


def listen_feld(fm: Frontmatter, pfad: tuple, befunde: Befunde,
                komma_string: bool = False) -> list[str] | None:
    """Liste von Texten. komma_string: auch ein Komma-getrennter Text ist erlaubt.

    Fehlt der Wert: None ohne Befund. Andere Form: Befund, None.
    """
    wert = fm.hole(*pfad)
    if wert is None:
        return None
    if komma_string and isinstance(wert, str):
        return [t.strip() for t in wert.split(",") if t.strip()]
    if isinstance(wert, list) and all(isinstance(t, str) for t in wert):
        return [t.strip() for t in wert]
    erwartet = "Liste von Texten oder Komma-getrennter Text" if komma_string else "Liste von Texten"
    _form_falsch(fm, pfad, erwartet, wert, befunde, "der Wert wird nicht übernommen")
    return None


def ganzzahl_feld(fm: Frontmatter, pfad: tuple, befunde: Befunde, minimum: int | None = None,
                  folge: str = "der Wert wird nicht übernommen") -> int | None:
    """Ganze Zahl (kein Wahrheitswert), optional mit Untergrenze. folge: Satz im Befund."""
    wert = fm.hole(*pfad)
    if wert is None:
        return None
    if isinstance(wert, int) and not isinstance(wert, bool) and (minimum is None or wert >= minimum):
        return wert
    erwartet = "ganze Zahl" if minimum is None else f"ganze Zahl ≥ {minimum}"
    _form_falsch(fm, pfad, erwartet, wert, befunde, folge)
    return None
