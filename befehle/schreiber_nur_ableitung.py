"""Ableitung und Text im Gleichschritt, wenn die KI den Text selbst umschreibt (specs/werkstatt.yaml WS-M4, WS-M5,
WS-M10, WS-M11).

In einem abgeleiteten Ablauf, dessen Schritte nicht als eigene Abschnitte im Text stehen, kann der Schreiber den Text
nicht sicher selbst ändern (WS-W6, WS-W7). Die KI schreibt ihn dann mit text_ersetzen um, und die Schritt-Operationen
mit nur_ableitung ändern nur die Ableitung: kein Byte des Texts. Neue Aussagen belegt der Eintrag mit Zitaten; der
Schreiber sucht ihre Zeile im Stand nach den vorher gewählten Einträgen und prüft sie nach denselben Regeln wie das
Einlesen der Ableitung (AB-P1, AB-P3 bis AB-P9). Eine Aussage, die wegfällt, darf der Text nicht mehr machen: ihr Zitat
muss vorher ein text_ersetzen entfernt haben. text_ersetzen zieht die Belege von Schritten und abgeleiteten Rollen
nach; was danach nicht mehr stimmt, meldet die Vorschau aus dem Neuscan des Kits, wie Übernehmen.
"""
from __future__ import annotations

import posixpath
import re

from ableitung import abgeleitete_schritte, beleg_grund, belegzeile
from schreiber_gleichschritt import agenten_von
from virtuelles_kit import SchreibFehler
from vokabular import AUSFUEHRENDE, WERKZEUG_PFAD

NUR_ABGELEITET = "nur_ableitung gilt nur für Skills mit abgeleitetem Ablauf."
KURZ = 60
AUFRUF = r"(?:`(?:[a-z0-9-]+:)?{name}`|subagent_type:\s*[\"'](?:[a-z0-9-]+:)?{name}[\"'])"


def kurz(text: str) -> str:
    return text if len(text) <= KURZ else text[:KURZ - 1] + "…"


def _name_im_zitat(feld: str, wert: str) -> str:
    """Was ein Zitat wörtlich enthalten muss (AB-P3, AB-P5, AB-P6; wie der Scanner)."""
    if feld == "werkzeug":
        treffer = WERKZEUG_PFAD.match(posixpath.normpath(wert))
        return treffer.group(1) if treffer else wert
    return wert


def _aehnlich(zitat: str, zeile: str) -> bool:
    """Steht in der Zeile noch mindestens die Hälfte der Wörter des alten Zitats (umformuliert statt entfernt)?"""
    alt = set(re.findall(r"\w+", zitat.lower()))
    return bool(alt) and len(alt & set(re.findall(r"\w+", zeile.lower()))) * 2 >= len(alt)


def _steht(beleg: object, texte: list[str]) -> bool:
    """AB-P1: Der Beleg gilt, wie ihn der Scanner liest (ableitung.beleg_grund)."""
    return beleg_grund(texte, beleg) is None


