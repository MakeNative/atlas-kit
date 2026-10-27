"""Form eines Änderungssatzes atlas-aenderung/0.1 prüfen (specs/werkstatt.yaml WS-F1 bis WS-F3).

Nur die Form: Felder, Typen, Namen, Längen. Ob ein genannter Agent oder Schritt existiert, prüft der
Schreiber gegen den Stand des Kits (WS-F4, WS-F5). Nur Standardbibliothek; das Vokabular kommt aus
atlas/vokabular.py (der Server hat atlas/ im sys.path).
"""
from __future__ import annotations

import re
import unicodedata

import werkstatt_pfade as pfade
from hooks import AGENT_EREIGNISSE, EREIGNISSE, WERKZEUG_EREIGNISSE
from ordner import HOOK_ORDNER
from vokabular import AUSFUEHRENDE, MENSCH, ROLLEN, SCHRITT_ID, WERKZEUG_PFAD

SCHEMA = "atlas-aenderung/0.1"
HERKUNFT = ("hand", "ki")
EINTRAEGE_HOECHSTENS = 200
TEXT_HOECHSTENS = 20000

# Werkstatt-eigene Muster; das Vokabular des Bauplans kommt aus atlas/vokabular.py.
EINTRAG_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
WERKZEUG_DATEINAME = re.compile(r"^tools/[A-Za-z0-9_-]+\.py$")
MODELL = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
TEXT_DATEI = re.compile(r"^(?:\.claude/)?(?:skills/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}/SKILL\.md|"
                        r"agents/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}\.md)$")

PFLICHT, KANN = "pflicht", "kann"
EINTRAG_FELDER = ("id", "beschreibung", "gruppe")
ZITAT_HOECHSTENS = 300
BELEG_ARTEN = ("schritt", *AUSFUEHRENDE, "mensch", "zurueck")
NUR_ABLEITUNG = ("nur_ableitung", "belege")
# Claude Code liest einen Agenten ohne tools-Zeile als „alle Werkzeuge“; eine leere Liste hieße also das Gegenteil.
KEINE_WERKZEUGE = "Wähl mindestens ein Werkzeug. Ohne Angabe darf der Agent alles."
MEMORY_BEREICHE = ("project", "local", "user")
MEMORY_UNGUELTIG = "memory muss project, local oder user sein."
NUR_AUS_AUSWERTUNG = "Memory-Einträge entfernt die Werkstatt nur aus einer Auswertung."
NICHT_GEMISCHT = "Ein Satz mit Memory-Einträgen enthält keine anderen Änderungen."
GEDAECHTNIS_ID = re.compile(r"^g[0-9a-f]{12}$")
GEDAECHTNIS_DATEI = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
KONTEXT_ID = re.compile(r"^k[0-9a-f]{12}$")
KONTEXT_INHALT_BYTES = 256 * 1024
NUR_AUS_PANEL = "Dateien des Kontexts ändert die Werkstatt nur aus dem Panel „Memory und Kontext“."
NUR_EINE_DATEI = "Ein Satz mit datei_ersetzen ändert genau eine Datei und enthält keine andere Änderung."
MATCHER_HOECHSTENS = 200
TIMEOUT_HOECHSTENS = 600


class SatzFehler(Exception):
    def __init__(self, meldungen: list[str]):
        super().__init__("; ".join(meldungen))
        self.meldungen = meldungen


def _steuerzeichen(text: str, mit_umbruch: bool) -> bool:
    for zeichen in text:
        if mit_umbruch and zeichen in "\n\t":
            continue
        if unicodedata.category(zeichen) in ("Cc", "Cf", "Zl", "Zp"):
            return True
    return False


