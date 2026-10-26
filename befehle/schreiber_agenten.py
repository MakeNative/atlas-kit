"""Agent-Operationen des Schreibers (WS-W1 bis WS-W3): anlegen, ändern, umbenennen, löschen."""
from __future__ import annotations

import hashlib
import re

import ablauf
import werkstatt_pfade as pfade
import zeilen as z
from ableitung import abgeleitete_schritte, beleg_grund, belegzeile
from aenderungssatz import KEINE_WERKZEUGE, ZITAT_HOECHSTENS
from schreiber_gleichschritt import agenten_von
from virtuelles_kit import SchreibFehler, mit_punkt, zeilen_der_anweisung
from vokabular import AUSFUEHRENDE
from yaml.nodes import ScalarNode
from zeilen import Textdatei, ZeilenFehler


def agent_text(datei: Textdatei) -> str:
    """Der Text unter dem Kopf, wie ihn GET /api/agent zeigt: ohne Leerzeilen vorn, mit Zeilenende am Schluss."""
    zeilen = [zeile.rstrip("\r\n") for zeile in datei.zeilen[datei.text_ab():]]
    while zeilen and not zeilen[0].strip():
        zeilen.pop(0)
    return "\n".join(zeilen) + ("\n" if zeilen else "")


def text_stand(text: str) -> str:
    """Stand des Texts für agent_aendern.text_stand (Review r20-2): sha256 der UTF-8-Bytes, hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


TEXT_GEAENDERT = ("{rel} hat sich geändert, seit das Formular den Agenten gelesen hat. Nimm diese Änderung zurück "
                  "und öffne den Agenten neu; sonst überschriebe sie die neueren Zeilen.")


TEXT_SCHON_VORGEMERKT = ("Den Text von {name} ändert schon ein früherer Eintrag dieses Satzes. Übernimm ihn zuerst und "
                         "ändere den Text danach noch einmal; sonst ginge die frühere Änderung verloren.")


Spanne = tuple[int, int, int, int]  # ein umbenannter Name: Anfang und Ende in der alten, dann in der neuen Zeile
Ersetzung = tuple[str, str, list[Spanne]]  # eine Zeile, die das Umbenennen geändert hat: vorher, nachher, Spannen


def nennung(kit_id: str, name: str) -> re.Pattern:
    """Eine Nennung des Agenten im Text: in Inline-Code oder Anführungszeichen, auch mit <kit>:-Präfix."""
    return re.compile(rf"(?<=[`\"'])((?:{re.escape(kit_id)}:)?){re.escape(name)}(?=[`\"'])")


def ersetze_namen(zeile: str, neu: str, muster: re.Pattern) -> tuple[str, list[Spanne]]:
    """Die Zeile mit neu statt jeder Nennung nach muster (der Präfix bleibt) und die Spannen der ersetzten Namen."""
    teile: list[str] = []
    spannen: list[Spanne] = []
    pos = versatz = 0
    for m in muster.finditer(zeile):
        von, bis = m.start() + len(m.group(1)), m.end()
        teile += [zeile[pos:von], neu]
        spannen.append((von, bis, von + versatz, von + versatz + len(neu)))
        versatz += len(neu) - (bis - von)
        pos = bis
    return "".join(teile) + zeile[pos:], spannen


def _abgebildet(stelle: int, spannen: list[Spanne], ende: bool) -> int:
    """Eine Stelle der alten Zeile in der neuen; berührt oder schneidet sie einen umbenannten Namen (auch genau an
    seinem Rand), zählt der ganze neue Name dazu."""
    versatz = 0
    for a_von, a_bis, n_von, n_bis in spannen:
        if stelle < a_von:
            break
        if stelle <= a_bis:
            return n_bis if ende else n_von
        versatz = n_bis - a_bis
    return stelle + versatz


def abgebildet(alt: str, neu: str, zitat: object, spannen: list[Spanne]) -> str | None:
    """Das Stück von neu, das zitat in alt war (über die Spannen der ersetzten Namen); None, wenn es nicht in alt
    steht oder kein Text ist."""
    if not isinstance(zitat, str) or not zitat.strip():
        return None
    anfang = alt.find(zitat)
    if anfang < 0:
        return None
    return neu[_abgebildet(anfang, spannen, False):_abgebildet(anfang + len(zitat), spannen, True)]


def zitat_aus_zeile(alt: str, neu: str, zitat: object, spannen: list[Spanne],
                    hoechstens: int = ZITAT_HOECHSTENS) -> str | None:
    """Das Zitat eines Belegs in einer Zeile, die das Umbenennen geändert hat (alt → neu, spannen: die ersetzten
    Namen): Es folgt seiner Stelle in alt (abgebildet), nur die Namen darin ändern sich; steht es nicht in alt (oder ist
    kein Text), die ganze neue Zeile. None, wenn so kein gültiges Zitat entsteht (länger als hoechstens oder nach
    ableitung.beleg_grund ungültig): dann bleibt der Beleg, und die Vorschau meldet ihn als kaputt."""
    ergebnis = abgebildet(alt, neu, zitat, spannen)
    if ergebnis is None:
        ergebnis = neu.strip()
    if len(ergebnis) > hoechstens or beleg_grund([neu], {"zeile": 1, "zitat": ergebnis}) is not None:
        return None
    return ergebnis


class AgentenMixin:
    def agent_anlegen(self, op: dict, eid: str) -> None:
        name = op["name"]
        rel = f"{self.kit.agenten_ordner}/{name}.md"
        if name in self.kit.agenten() or self.kit.existiert(rel):
            raise SchreibFehler(f"Agent {name} gibt es schon.")
        if op.get("tools") == []:
            raise SchreibFehler(KEINE_WERKZEUGE)
        kopf = ["---", f"name: {z.skalar(name)}", f"description: {z.skalar(op['beschreibung'])}"]
        if op.get("tools") is not None:
            kopf.append(f"tools: {z.skalar(', '.join(op['tools']))}")
        if op.get("model"):
            kopf.append(f"model: {z.skalar(op['model'])}")
        if op.get("memory"):
            kopf.append(f"memory: {op['memory']}")
        if op.get("rolle"):
            kopf += ["atlas:", f"  rolle: {op['rolle']}"]
        text = op["text"].strip("\n")
        zeilen = [*kopf, "---", "", *(text.split("\n") if text else [])]
        self.kit.schreibe(rel, ("\n".join(zeilen) + "\n").encode("utf-8"))
        self.buch.geloeschte_agenten.pop(name, None)
        self.buch.wer_heisst[name] = ("neu", eid)

    def agent_aendern(self, op: dict, _eid: str) -> None:
        name = op["name"]
        rel = self._agent(name)
        if op.get("tools") == []:
            raise SchreibFehler(KEINE_WERKZEUGE)
        datei = self.kit.textdatei(rel)
        if "text_stand" in op:
            self._pruefe_text_stand(rel, name, datei, op["text_stand"])
        alte = datei.texte()
        if "beschreibung" in op:
            datei.frontmatter().setze("description", z.skalar(op["beschreibung"]))
        for feld, wert in (("tools", op.get("tools")), ("model", op.get("model"))):
            if feld not in op:
                continue
            fm = datei.frontmatter()
            if wert is None:
                if fm.wurzel is not None:
                    fm.wurzel.entferne(feld)
            else:
                fm.setze(feld, z.skalar(", ".join(wert) if feld == "tools" else wert))
        if "memory" in op:
            self._setze_memory(datei, op["memory"])
        if "rolle" in op:
            self._setze_rolle(datei, op["rolle"])
        if "text" in op:
            self._ersetze_text(datei, op["text"])
        if "neuer_name" in op and op["neuer_name"] != name:
            vor_dem_namen = datei.texte()
            ziel, namenszeile = self._benenne_um(name, op["neuer_name"], rel, datei)
            self._rolle_umbenennen(name, op["neuer_name"], ziel)
            self._belege_nachziehen(ziel, alte, datei.texte(), 1, len(alte), len(datei.texte()) - len(alte))
            self._eigenes_zitat_umbenennen(op["neuer_name"], vor_dem_namen, datei.texte(), namenszeile)
        else:
            self.kit.schreibe_text(rel, datei)
            self._belege_nachziehen(rel, alte, datei.texte(), 1, len(alte), len(datei.texte()) - len(alte))

    def _pruefe_text_stand(self, rel: str, name: str, datei: Textdatei, stand: str) -> None:
        """WS-PT7: Der Text, aus dem das Formular die Teile gebildet hat, muss der Zwischenstand des Satzes sein (bis zu
        diesem Eintrag). Hat kein früherer Eintrag den Text angefasst, ist das die Platte. Sonst scheitert der Eintrag,
        nie wird still überschrieben (Review r20-2, r20-12)."""
        if text_stand(agent_text(datei)) == stand:
            return
        roh = self.kit.von_platte(rel)
        try:
            platte = agent_text(Textdatei.aus_bytes(roh)) if roh is not None else None
        except UnicodeDecodeError:
            platte = None
        if platte is not None and text_stand(platte) == stand:
            raise SchreibFehler(TEXT_SCHON_VORGEMERKT.format(name=name))
        raise SchreibFehler(TEXT_GEAENDERT.format(rel=rel))

    @staticmethod
    def _setze_memory(datei: Textdatei, memory: str | None) -> None:
        fm = datei.frontmatter()
        if memory is None:
            if fm.wurzel is not None:
                fm.wurzel.entferne("memory")
            return
        if fm.wurzel is not None and fm.wurzel.paar("memory") is not None:
            fm.wurzel.setze("memory", memory)
        elif fm.wurzel is not None and fm.wurzel.paar("atlas") is not None:
            anfang, _ = fm.wurzel.bereich("atlas")
            datei.fuege_ein(anfang, [" " * fm.wurzel.einzug() + f"memory: {memory}"])
        else:
            fm.haenge_an([f"memory: {memory}"])

    def _setze_rolle(self, datei: Textdatei, rolle: str | None) -> None:
        fm = datei.frontmatter()
        atlas = fm.wurzel.unter("atlas") if fm.wurzel is not None else None
        if rolle is None:
            if atlas is None:
                return
            atlas.entferne("rolle")
            fm = datei.frontmatter()
            if fm.werte().get("atlas") is None and fm.wurzel is not None and fm.wurzel.paar("atlas"):
                fm.wurzel.entferne("atlas")
            return
        if atlas is not None:
            atlas.setze("rolle", rolle)
        elif fm.wurzel is not None and fm.wurzel.paar("atlas") is not None:
            raise ZeilenFehler("Das atlas-Feld des Agenten ist keine Abbildung; die Werkstatt ändert es nicht.")
        else:
            fm.haenge_an(["atlas:", f"  rolle: {rolle}"])

    def _ersetze_text(self, datei: Textdatei, text: str) -> None:
        anfang = datei.text_ab()
        erste = anfang
        while erste < len(datei) and z.ist_leer(datei.zeilen[erste]):
            erste += 1
        neu = text.strip("\n")
        zeilen = neu.split("\n") if neu else []
        if erste == anfang and zeilen:
            zeilen = ["", *zeilen]
        datei.ersetze(erste, len(datei), zeilen)

    def _rolle_umbenennen(self, alt: str, neu: str, ziel: str) -> None:
        """Die abgeleitete Rolle zieht mit dem Agenten um (Name und Datei)."""
        agenten = self.kit.ableitung.get("agenten") if isinstance(self.kit.ableitung, dict) else None
        if isinstance(agenten, dict) and isinstance(agenten.get(alt), dict):
            eintrag = agenten.pop(alt)
            eintrag["datei"] = ziel
            agenten[neu] = eintrag

    @staticmethod
    def _setze_namen(datei: Textdatei, neu: str) -> tuple[int, Spanne] | None:
        """Setzt name im Frontmatter; (Zeile, Spanne des Werts), wenn das Feld vorher und nachher genau eine Zeile ist:
        alt vom Anfang des Werts bis zum Zeilenende (samt Kommentar, den setze nicht übernimmt), neu der Wert."""
        fm = datei.frontmatter()
        paar = fm.wurzel.paar("name") if fm.wurzel is not None else None
        wert = z.skalar(neu)
        if paar is None or not isinstance(paar[1], ScalarNode):
            fm.setze("name", wert)
            return None
        anfang, ende = fm.wurzel.bereich("name")
        a_von, a_bis = paar[1].start_mark.column, len(datei.text(anfang))
        fm.setze("name", wert)
        if ende - anfang != 1 or paar[1].start_mark.line != paar[1].end_mark.line:
            return None
        n_bis = len(datei.text(anfang))
        return anfang + 1, (a_von, a_bis, n_bis - len(wert), n_bis)

    def _eigenes_zitat_umbenennen(self, neu: str, vorher: list[str], nachher: list[str],
                                  namenszeile: tuple[int, Spanne] | None) -> None:
        """Liegt der Beleg der eigenen Rolle in der name-Zeile, die das Umbenennen geändert hat, folgt das Zitat seiner
        Stelle über die Spanne des Namens (zitat_aus_zeile); vorher/nachher: die Zeilen der Datei vor und nach dem
        Umbenennen."""
        eintrag = (self.kit.ableitung.get("agenten") or {}).get(neu) if isinstance(self.kit.ableitung, dict) else None
        beleg = eintrag.get("beleg") if isinstance(eintrag, dict) else None
        if namenszeile is None or len(vorher) != len(nachher) or belegzeile(beleg) != namenszeile[0]:
            return
        zeile, spanne = namenszeile
        if vorher[zeile - 1] != nachher[zeile - 1]:
            self._zitat_nachziehen(beleg, vorher[zeile - 1], nachher[zeile - 1], [spanne])

    @staticmethod
    def _zitat_nachziehen(beleg: dict, alt: str, neu: str, spannen: list[Spanne]) -> None:
        """Neues Zitat nach zitat_aus_zeile; ohne gültiges bleibt der Beleg, wie er ist (der Neuscan der Vorschau meldet
        ihn)."""
        zitat = zitat_aus_zeile(alt, neu, beleg.get("zitat"), spannen)
        if zitat is not None:
            beleg["zitat"] = zitat

    def _nennung(self, name: str) -> re.Pattern:
        return nennung(self.kit.kit_id, name)

    def _wer(self, name: str) -> tuple[str, str]:
        """Welcher Agent den Namen gerade trägt: ein Agent des Kits nach seinem Namen vor dem Satz, ein neu angelegter
        nach seinem Eintrag."""
        return self.buch.wer_heisst.get(name, ("kit", name))

    def _benenne_um(self, alt: str, neu: str, rel: str, datei: Textdatei) -> tuple[str, tuple[int, Spanne] | None]:
        """(neue Datei, Zeile und Spanne des Namens im Frontmatter nach _setze_namen)."""
        if neu in self.kit.agenten():
            raise SchreibFehler(f"Agent {neu} gibt es schon.")
        ziel = f"{self.kit.agenten_ordner}/{neu}.md"
        if ziel != rel and self.kit.existiert(ziel):
            raise SchreibFehler(f"{ziel} gibt es schon.")
        self.buch.wer_heisst[neu] = self._wer(alt)
        self.buch.wer_heisst.pop(alt, None)
        namenszeile = self._setze_namen(datei, neu)
        if ziel != rel:
            self.kit.loesche(rel)
            herkunft = next((a for a, n in self.buch.umbenannt.items() if n == rel), rel)
            self.buch.umbenannt.pop(herkunft, None)
            if herkunft != ziel:
                self.buch.umbenannt[herkunft] = ziel
        self.kit.schreibe_text(ziel, datei)
        muster = self._nennung(alt)
        geaenderte_zeilen: dict[str, dict[int, Ersetzung]] = {}  # Skill → Zeile → (vorher, nachher, Spannen)
        je_datei: dict[str, dict[int, Ersetzung]] = {}
        for skill, srel in self.kit.skills().items():
            try:
                sdatei = self.kit.textdatei(srel)
            except SchreibFehler:
                continue  # nicht als UTF-8 lesbar: bleibt, wie sie ist; Verweise darin meldet die Nachprüfung
            geaendert = self._ersetze_nennungen(sdatei, sdatei.text_ab(), neu, muster)
            try:
                self._benenne_in_schritten_um(sdatei, alt, neu)
            except ZeilenFehler:
                pass  # ohne gültiges Frontmatter gibt es keine eigenen Schritte; der Text darunter ist umbenannt
            if sdatei.als_bytes() == self.kit.lies(srel) or not self._schreibe_umbenannt(srel, sdatei, alt, skill):
                continue
            if geaendert:
                geaenderte_zeilen[skill] = je_datei[srel] = geaendert
        self._benenne_in_ableitung_um(alt, neu, geaenderte_zeilen)
        self._benenne_in_buchfuehrung_um(alt, neu, je_datei)
        self._benenne_in_agenten_um(alt, neu, ziel, muster)
        return ziel, namenszeile

    @staticmethod
    def _ersetze_nennungen(datei: Textdatei, ab: int, neu: str, muster: re.Pattern) -> dict[int, Ersetzung]:
        """Jede Nennung nach muster ab Zeile ab (0-basiert) durch neu; Zeile (1-basiert) → (vorher, nachher, Spannen)."""
        geaendert: dict[int, Ersetzung] = {}
        for i in range(ab, len(datei)):
            vorher = datei.text(i)
            nachher, spannen = ersetze_namen(vorher, neu, muster)
            if spannen:
                datei.ersetze(i, i + 1, [nachher])
                geaendert[i + 1] = (vorher, nachher, spannen)
        return geaendert

    def _schreibe_umbenannt(self, rel: str, datei: Textdatei, alt: str, skill: str | None = None) -> bool:
        """Schreibt eine Datei, in der das Umbenennen etwas geändert hat. Eine Datei, die die Werkstatt nicht schreibt
        (Symlink, Name außerhalb der Freigabeliste), bleibt, wie sie ist, und kommt in nicht_umbenannt: die Vorschau
        warnt davor, das Umbenennen gelingt. Nutzt aber der Ablauf dieser Skill (Ableitung oder atlas.schritte) den
        Agenten, bräche das Umbenennen ihn: dann scheitert es."""
        try:
            self.kit.pruefe_ziel(rel)
        except SchreibFehler as fehler:
            grund = self._grund_nicht_geschrieben(rel, fehler)
            if skill is not None and any(alt in agenten_von(s) for s in self.schritte_der_skill(skill, rel)):
                raise SchreibFehler(f"{grund}, das Umbenennen würde den Ablauf von {skill} brechen.")
            self.buch.nicht_umbenannt.append((rel, alt, grund))
            return False
        self.kit.schreibe_text(rel, datei)
        return True

    def _benenne_in_buchfuehrung_um(self, alt: str, neu: str, geaendert: dict[str, dict[int, Ersetzung]]) -> None:
        """Was frühere Einträge als weggefallen gemerkt haben (WS-M4), heißt danach wie im Text: Namen alt → neu, und
        Zitate in Zeilen, die das Umbenennen geändert hat (geaendert: Datei → Zeile → Ersetzung), folgen ihrer Stelle."""
        def namen(menge: set[str]) -> set[str]:
            return {neu if n == alt else n for n in menge}

        def zitat(rel: str, text: str) -> str:
            for vorher, nachher, spannen in geaendert.get(rel, {}).values():
                neu_text = abgebildet(vorher, nachher, text, spannen)
                if neu_text is not None:
                    return neu_text
            return text

        b = self.buch
        b.entfernt = [(eid, skill, rel, [zitat(rel, t) for t in zitate], namen(n))
                      for eid, skill, rel, zitate, n in b.entfernt]
        b.weg_genannt = [(eid, skill, rel, namen(n)) for eid, skill, rel, n in b.weg_genannt]
        for _eid, _skill, rel, _was, beleg in b.weg_belege:
            zeile = belegzeile(beleg)
            if zeile in geaendert.get(rel, {}):
                vorher, nachher, spannen = geaendert[rel][zeile]
                neu_text = abgebildet(vorher, nachher, beleg.get("zitat"), spannen)
                if neu_text is not None:
                    beleg["zitat"] = neu_text

    def _benenne_in_agenten_um(self, alt: str, neu: str, ziel: str, muster: re.Pattern) -> None:
        """Nennungen im Text der anderen Agenten (nach dem Frontmatter, dasselbe Muster wie in den Skills); hat sich die
        Zeile des Zitats einer abgeleiteten Rolle dabei geändert, folgt das Zitat seiner Stelle. Eine Datei, die die
        Werkstatt nicht schreibt, bleibt, wie sie ist (_schreibe_umbenannt)."""
        rollen = self.kit.ableitung.get("agenten") if isinstance(self.kit.ableitung, dict) else None
        for name, arel in self.kit.agenten().items():
            if arel == ziel:
                continue
            try:
                adatei = self.kit.textdatei(arel)
                anfang = adatei.text_ab()
            except (SchreibFehler, ZeilenFehler):
                continue  # unlesbar oder ohne gültiges Frontmatter: bleibt, wie sie ist; die Nachprüfung meldet Verweise
            geaendert = self._ersetze_nennungen(adatei, anfang, neu, muster)
            if not geaendert or not self._schreibe_umbenannt(arel, adatei, alt):
                continue
            rolle = rollen.get(name) if isinstance(rollen, dict) else None
            beleg = rolle.get("beleg") if isinstance(rolle, dict) else None
            if belegzeile(beleg) in geaendert:
                self._zitat_nachziehen(beleg, *geaendert[beleg["zeile"]])

    def _grund_nicht_geschrieben(self, rel: str, fehler: SchreibFehler) -> str:
        if not pfade.freigegeben(rel, self.kit.skills_ordner, self.kit.agenten_ordner):
            return "der Dateiname liegt außerhalb der Freigabeliste der Werkstatt"
        return str(fehler).removesuffix(".")

    def _benenne_in_schritten_um(self, datei: Textdatei, alt: str, neu: str) -> None:
        if not self._hat_eigene(datei):
            return
        # Die Werte einmal lesen; die Einträge (Zeilen im Frontmatter) nur für eine Änderung, danach stimmen sie neu.
        for i, schritt in enumerate(self._schritt_werte(datei)):
            parallel = schritt.get("parallel")
            if schritt.get("agent") == alt:
                self._eintraege(datei)[i].setze("agent", z.skalar(neu))
            if isinstance(parallel, list) and alt in parallel:
                self._eintraege(datei)[i].setze("parallel", z.flow_liste([neu if n == alt else n for n in parallel]))

    def _benenne_in_ableitung_um(self, alt: str, neu: str, geaendert: dict[str, dict[int, Ersetzung]]) -> None:
        """agent, parallel und die Schlüssel von belege.parallel; das Zitat eines Belegs in einer Zeile, die das
        Umbenennen geändert hat (geaendert: Skill → Zeile → (vorher, nachher, Spannen)), folgt seiner Stelle."""
        if not isinstance(self.kit.ableitung, dict):
            return
        for skill, eintrag in self.kit.ableitung.get("skills", {}).items():
            for schritt in abgeleitete_schritte(eintrag) or []:
                if not isinstance(schritt, dict):
                    continue
                if schritt.get("agent") == alt:
                    schritt["agent"] = neu
                if isinstance(schritt.get("parallel"), list):
                    schritt["parallel"] = [neu if n == alt else n for n in schritt["parallel"]]
                belege = schritt.get("belege") if isinstance(schritt.get("belege"), dict) else {}
                if isinstance(belege.get("parallel"), dict) and alt in belege["parallel"]:
                    belege["parallel"][neu] = belege["parallel"].pop(alt)
                zeilen = geaendert.get(skill, {})
                for beleg in self._alle_belege(schritt):
                    if belegzeile(beleg) in zeilen:
                        self._zitat_nachziehen(beleg, *zeilen[beleg["zeile"]])

    @staticmethod
    def _alle_belege(schritt: dict) -> list[dict]:
        liste = [schritt["beleg"]] if isinstance(schritt.get("beleg"), dict) else []
        belege = schritt.get("belege") if isinstance(schritt.get("belege"), dict) else {}
        for name, beleg in belege.items():
            if name == "parallel" and isinstance(beleg, dict):
                liste += [b for b in beleg.values() if isinstance(b, dict)]
            elif isinstance(beleg, dict):
                liste.append(beleg)
        return liste

    def agent_loeschen(self, op: dict, eid: str) -> None:
        name = op["name"]
        self.kit.loesche(self._agent(name))
        self.buch.geloeschte_agenten[name] = eid
        self.buch.wer_heisst.pop(name, None)

    def offene_nicht_umbenannt(self) -> list[tuple[str, str, str]]:
        """nicht_umbenannt gegen den Endzustand des Satzes, jeder Eintrag einmal: Die Warnung bleibt, wenn die Datei den
        alten Namen am Ende noch nennt und ihn dann niemand trägt oder ein anderer Agent als der, der ihn im Kit trug
        (neu angelegt, Namenstausch): die Nennung meint jetzt ihn. Trägt ihn am Ende wieder der Agent des Kits
        (Rückbenennung, auch über eine Kette), entfällt sie."""
        agenten = self.kit.agenten()
        offen = [(rel, alt, grund) for rel, alt, grund in self.buch.nicht_umbenannt
                 if not (alt in agenten and self._wer(alt) == ("kit", alt)) and self._nennt_noch(rel, alt)]
        return list(dict.fromkeys(offen))

    def _nennt_noch(self, rel: str, name: str) -> bool:
        if not self.kit.existiert(rel):
            return False
        try:
            datei = self.kit.textdatei(rel)
            anfang = datei.text_ab()
        except (SchreibFehler, ZeilenFehler):
            return True  # nicht mehr lesbar: die Warnung bleibt
        muster = self._nennung(name)
        return any(muster.search(datei.text(i)) for i in range(anfang, len(datei)))

    def noch_genutzt(self) -> dict[str, list[str]]:
        """Agenten, die gelöscht wurden und nach allen Einträgen noch ein Schritt nutzt: Eintrag → Stellen."""
        ergebnis: dict[str, list[str]] = {}
        vorhanden = self.kit.agenten()
        for name, eid in self.buch.geloeschte_agenten.items():
            if name in vorhanden:
                continue
            stellen = [f"{skill}/{s.get('id')}" for skill, rel in sorted(self.kit.skills().items())
                       for s in self.schritte_der_skill(skill, rel) if name in agenten_von(s)]
            if stellen:
                ergebnis[eid] = [name, *stellen]
        return ergebnis