class NurAbleitungMixin:
    def _nur_ableitung_datei(self, skill: str) -> str:
        art, rel = self._art(skill)
        if art != "abgeleitet":
            raise SchreibFehler(NUR_ABGELEITET)
        return rel

    def _beleg_aus_zitat(self, rel: str, zitat: str) -> dict:
        zitat = zitat.strip()
        texte = self.kit.textdatei(rel).texte()
        treffer = [i for i, zeile in enumerate(texte) if zitat in zeile]
        if not treffer:
            raise SchreibFehler(f"„{kurz(zitat)}“ steht nicht in {rel}.", ("zitat", rel, zitat))
        if len(treffer) > 1:
            raise SchreibFehler(f"„{kurz(zitat)}“ steht in {rel} {len(treffer)}-mal; nimm ein Zitat, das genau "
                                f"einmal vorkommt.")
        if len(zitat.split()) < 3 and zitat != texte[treffer[0]].strip():
            raise SchreibFehler(f"„{kurz(zitat)}“ ist zu kurz: Ein Zitat hat mindestens 3 Wörter oder ist die "
                                f"ganze Zeile.")
        return {"zeile": treffer[0] + 1, "zitat": zitat}

    def _beleg_mit_namen(self, rel: str, zitat: str, feld: str, wert: str) -> dict:
        beleg = self._beleg_aus_zitat(rel, zitat)
        name = _name_im_zitat(feld, wert)
        if name not in beleg["zitat"]:
            raise SchreibFehler(f"Der Beleg für {feld} {wert} muss {name} wörtlich enthalten.")
        return beleg

    def _muss_weg(self, rel: str, belege: list) -> None:
        """WS-M4: Eine Aussage, die wegfällt, macht der Text nicht mehr; sonst fehlt das text_ersetzen dazu."""
        texte = self.kit.textdatei(rel).texte()
        for beleg in belege:
            if _steht(beleg, texte):
                zitat = beleg["zitat"].strip()
                raise SchreibFehler(f"„{kurz(zitat)}“ steht noch in {rel}; der Text muss ihn erst entfernen "
                                    f"(text_ersetzen im selben Satz).", ("zitat_weg", rel, zitat))

    # ------------------------------------------------------------ Operationen mit nur_ableitung

    def _merke_weg(self, skill: str, rel: str, belege: list, namen: set[str], was: str | None = None,
                   sid: str = "", warnen: bool = False) -> None:
        """Merkt Zitate und Ausführende, die wegfallen, für die Prüfung nach allen Einträgen (WS-M4); bei einer Schleife
        oder einem Mensch-Schritt (was) auch die Stelle, die nach allen Einträgen als Warnung erscheint. Mit warnen
        (Änderung von Hand, der Schreiber hat den Text selbst angepasst, wo er konnte) wird ein weggefallener
        Ausführender, den der Text noch nennt, nur eine Warnung, nie ein Fehler."""
        zitate = list(dict.fromkeys(b["zitat"].strip() for b in belege if isinstance(b, dict)
                                    and isinstance(b.get("zitat"), str) and b["zitat"].strip()))
        if was:
            self.buch.weg_belege += [(self.eintrag, skill, rel, f"{was} von Schritt {sid}", dict(b)) for b in belege
                                     if belegzeile(b) is not None]
        if warnen:
            if namen:
                self.buch.weg_genannt.append((self.eintrag, skill, rel, set(namen)))
        elif zitate or namen:
            self.buch.entfernt.append((self.eintrag, skill, rel, zitate, set(namen)))

    def _nur_entfernen(self, skill: str, sid: str) -> None:
        rel = self._nur_ableitung_datei(skill)
        schritte, n = self._abgeleiteter_schritt(skill, sid)
        self._nicht_der_einzige(skill, sid, schritte)
        belege = self._alle_belege(schritte[n])
        self._muss_weg(rel, belege)
        self._merke_weg(skill, rel, belege, agenten_von(schritte[n]))
        self._schleifen_umlegen(schritte, n)
        del schritte[n]

    def _nur_einfuegen(self, skill: str, op: dict) -> None:
        rel = self._nur_ableitung_datei(skill)
        schritte = self.kit.ableitung_von(skill)
        ids = [s.get("id") if isinstance(s, dict) else None for s in schritte]
        schritt = op["schritt"]
        k = self._position(skill, ids, op["nach"], schritt["id"])
        zitate = op.get("belege") or {}
        neu = {"id": schritt["id"], "titel": schritt["titel"]}
        belege: dict = {}
        for feld in AUSFUEHRENDE:
            if schritt.get(feld) is None:
                continue
            neu[feld] = schritt[feld]
            if feld == "parallel":
                belege["parallel"] = {name: self._beleg_mit_namen(rel, zitate["parallel"][name], "parallel", name)
                                      for name in schritt["parallel"]}
            else:
                belege[feld] = self._beleg_mit_namen(rel, zitate[feld], feld, schritt[feld])
        if schritt.get("mensch"):
            neu["mensch"] = schritt["mensch"]
            belege["mensch"] = self._beleg_aus_zitat(rel, zitate["mensch"])
        neu["beleg"] = self._beleg_aus_zitat(rel, zitate["schritt"])
        if belege:
            neu["belege"] = belege
        schritte.insert(k, neu)

    def _nur_aendern(self, skill: str, sid: str, aenderung: dict, zitate: dict) -> None:
        rel = self._nur_ableitung_datei(skill)
        schritte, n = self._abgeleiteter_schritt(skill, sid)
        schritt = schritte[n]
        alte = schritt.get("belege") if isinstance(schritt.get("belege"), dict) else {}
        belege = dict(alte)
        if "titel" in aenderung:
            schritt["titel"] = aenderung["titel"]
        if any(f in aenderung for f in AUSFUEHRENDE):
            self._nur_ausfuehrende(skill, rel, schritt, aenderung, zitate, alte, belege)
        if "mensch" in aenderung:
            self._nur_mensch(skill, rel, schritt, aenderung["mensch"], zitate, belege)
        if "schritt" in zitate:
            schritt["beleg"] = self._beleg_aus_zitat(rel, zitate["schritt"])
        if "mensch" in zitate and "mensch" not in aenderung:
            if not schritt.get("mensch"):
                raise SchreibFehler("belege.mensch passt zu keinem Mensch-Schritt.")
            belege["mensch"] = self._beleg_aus_zitat(rel, zitate["mensch"])
        if not any(f in aenderung for f in AUSFUEHRENDE):
            self._neue_zitate_bleibender(rel, schritt, zitate, belege)
        if belege:
            schritt["belege"] = belege
        else:
            schritt.pop("belege", None)

    def _nur_ausfuehrende(self, skill: str, rel: str, schritt: dict, aenderung: dict, zitate: dict, alte: dict,
                          belege: dict) -> None:
        """Neue Ausführende: genanntes Zitat oder bisheriger Beleg desselben Namens; wegfallende müssen aus dem Text."""
        namen_vorher = agenten_von(schritt)
        neu = {f: aenderung[f] for f in AUSFUEHRENDE if aenderung.get(f) is not None}
        for feld in AUSFUEHRENDE:
            schritt.pop(feld, None)
            belege.pop(feld, None)
        behalten: list[dict] = []
        for feld, wert in neu.items():
            schritt[feld] = wert
            if feld == "parallel":
                belege["parallel"] = {name: self._ausfuehrender_beleg(rel, "parallel", name, zitate, alte)
                                      for name in wert}
                behalten += belege["parallel"].values()
            else:
                belege[feld] = self._ausfuehrender_beleg(rel, feld, wert, zitate, alte)
                behalten.append(belege[feld])
        bisher = [alte.get(f) for f in AUSFUEHRENDE if f != "parallel"]
        if isinstance(alte.get("parallel"), dict):
            bisher += alte["parallel"].values()
        zitate_behalten = {b.get("zitat") for b in behalten}
        weg = [b for b in bisher if isinstance(b, dict) and b.get("zitat") not in zitate_behalten]
        self._muss_weg(rel, weg)
        self._merke_weg(skill, rel, weg, namen_vorher - agenten_von(neu))

    def _neue_zitate_bleibender(self, rel: str, schritt: dict, zitate: dict, belege: dict) -> None:
        """Neue Zitate für Ausführende, die bleiben (z. B. nach einem umgeschriebenen Satz)."""
        for feld in AUSFUEHRENDE:
            if feld not in zitate:
                continue
            if schritt.get(feld) is None:
                raise SchreibFehler(f"belege.{feld} passt zu keinem {feld} des Schritts.")
            if feld == "parallel":
                for name, zitat in zitate["parallel"].items():
                    if name not in schritt["parallel"]:
                        raise SchreibFehler(f"belege.parallel nennt {name}, der Schritt startet {name} nicht.")
                    belege.setdefault("parallel", {})[name] = self._beleg_mit_namen(rel, zitat, "parallel", name)
            else:
                belege[feld] = self._beleg_mit_namen(rel, zitate[feld], feld, schritt[feld])

    def _nur_mensch(self, skill: str, rel: str, schritt: dict, wert: str | None, zitate: dict, belege: dict) -> None:
        schritt.pop("mensch", None)
        vorher = belege.pop("mensch", None)
        if wert is None:
            self._muss_weg(rel, [vorher])
            self._merke_weg(skill, rel, [vorher], set(), "der Mensch-Schritt", schritt.get("id", ""))
            return
        schritt["mensch"] = wert
        if "mensch" in zitate:
            belege["mensch"] = self._beleg_aus_zitat(rel, zitate["mensch"])
        elif vorher is not None:
            belege["mensch"] = vorher
        else:
            raise SchreibFehler(f"Für mensch {wert} fehlt ein Beleg (belege.mensch).")

    def _ausfuehrender_beleg(self, rel: str, feld: str, wert: str, zitate: dict, alte: dict) -> dict:
        """Beleg eines Ausführenden: genanntes Zitat, sonst der bisherige Beleg desselben Namens (als agent oder in der
        Parallelgruppe), sonst ein Fehler."""
        zitat = zitate.get("parallel", {}).get(wert) if feld == "parallel" else zitate.get(feld)
        if isinstance(zitat, str):
            return self._beleg_mit_namen(rel, zitat, feld, wert)
        if feld in ("agent", "parallel"):
            bisher = alte.get("agent") if isinstance(alte.get("agent"), dict) and \
                wert in str(alte["agent"].get("zitat", "")) else None
            parallel = alte.get("parallel") if isinstance(alte.get("parallel"), dict) else {}
            bisher = bisher or (parallel.get(wert) if isinstance(parallel.get(wert), dict) else None)
        else:
            bisher = alte.get(feld) if isinstance(alte.get(feld), dict) and \
                _name_im_zitat(feld, wert) in str(alte[feld].get("zitat", "")) else None
        if bisher is None:
            ort = f"belege.parallel.{wert}" if feld == "parallel" else f"belege.{feld}"
            raise SchreibFehler(f"Für {feld} {wert} fehlt ein Beleg ({ort}).")
        return dict(bisher)

    def _nur_schleife(self, skill: str, op: dict) -> None:
        rel = self._nur_ableitung_datei(skill)
        schritte, n = self._abgeleiteter_schritt(skill, op["von"])
        schritt = schritte[n]
        if op["zurueck"] is None:
            schritt.pop("bei_befund", None)
            if isinstance(schritt.get("belege"), dict):
                self._muss_weg(rel, [schritt["belege"].get("zurueck")])
                self._merke_weg(skill, rel, [schritt["belege"].get("zurueck")], set(), "die Schleife", op["von"])
                schritt["belege"].pop("zurueck", None)
                if not schritt["belege"]:
                    schritt.pop("belege")
            return
        _, ziel = self._abgeleiteter_schritt(skill, op["zurueck"])
        if ziel >= n:
            raise SchreibFehler(f"Die Schleife von {op['von']} zurück zu {op['zurueck']} führt nicht zurück.")
        beleg = self._beleg_aus_zitat(rel, op["belege"]["zurueck"])
        grenze = op.get("max")
        if grenze is not None and not re.search(rf"(?<!\d){grenze}(?!\d)", beleg["zitat"]):
            raise SchreibFehler(f"Der Beleg der Schleife muss die Zahl {grenze} enthalten.")
        schritt["bei_befund"] = {"zurueck": op["zurueck"], **({"max": grenze} if grenze is not None else {})}
        if not isinstance(schritt.get("belege"), dict):
            schritt["belege"] = {}
        schritt["belege"]["zurueck"] = beleg

    # ------------------------------------------------------------ Belege nach text_ersetzen (WS-M5, WS-M10)

    def _belege_der_datei(self, rel: str) -> list[tuple[str | None, str, list]]:
        """(Skill oder None für eine Rolle, Schritt-id bzw. Agent, Belege) aller Angaben der Ableitung, die der Scanner
        in rel prüft: Schritte der Skill, deren SKILL.md rel ist, und Rollen des Agenten, dessen Datei rel ist (beide
        nach ihrem Namen, wie der Scanner; datei im Eintrag zählt nicht)."""
        if not isinstance(self.kit.ableitung, dict):
            return []
        liste: list[tuple[str | None, str, list]] = []
        skills = self.kit.skills()
        for skill, eintrag in self.kit.ableitung.get("skills", {}).items():
            if skills.get(skill) == rel:
                liste += [(skill, str(s.get("id")), self._alle_belege(s))
                          for s in abgeleitete_schritte(eintrag) or [] if isinstance(s, dict)]
        rollen = self.kit.ableitung.get("agenten") or {}
        if rollen:
            agenten = self.kit.agenten()
            for name, eintrag in rollen.items():
                if isinstance(eintrag, dict) and isinstance(eintrag.get("beleg"), dict) and agenten.get(name) == rel:
                    liste.append((None, name, [eintrag["beleg"]]))
        return liste

    def _belege_nachziehen(self, rel: str, alte: list[str], texte: list[str], von: int, bis: int, versatz: int) -> None:
        """Belege nach der ersetzten Stelle rücken um versatz; Belege darin zeigen auf die Zeile der neuen Stelle mit
        ihrem Zitat, bevorzugt auf eine unveränderte Zeile, sonst die nächste zur alten Zeile; ohne Treffer bleiben
        sie (WS-M5)."""
        geister = [b for _, _, r, _, b in self.buch.weg_belege if r == rel]
        for _, _, belege in [*self._belege_der_datei(rel), (None, "", geister)]:
            for beleg in belege:
                zeile = belegzeile(beleg)
                if zeile is None:
                    continue
                if zeile > bis:
                    beleg["zeile"] = zeile + versatz
                elif zeile >= von:
                    zitat = beleg.get("zitat")
                    if not isinstance(zitat, str) or not zitat.strip():
                        continue
                    treffer = [z for z in range(von, min(bis + versatz, len(texte)) + 1) if zitat in texte[z - 1]]
                    if treffer:
                        vorher = alte[zeile - 1] if zeile <= len(alte) else None
                        beleg["zeile"] = min(treffer, key=lambda z: (texte[z - 1] != vorher, abs(z - zeile), z))

    def noch_im_text(self) -> list[tuple[str, str, str, int, str, str]]:
        """WS-M4 nach allen Einträgen: Ein weggefallenes Zitat steht nirgends mehr in der Datei (außer als Beleg eines
        bleibenden Schritts), und ein weggefallener Ausführender, den kein bleibender Schritt der Skill nennt, wird im
        Text der Skill nicht mehr aufgerufen; (Eintrag, Datei, Zeilentext, Zeile, Art, Zitat bzw. Name)."""
        funde = []
        for eid, skill, rel, zitate, namen in self.buch.entfernt:
            texte = self._texte(rel)
            if not texte:
                continue
            schritte = self.schritte_der_skill(skill, rel)
            belegt = {b["zeile"] for s in schritte for b in self._alle_belege(s) if _steht(b, texte)}
            for zitat in zitate:
                zeile = next((i + 1 for i, z in enumerate(texte) if zitat in z and i + 1 not in belegt), None)
                if zeile is not None:
                    funde.append((eid, rel, texte[zeile - 1], zeile, "zitat", zitat))
            genutzt = {n for s in schritte for n in agenten_von(s)}
            for name in sorted(namen - genutzt):
                muster = re.compile(AUFRUF.format(name=re.escape(name)))
                zeile = next((i + 1 for i, z in enumerate(texte) if muster.search(z)), None)
                if zeile is not None:
                    funde.append((eid, rel, texte[zeile - 1], zeile, "aufruf", name))
        return funde

    def wegfall_hinweise(self) -> list[tuple[str, int, str]]:
        """Stellen, an denen Weggefallenes vielleicht nur umformuliert steht, als Warnung: weggefallene
        Ausführende, die der Text der Skill noch als ganzes Wort nennt (nach einer Änderung von Hand auch als
        Aufruf), und die Zeile der alten Schleife bzw. des Mensch-Schritts; (Datei, Zeile, Text)."""
        hinweise = []
        quellen = [(skill, rel, namen, False) for _eid, skill, rel, _zitate, namen in self.buch.entfernt]
        quellen += [(skill, rel, namen, True) for _eid, skill, rel, namen in self.buch.weg_genannt]
        for skill, rel, namen, auch_aufrufe in quellen:
            texte = self._texte(rel)
            if not texte:
                continue
            genutzt = {n for s in self.schritte_der_skill(skill, rel) for n in agenten_von(s)}
            aufruf = [] if auch_aufrufe else [re.compile(AUFRUF.format(name=re.escape(n))) for n in namen]
            for name in sorted(namen - genutzt):
                wort = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])")
                zeile = next((i + 1 for i, z in enumerate(texte) if wort.search(z)
                              and not any(m.search(z) for m in aufruf)), None)
                if zeile is not None:
                    hinweise.append((rel, zeile, f"Der Text in {rel} nennt {name} noch (Zeile {zeile}), im Bild kommt "
                                                 f"er dort nicht mehr vor: „{texte[zeile - 1].strip()[:60]}“."))
        for _eid, _skill, rel, was, beleg in self.buch.weg_belege:
            texte = self._texte(rel)
            zeile = belegzeile(beleg)
            if zeile is not None and 1 <= zeile <= len(texte) and _aehnlich(str(beleg.get("zitat", "")), texte[zeile - 1]):
                hinweise.append((rel, zeile, f"Hier stand {was} in {rel} (Zeile {zeile}): „{texte[zeile - 1].strip()[:60]}“. "
                                             f"Prüf, ob der Text sie noch beschreibt."))
        return hinweise

    def _texte(self, rel: str) -> list[str]:
        """Zeilentexte einer Datei für die Nachprüfungen; eine Datei, die es nicht gibt oder die sich nicht als UTF-8
        lesen lässt, hat keine (der Scanner überspringt sie)."""
        try:
            return self.kit.textdatei(rel).texte()
        except SchreibFehler:
            return []

    def nennungen(self) -> list[tuple[str, str, int, str]]:
        """WS-M11: gelöschte Agenten, die eine Skill oder ein anderer Agent noch als ganzes Wort nennt;
        (Name, Datei, Zeile, Zeilentext), höchstens eine Stelle je Datei."""
        stellen = []
        vorhanden = self.kit.agenten()
        dateien = sorted(self.kit.skills().values()) + sorted(vorhanden.values())
        for name in sorted(self.buch.geloeschte_agenten):
            if name in vorhanden:
                continue
            muster = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])")
            for rel in dateien:
                texte = self._texte(rel)
                zeile = next((i + 1 for i, z in enumerate(texte) if muster.search(z)), None)
                if zeile is not None:
                    stellen.append((name, rel, zeile, texte[zeile - 1]))
        return stellen

    def noch_aufgerufen(self) -> list[tuple[str, str, str, int, str]]:
        """WS-M11: gelöschte Agenten, die der Text einer Skill noch aufruft (Inline-Code, subagent_type);
        (Eintrag, Name, Datei, Zeile, Zeilentext)."""
        stellen = []
        vorhanden = self.kit.agenten()
        for name, eid in self.buch.geloeschte_agenten.items():
            if name in vorhanden:
                continue
            muster = re.compile(AUFRUF.format(name=re.escape(name)))
            for _skill, rel in sorted(self.kit.skills().items()):
                for i, zeile in enumerate(self._texte(rel)):
                    if muster.search(zeile):
                        stellen.append((eid, name, rel, i + 1, zeile))
                        break
        return stellen