class _Pruefer:
    def __init__(self, hook_ordner: str | None = None) -> None:
        self.meldungen: list[str] = []
        self.hook_ordner = hook_ordner

    def hook_skript_erlaubt(self, wert: object) -> bool:
        ordner = (self.hook_ordner,) if self.hook_ordner is not None else tuple(HOOK_ORDNER.values())
        return any(pfade.ist_hook_skript(wert, o) for o in ordner)

    def fehler(self, ort: str, text: str) -> None:
        self.meldungen.append(f"{ort}: {text}")

    def zeile(self, ort: str, wert: object, hoechstens: int, leer_erlaubt: bool = False) -> bool:
        if not isinstance(wert, str):
            self.fehler(ort, "erwartet einen Text")
            return False
        if (not wert.strip() and not leer_erlaubt) or len(wert) > hoechstens:
            self.fehler(ort, f"erwartet 1 bis {hoechstens} Zeichen")
            return False
        if _steuerzeichen(wert, mit_umbruch=False):
            self.fehler(ort, "eine Zeile ohne Steuerzeichen")
            return False
        return True

    def langtext(self, ort: str, wert: object) -> bool:
        if not isinstance(wert, str) or len(wert) > TEXT_HOECHSTENS:
            self.fehler(ort, f"erwartet einen Text mit höchstens {TEXT_HOECHSTENS} Zeichen")
            return False
        if _steuerzeichen(wert, mit_umbruch=True):
            self.fehler(ort, "enthält Steuerzeichen")
            return False
        return True

    def muster(self, ort: str, wert: object, muster: re.Pattern, was: str) -> bool:
        if not isinstance(wert, str) or not muster.fullmatch(wert):
            self.fehler(ort, f"ist kein gültiger {was}")
            return False
        return True

    def auswahl(self, ort: str, wert: object, erlaubt: tuple) -> bool:
        if wert not in erlaubt:
            self.fehler(ort, f"erlaubt sind {' | '.join(erlaubt)}")
            return False
        return True

    def felder(self, ort: str, objekt: object, pflicht: tuple, kann: tuple) -> dict | None:
        if not isinstance(objekt, dict):
            self.fehler(ort, "erwartet ein Objekt")
            return None
        for name in objekt:
            if name not in pflicht and name not in kann:
                self.fehler(ort, f"unbekanntes Feld {name}")
        for name in pflicht:
            if name not in objekt:
                self.fehler(ort, f"Pflichtfeld {name} fehlt")
        return objekt

    def liste_von_zeilen(self, ort: str, wert: object, hoechstens: int = 200) -> bool:
        if not isinstance(wert, list) or not wert or len(wert) > 50:
            self.fehler(ort, "erwartet eine Liste mit 1 bis 50 Einträgen")
            return False
        return all(self.zeile(f"{ort}[{i + 1}]", w, hoechstens) for i, w in enumerate(wert))

    def tools(self, ort: str, wert: object) -> None:
        if wert is None:
            return
        if not isinstance(wert, list) or len(wert) > 50:
            self.fehler(ort, "erwartet eine Liste")
            return
        if not wert:
            self.fehler(ort, KEINE_WERKZEUGE)
            return
        for i, w in enumerate(wert):
            if self.zeile(f"{ort}[{i + 1}]", w, 100) and ("," in w or w != w.strip()):
                self.fehler(f"{ort}[{i + 1}]", "ohne Komma und ohne Leerzeichen am Rand")

    def ausfuehrende(self, ort: str, objekt: dict, nullbar: bool) -> None:
        genannt = [f for f in AUSFUEHRENDE if objekt.get(f) is not None]
        if len(genannt) > 1:
            self.fehler(ort, f"nennt mehr als einen Ausführenden ({', '.join(genannt)})")
        for feld in AUSFUEHRENDE:
            if feld not in objekt or (objekt[feld] is None and nullbar):
                continue
            wert = objekt[feld]
            if feld in ("agent", "skill"):
                self.muster(f"{ort}.{feld}", wert, NAME, "Name")
            elif feld == "werkzeug":
                if self.muster(f"{ort}.{feld}", wert, WERKZEUG_DATEINAME, "Werkzeug-Pfad (tools/<x>.py)") \
                        and not WERKZEUG_PFAD.fullmatch(wert):
                    self.fehler(f"{ort}.{feld}", "ist kein Werkzeug-Pfad des Bauplans")
            elif not isinstance(wert, list) or not 2 <= len(wert) <= 20:
                self.fehler(f"{ort}.parallel", "erwartet eine Liste mit 2 bis 20 Namen")
            else:
                for i, name in enumerate(wert):
                    self.muster(f"{ort}.parallel[{i + 1}]", name, NAME, "Name")

    def mensch(self, ort: str, wert: object, nullbar: bool) -> None:
        if wert is None and nullbar:
            return
        self.auswahl(ort, wert, MENSCH)

    def grenze(self, ort: str, wert: object) -> None:
        if wert is None:
            return
        if not isinstance(wert, int) or isinstance(wert, bool) or not 1 <= wert <= 99:
            self.fehler(ort, "erwartet eine ganze Zahl von 1 bis 99")

    def schritt(self, ort: str, objekt: object, mit_anweisung: bool, mit_schleife: bool) -> None:
        kann = (*AUSFUEHRENDE, "mensch") + (("bei_befund",) if mit_schleife else ())
        pflicht = ("id", "titel") + (("anweisung",) if mit_anweisung else ())
        s = self.felder(ort, objekt, pflicht, kann)
        if s is None:
            return
        self.muster(f"{ort}.id", s.get("id"), SCHRITT_ID, "Schritt-id")
        self.zeile(f"{ort}.titel", s.get("titel"), 40)
        self.ausfuehrende(ort, s, nullbar=False)
        if "mensch" in s:
            self.mensch(f"{ort}.mensch", s["mensch"], nullbar=True)
        if mit_anweisung:
            self.langtext(f"{ort}.anweisung", s.get("anweisung"))
        if mit_schleife and s.get("bei_befund") is not None:
            bb = self.felder(f"{ort}.bei_befund", s["bei_befund"], ("zurueck",), ("max",))
            if bb is not None:
                self.muster(f"{ort}.bei_befund.zurueck", bb.get("zurueck"), SCHRITT_ID, "Schritt-id")
                self.grenze(f"{ort}.bei_befund.max", bb.get("max"))


def _nur_ableitung(p: _Pruefer, ort: str, op: dict, belege_erlaubt: tuple[str, ...]) -> bool:
    """WS-M4: nur_ableitung (true) und belege (Zitate je Aussage); gibt zurück, ob nur_ableitung gilt."""
    nur = op.get("nur_ableitung")
    if "nur_ableitung" in op and nur is not True:
        p.fehler(f"{ort}.nur_ableitung", "erlaubt ist nur true")
    if "belege" not in op:
        return nur is True
    if nur is not True:
        p.fehler(f"{ort}.belege", "gibt es nur mit nur_ableitung")
        return False
    belege = p.felder(f"{ort}.belege", op["belege"], (), belege_erlaubt)
    if belege is None:
        return True
    for art, wert in belege.items():
        if art == "parallel":
            if not isinstance(wert, dict) or not wert:
                p.fehler(f"{ort}.belege.parallel", "erwartet ein Objekt <agent>: <zitat>")
                continue
            for name, zitat in wert.items():
                p.muster(f"{ort}.belege.parallel", name, NAME, "Name")
                p.zeile(f"{ort}.belege.parallel.{name}", zitat, ZITAT_HOECHSTENS)
        else:
            p.zeile(f"{ort}.belege.{art}", wert, ZITAT_HOECHSTENS)
    return True


def _belege_der_aussagen(p: _Pruefer, ort: str, schritt: object, belege: object) -> None:
    """Jede Aussage eines neuen Schritts (Ausführender, mensch) braucht mit nur_ableitung ihren Beleg."""
    if not isinstance(schritt, dict):
        return
    belege = belege if isinstance(belege, dict) else {}
    for feld in AUSFUEHRENDE:
        if schritt.get(feld) is None:
            continue
        if feld == "parallel":
            # Namen, die kein Text sind, meldet schon die Prüfung der Ausführenden.
            fehlen = [n for n in schritt["parallel"] if isinstance(n, str) and (
                not isinstance(belege.get("parallel"), dict) or n not in belege["parallel"])] \
                if isinstance(schritt["parallel"], list) else []
            if fehlen:
                p.fehler(f"{ort}.belege.parallel", f"Beleg fehlt für {', '.join(map(str, fehlen))}")
        elif feld not in belege:
            p.fehler(f"{ort}.belege.{feld}", "Pflichtfeld fehlt")
    if schritt.get("mensch") is not None and "mensch" not in belege:
        p.fehler(f"{ort}.belege.mensch", "Pflichtfeld fehlt")


def _agent_felder(p: _Pruefer, ort: str, op: dict, anlegen: bool) -> None:
    # Agent-Beschreibungen echter Kits tragen oft Beispiele mit Zeilenumbrüchen und sind mehrere tausend Zeichen lang
    # (mobile-kit: über 2000); der Schreiber setzt sie dann als eine gequotete YAML-Zeile.
    if "beschreibung" in op and p.langtext(f"{ort}.beschreibung", op["beschreibung"]) and not op["beschreibung"].strip():
        p.fehler(f"{ort}.beschreibung", "darf nicht leer sein")
    if "rolle" in op and not (op["rolle"] is None and not anlegen):
        p.auswahl(f"{ort}.rolle", op["rolle"], ROLLEN)
    if "tools" in op:
        p.tools(f"{ort}.tools", op["tools"])
    if "model" in op and op["model"] is not None:
        p.muster(f"{ort}.model", op["model"], MODELL, "Modellname")
    if "text" in op and p.langtext(f"{ort}.text", op["text"]):
        # Das Frontmatter endet an der ersten Zeile '---' nach Zeile 1; Trennlinien im Text stören das nicht (mobile-kit
        # trennt so seine Abschnitte, Review r20-3). Nur eine erste Zeile '---' sähe wie ein zweiter Kopf aus.
        erste = next((z for z in op["text"].splitlines() if z.strip()), "")
        if erste.strip() == "---":
            p.fehler(f"{ort}.text", "darf nicht mit einer Zeile '---' beginnen")
    if "memory" in op and not (op["memory"] is None and not anlegen) and op["memory"] not in MEMORY_BEREICHE:
        p.fehler(f"{ort}.memory", MEMORY_UNGUELTIG)
    if "text_stand" in op and not anlegen:
        if "text" not in op:
            p.fehler(f"{ort}.text_stand", "nur zusammen mit text")
        elif not (isinstance(op["text_stand"], str) and re.fullmatch(r"[0-9a-f]{64}", op["text_stand"])):
            p.fehler(f"{ort}.text_stand", "erwartet sha256 als 64 Zeichen hex")


def _matcher(p: _Pruefer, ort: str, wert: object, ereignis: object) -> None:
    if wert is None or not p.zeile(ort, wert, MATCHER_HOECHSTENS):
        return
    if ereignis in WERKZEUG_EREIGNISSE or ereignis in AGENT_EREIGNISSE:
        try:
            re.compile(wert)
        except re.error:
            p.fehler(ort, "ist kein gültiger regulärer Ausdruck")


def _hook_ref(p: _Pruefer, ort: str, wert: object) -> None:
    ref = p.felder(ort, wert, ("ereignis", "skript"), ("matcher",))
    if ref is None:
        return
    p.zeile(f"{ort}.ereignis", ref.get("ereignis"), MATCHER_HOECHSTENS)
    if ref.get("matcher") is not None:
        p.zeile(f"{ort}.matcher", ref["matcher"], MATCHER_HOECHSTENS, leer_erlaubt=True)
    skript = ref.get("skript")
    if p.zeile(f"{ort}.skript", skript, MATCHER_HOECHSTENS) and (
            skript.startswith("/") or "\\" in skript or any(t in ("", ".", "..") for t in skript.split("/"))):
        p.fehler(f"{ort}.skript", "ist kein Pfad im Kit")


def _timeout(p: _Pruefer, ort: str, wert: object) -> None:
    if wert is not None and (not isinstance(wert, int) or isinstance(wert, bool) or not 1 <= wert <= TIMEOUT_HOECHSTENS):
        p.fehler(ort, f"erwartet eine ganze Zahl von 1 bis {TIMEOUT_HOECHSTENS}")


def _hook_text(p: _Pruefer, ort: str, wert: object) -> None:
    if p.langtext(ort, wert) and not wert.strip():
        p.fehler(ort, "darf nicht leer sein")


def _pruefe_hook_operation(p: _Pruefer, ort: str, art: str, op: object) -> None:
    """Ticket 31 (WS-F2, WS-F3): ereignis aus atlas/hooks.EREIGNISSE, matcher, skript, text, timeout, text_stand."""
    if art == "hook_anlegen":
        o = p.felder(ort, op, ("ereignis", "skript"), ("matcher", "text", "timeout"))
        if o is None:
            return
        p.auswahl(f"{ort}.ereignis", o.get("ereignis"), EREIGNISSE)
        _matcher(p, f"{ort}.matcher", o.get("matcher"), o.get("ereignis"))
        if not p.hook_skript_erlaubt(o.get("skript")):
            ordner = p.hook_ordner or "<hooks>"
            p.fehler(f"{ort}.skript", f"erwartet {ordner}/<name>.py bzw. .sh mit einem Namen aus a-z, 0-9, - und _")
        if "text" in o:
            _hook_text(p, f"{ort}.text", o["text"])
        _timeout(p, f"{ort}.timeout", o.get("timeout"))
    elif art == "hook_aendern":
        o = p.felder(ort, op, ("hook",), ("ereignis", "matcher", "text", "text_stand", "timeout"))
        if o is None:
            return
        _hook_ref(p, f"{ort}.hook", o.get("hook"))
        if "ereignis" in o:
            p.auswahl(f"{ort}.ereignis", o["ereignis"], EREIGNISSE)
        ereignis = o.get("ereignis", (o.get("hook") or {}).get("ereignis") if isinstance(o.get("hook"), dict) else None)
        if "matcher" in o:
            _matcher(p, f"{ort}.matcher", o["matcher"], ereignis)
        if "text" in o:
            _hook_text(p, f"{ort}.text", o["text"])
        if "text" in o and "text_stand" not in o:
            p.fehler(f"{ort}.text", "braucht text_stand (sha256 des Skripts vor dem Satz)")
        if "text_stand" in o:
            if "text" not in o:
                p.fehler(f"{ort}.text_stand", "nur zusammen mit text")
            elif not (isinstance(o["text_stand"], str) and SHA256.fullmatch(o["text_stand"])):
                p.fehler(f"{ort}.text_stand", "erwartet sha256 als 64 Zeichen hex")
        _timeout(p, f"{ort}.timeout", o.get("timeout"))
    else:
        o = p.felder(ort, op, ("hook",), ())
        if o is not None:
            _hook_ref(p, f"{ort}.hook", o.get("hook"))


def _pruefe_operation(p: _Pruefer, ort: str, art: str, op: object) -> None:
    if art == "agent_anlegen":
        o = p.felder(ort, op, ("name", "beschreibung", "text"), ("rolle", "tools", "model", "memory"))
        if o is not None:
            p.muster(f"{ort}.name", o.get("name"), NAME, "Name")
            _agent_felder(p, ort, o, anlegen=True)
    elif art == "agent_aendern":
        o = p.felder(ort, op, ("name",), ("beschreibung", "rolle", "tools", "model", "text", "neuer_name", "text_stand",
                                          "memory"))
        if o is not None:
            p.muster(f"{ort}.name", o.get("name"), NAME, "Name")
            if "neuer_name" in o:
                p.muster(f"{ort}.neuer_name", o["neuer_name"], NAME, "Name")
            _agent_felder(p, ort, o, anlegen=False)
            if len(o) == 1:
                p.fehler(ort, "ändert nichts")
    elif art == "agent_loeschen":
        o = p.felder(ort, op, ("name",), ())
        if o is not None:
            p.muster(f"{ort}.name", o.get("name"), NAME, "Name")
    elif art == "schritt_einfuegen":
        nur = isinstance(op, dict) and op.get("nur_ableitung") is True
        o = p.felder(ort, op, ("skill", "nach", "schritt") + (() if nur else ("anweisung",)),
                     ("nutzen", "anweisung", *NUR_ABLEITUNG))
        if o is not None:
            p.muster(f"{ort}.skill", o.get("skill"), NAME, "Name")
            if o.get("nach") is not None:
                p.muster(f"{ort}.nach", o["nach"], SCHRITT_ID, "Schritt-id")
            p.schritt(f"{ort}.schritt", o.get("schritt"), mit_anweisung=False, mit_schleife=False)
            if _nur_ableitung(p, ort, o, BELEG_ARTEN[:-1]):
                if "anweisung" in o:
                    p.fehler(f"{ort}.anweisung", "entfällt mit nur_ableitung; den Text schreibt text_ersetzen")
                if not isinstance(o.get("belege"), dict) or "schritt" not in o["belege"]:
                    p.fehler(f"{ort}.belege.schritt", "Pflichtfeld fehlt")
                _belege_der_aussagen(p, ort, o.get("schritt"), o.get("belege"))
            else:
                p.langtext(f"{ort}.anweisung", o.get("anweisung"))
            if "nutzen" in o:
                p.zeile(f"{ort}.nutzen", o["nutzen"], 300)
    elif art == "schritt_aendern":
        o = p.felder(ort, op, ("skill", "id"), ("schritt", "anweisung", *NUR_ABLEITUNG))
        if o is not None:
            p.muster(f"{ort}.skill", o.get("skill"), NAME, "Name")
            p.muster(f"{ort}.id", o.get("id"), SCHRITT_ID, "Schritt-id")
            if _nur_ableitung(p, ort, o, BELEG_ARTEN[:-1]) and "anweisung" in o:
                p.fehler(f"{ort}.anweisung", "entfällt mit nur_ableitung; den Text schreibt text_ersetzen")
            if "schritt" in o:
                s = p.felder(f"{ort}.schritt", o["schritt"], (), ("titel", "mensch", *AUSFUEHRENDE))
                if s is not None:
                    if "titel" in s:
                        p.zeile(f"{ort}.schritt.titel", s["titel"], 40)
                    if "mensch" in s:
                        p.mensch(f"{ort}.schritt.mensch", s["mensch"], nullbar=True)
                    p.ausfuehrende(f"{ort}.schritt", s, nullbar=True)
                    if not s and "anweisung" not in o and "belege" not in o:
                        p.fehler(ort, "ändert nichts")
            if "anweisung" in o:
                p.langtext(f"{ort}.anweisung", o["anweisung"])
            if "schritt" not in o and "anweisung" not in o and "belege" not in o:
                p.fehler(ort, "ändert nichts")
    elif art == "schritt_entfernen":
        o = p.felder(ort, op, ("skill", "id"), ("nur_ableitung",))
        if o is not None:
            _nur_ableitung(p, ort, o, ())
            p.muster(f"{ort}.skill", o.get("skill"), NAME, "Name")
            p.muster(f"{ort}.id", o.get("id"), SCHRITT_ID, "Schritt-id")
    elif art == "schleife_setzen":
        o = p.felder(ort, op, ("skill", "von", "zurueck"), ("max", "satz", *NUR_ABLEITUNG))
        if o is not None:
            if "satz" in o and o["satz"] is not True:
                p.fehler(f"{ort}.satz", "erlaubt ist nur true")
            if _nur_ableitung(p, ort, o, ("zurueck",)) and o.get("zurueck") is not None \
                    and (not isinstance(o.get("belege"), dict) or "zurueck" not in o["belege"]):
                p.fehler(f"{ort}.belege.zurueck", "Pflichtfeld fehlt")
            p.muster(f"{ort}.skill", o.get("skill"), NAME, "Name")
            p.muster(f"{ort}.von", o.get("von"), SCHRITT_ID, "Schritt-id")
            if o.get("zurueck") is not None:
                p.muster(f"{ort}.zurueck", o["zurueck"], SCHRITT_ID, "Schritt-id")
            p.grenze(f"{ort}.max", o.get("max"))
    elif art == "text_ersetzen":
        o = p.felder(ort, op, ("datei", "alt", "neu"), ())
        if o is not None:
            datei = o.get("datei")
            if not isinstance(datei, str) or ".." in datei or not (TEXT_DATEI.fullmatch(datei)
                                                                    or p.hook_skript_erlaubt(datei)):
                p.fehler(f"{ort}.datei", "erwartet skills/<name>/SKILL.md, agents/<name>.md (im Projekt unter .claude/) "
                                         "oder ein Hook-Skript <hooks>/<name>.py bzw. .sh")
            if p.langtext(f"{ort}.alt", o.get("alt")) and not o["alt"]:
                p.fehler(f"{ort}.alt", "darf nicht leer sein")
            p.langtext(f"{ort}.neu", o.get("neu"))
    elif art in HOOK_OPERATIONEN:
        _pruefe_hook_operation(p, ort, art, op)
    elif art in MEMORY_OPERATIONEN:
        letzter = ("zeilentext",) if art == "memory_zeile_entfernen" else ("sha256",)
        o = p.felder(ort, op, ("gedaechtnis_id", "datei", *letzter), ())
        if o is not None:
            p.muster(f"{ort}.gedaechtnis_id", o.get("gedaechtnis_id"), GEDAECHTNIS_ID, "Memory-Ordner")
            p.muster(f"{ort}.datei", o.get("datei"), GEDAECHTNIS_DATEI, "Dateiname einer Memory-Datei")
            if art == "memory_zeile_entfernen":
                p.zeile(f"{ort}.zeilentext", o.get("zeilentext"), TEXT_HOECHSTENS)
            else:
                p.muster(f"{ort}.sha256", o.get("sha256"), SHA256, "sha256")
    elif art in KONTEXT_OPERATIONEN:
        o = p.felder(ort, op, ("datei_id", "sha256", "inhalt"), ())
        if o is not None:
            p.muster(f"{ort}.datei_id", o.get("datei_id"), KONTEXT_ID, "Kennung einer Kontext-Datei")
            p.muster(f"{ort}.sha256", o.get("sha256"), SHA256, "sha256")
            inhalt = o.get("inhalt")
            if not isinstance(inhalt, str) or "\x00" in inhalt or not _utf8_bis(inhalt, KONTEXT_INHALT_BYTES):
                p.fehler(f"{ort}.inhalt", "erwartet einen Text mit höchstens 256 KiB ohne NUL-Zeichen")
    elif art == "skill_anlegen":
        o = p.felder(ort, op, ("name", "beschreibung", "nutzen", "schritte"), ("eingabe", "ergebnis"))
        if o is not None:
            p.muster(f"{ort}.name", o.get("name"), NAME, "Name")
            p.zeile(f"{ort}.beschreibung", o.get("beschreibung"), 1000)
            p.zeile(f"{ort}.nutzen", o.get("nutzen"), 300)
            for feld in ("eingabe", "ergebnis"):
                if feld in o:
                    p.liste_von_zeilen(f"{ort}.{feld}", o[feld])
            schritte = o.get("schritte")
            if not isinstance(schritte, list) or not 1 <= len(schritte) <= 50:
                p.fehler(f"{ort}.schritte", "erwartet eine Liste mit 1 bis 50 Schritten")
            else:
                for i, s in enumerate(schritte):
                    p.schritt(f"{ort}.schritte[{i + 1}]", s, mit_anweisung=True, mit_schleife=True)
                    anweisung = s.get("anweisung") if isinstance(s, dict) else None
                    if isinstance(anweisung, str) and not anweisung.strip():
                        p.fehler(f"{ort}.schritte[{i + 1}].anweisung", "darf nicht leer sein")


OPERATIONEN = ("agent_anlegen", "agent_aendern", "agent_loeschen", "schritt_einfuegen", "schritt_aendern",
               "schritt_entfernen", "schleife_setzen", "skill_anlegen", "text_ersetzen", "hook_anlegen", "hook_aendern",
               "hook_entfernen")
HOOK_OPERATIONEN = ("hook_anlegen", "hook_aendern", "hook_entfernen")
MEMORY_OPERATIONEN = ("memory_zeile_entfernen", "memory_datei_entfernen")
KONTEXT_OPERATIONEN = ("datei_ersetzen",)


def _utf8_bis(text: str, hoechstens: int) -> bool:
    try:
        return len(text.encode("utf-8")) <= hoechstens
    except UnicodeEncodeError:
        return False


def ist_kontext_satz(satz: object) -> bool:
    eintraege = satz.get("eintraege") if isinstance(satz, dict) else None
    return isinstance(eintraege, list) and any(isinstance(e, dict) and any(k in KONTEXT_OPERATIONEN for k in e)
                                               for e in eintraege)


def ist_gedaechtnis_satz(satz: object) -> bool:
    eintraege = satz.get("eintraege") if isinstance(satz, dict) else None
    return isinstance(eintraege, list) and any(isinstance(e, dict) and any(k in MEMORY_OPERATIONEN for k in e)
                                               for e in eintraege)


def pruefe(satz: object, gedaechtnis_erlaubt: bool = False, kontext_erlaubt: bool = False,
           hook_ordner: str | None = None) -> dict:
    """Wirft SatzFehler mit allen Meldungen; gibt den Satz unverändert zurück. hook_ordner (hooks bzw. .claude/hooks)
    legt fest, wo Hook-Skripte liegen dürfen; ohne gilt beides."""
    p = _Pruefer(hook_ordner)
    s = p.felder("Satz", satz, ("schema", "titel", "herkunft", "eintraege"), ())
    if s is None:
        raise SatzFehler(p.meldungen)
    if s.get("schema") != SCHEMA:
        p.fehler("Satz.schema", f"erwartet {SCHEMA}")
    p.zeile("Satz.titel", s.get("titel"), 120)
    p.auswahl("Satz.herkunft", s.get("herkunft"), HERKUNFT)
    eintraege = s.get("eintraege")
    if not isinstance(eintraege, list) or not 1 <= len(eintraege) <= EINTRAEGE_HOECHSTENS:
        p.fehler("Satz.eintraege", f"erwartet eine Liste mit 1 bis {EINTRAEGE_HOECHSTENS} Einträgen")
        raise SatzFehler(p.meldungen)
    if ist_kontext_satz(s):
        if not kontext_erlaubt:
            raise SatzFehler([NUR_AUS_PANEL])
        if len(eintraege) != 1 or not all(isinstance(e, dict) and any(k in KONTEXT_OPERATIONEN for k in e)
                                          for e in eintraege):
            raise SatzFehler([NUR_EINE_DATEI])
    if ist_gedaechtnis_satz(s):
        if not gedaechtnis_erlaubt:
            raise SatzFehler([NUR_AUS_AUSWERTUNG])
        if not all(isinstance(e, dict) and any(k in MEMORY_OPERATIONEN for k in e) for e in eintraege):
            raise SatzFehler([NICHT_GEMISCHT])
    gesehen: set[str] = set()
    for i, e in enumerate(eintraege):
        ort = f"Eintrag {i + 1}"
        if not isinstance(e, dict):
            p.fehler(ort, "erwartet ein Objekt")
            continue
        eid = e.get("id")
        if p.muster(f"{ort}.id", eid, EINTRAG_ID, "Eintrag-id"):
            if eid in gesehen:
                p.fehler(f"{ort}.id", f"{eid} kommt doppelt vor")
            gesehen.add(eid)
            ort = f"Eintrag {eid}"
        p.zeile(f"{ort}.beschreibung", e.get("beschreibung"), 300)
        if "gruppe" in e:
            p.muster(f"{ort}.gruppe", e["gruppe"], EINTRAG_ID, "Gruppenname")
        arten = [k for k in e if k not in EINTRAG_FELDER]
        if len(arten) != 1:
            p.fehler(ort, "braucht genau eine Operation")
            continue
        art = arten[0]
        if art not in OPERATIONEN and art not in MEMORY_OPERATIONEN and art not in KONTEXT_OPERATIONEN:
            p.fehler(ort, f"unbekannte Operation {art}")
            continue
        _pruefe_operation(p, f"{ort}.{art}", art, e[art])
    if p.meldungen:
        raise SatzFehler(p.meldungen)
    return s


def operation(eintrag: dict) -> tuple[str, dict]:
    art = next(k for k in eintrag if k not in EINTRAG_FELDER)
    return art, eintrag[art]
