"""Schreiber für Skills mit eigenen Atlas-Angaben (WS-W4, WS-W5), ohne Ablauf (WS-W8) und neue Skills (WS-W9).

Atlas-Feld und nummerierte Prosa bleiben im Gleichschritt; gezählt wird mit ablauf.prosa_schritte des Scanners.
"""
from __future__ import annotations

import re

import ablauf
import zeilen as z
from schreiber_gleichschritt import agenten_von, ausfuehrender
from virtuelles_kit import SchreibFehler, mit_punkt, zeilen_der_anweisung
from vokabular import AUSFUEHRENDE, MENSCH_SATZ, finde_mensch_satz, finde_schleifen_satz, schleifen_satz, schritte_eigen
from zeilen import Textdatei, ZeilenFehler


class KonventionMixin:
    # ------------------------------------------------------------ Konvention: Positionen

    @staticmethod
    def _hat_eigene(datei: Textdatei) -> bool:
        return schritte_eigen(datei.frontmatter().werte().get("atlas"))

    def _schritte_knoten(self, datei: Textdatei):
        fm = datei.frontmatter()
        atlas = fm.wurzel.unter("atlas") if fm.wurzel is not None else None
        paar = atlas.paar("schritte") if atlas is not None else None
        if paar is None or not hasattr(paar[1], "value") or getattr(paar[1], "flow_style", False) \
                or not isinstance(paar[1].value, list):
            raise ZeilenFehler("atlas.schritte steht nicht als Liste untereinander; die Werkstatt ändert es nicht.")
        return fm, atlas, paar[1]

    def _eintraege(self, datei: Textdatei) -> list[z.Abbildung]:
        fm, _, liste = self._schritte_knoten(datei)
        return [z.Abbildung(fm, k) for k in liste.value]

    def _schritt_werte(self, datei: Textdatei) -> list[dict]:
        atlas = datei.frontmatter().werte().get("atlas")
        schritte = atlas.get("schritte") if isinstance(atlas, dict) else None
        return [s if isinstance(s, dict) else {} for s in schritte] if isinstance(schritte, list) else []

    def _strich_zeile(self, datei: Textdatei, eintrag: z.Abbildung) -> int:
        zeile = eintrag.fm.absolut(eintrag.knoten.start_mark.line)
        vorne = datei.text(zeile)[:eintrag.knoten.start_mark.column]
        if vorne.strip().startswith("-"):
            return zeile
        if zeile > 0 and datei.text(zeile - 1).strip() == "-":
            return zeile - 1
        raise ZeilenFehler("Ein Eintrag in atlas.schritte hat eine ungewohnte Form; die Werkstatt ändert ihn nicht.")

    def _einzuege(self, datei: Textdatei) -> tuple[int, int]:
        eintraege = self._eintraege(datei)
        if not eintraege:
            raise ZeilenFehler("atlas.schritte ist leer.")
        strich = self._strich_zeile(datei, eintraege[0])
        spalte_strich = datei.text(strich).index("-")
        spalte_feld = eintraege[0].knoten.start_mark.column if strich == eintraege[0].fm.absolut(
            eintraege[0].knoten.start_mark.line) else spalte_strich + 2
        return spalte_strich, spalte_feld

    @staticmethod
    def _ausfuehrender(schritt: dict) -> list[str]:
        if schritt.get("agent"):
            return [f"agent: {z.skalar(schritt['agent'])}"]
        if schritt.get("parallel"):
            return [f"parallel: {z.flow_liste(schritt['parallel'])}"]
        if schritt.get("werkzeug"):
            return [f"werkzeug: {z.skalar(schritt['werkzeug'])}"]
        if schritt.get("skill"):
            return [f"skill: {z.skalar(schritt['skill'])}"]
        return []

    def _eintrag_zeilen(self, schritt: dict, strich: int, feld: int) -> list[str]:
        felder = [f"titel: {z.skalar(schritt['titel'])}", *self._ausfuehrender(schritt)]
        if schritt.get("mensch"):
            felder.append(f"mensch: {schritt['mensch']}")
        bb = schritt.get("bei_befund")
        if bb:
            felder.append(f"bei_befund: {z.bei_befund(bb['zurueck'], bb.get('max'))}")
        kopf = " " * strich + "-" + " " * (feld - strich - 1) + f"id: {z.skalar(schritt['id'])}"
        return [kopf, *(" " * feld + f for f in felder)]

    # ------------------------------------------------------------ Konvention: Prosa

    def _prosa(self, datei: Textdatei, erwartet: int, skill: str) -> list:
        prosa = ablauf.prosa_schritte(datei.texte(), datei.text_ab() + 1)
        if len(prosa) != erwartet or any(p.nr != i + 1 for i, p in enumerate(prosa)):
            raise SchreibFehler(f"Die nummerierten Schritte im Text von {skill} passen nicht zu atlas.schritte. "
                                f"Die Werkstatt ändert die Skill erst, wenn beides übereinstimmt.")
        return prosa

    @staticmethod
    def _ist_fett(datei: Textdatei, prosa: list) -> bool:
        return bool(prosa) and bool(ablauf.PROSA_FETT.match(datei.text(prosa[0].zeile - 1)))

    @staticmethod
    def _block_ende(datei: Textdatei, anfang: int, grenze: int) -> int:
        i = anfang + 1
        while i < min(grenze, len(datei)):
            text = datei.text(i)
            if not text.strip() or text[0] not in " \t":
                break
            i += 1
        return i

    def _prosa_block(self, datei: Textdatei, prosa: list, n: int) -> tuple[int, int]:
        anfang = prosa[n].zeile - 1
        grenze = prosa[n + 1].zeile - 1 if n + 1 < len(prosa) else len(datei)
        return anfang, self._block_ende(datei, anfang, grenze)

    @staticmethod
    def _prosa_zeilen(einzug: str, nr: int, titel: str, anweisung: str, fett: bool) -> list[str]:
        teile = zeilen_der_anweisung(anweisung)
        erste = teile[0] if teile else ""
        if fett:
            kopf = f"{einzug}{nr}. **{mit_punkt(titel)}**" + (f" {erste}" if erste else "")
        else:
            kopf = f"{einzug}{nr}. {titel}: {erste}" if erste else f"{einzug}{nr}. {titel}"
        weiter = einzug + " " * len(f"{nr}. ")
        return [kopf, *(weiter + t for t in teile[1:])]

    def _nummeriere(self, datei: Textdatei) -> None:
        for i, p in enumerate(ablauf.prosa_schritte(datei.texte(), datei.text_ab() + 1)):
            if p.nr != i + 1:
                zeile = p.zeile - 1
                datei.ersetze(zeile, zeile + 1, [re.sub(r"^(\s*)\d+\.", rf"\g<1>{i + 1}.", datei.text(zeile), count=1)])


    def _konvention_einfuegen(self, skill: str, rel: str, op: dict) -> None:
        datei = self.kit.textdatei(rel)
        ids = [s.get("id") for s in self._schritt_werte(datei)]
        if not ids:
            # atlas.schritte: [] – der erste Schritt ersetzt die leere Liste wie bei einer Skill ohne Ablauf
            self._ohne_einfuegen(skill, rel, op)
            return
        k = self._position(skill, ids, op["nach"], op["schritt"]["id"])
        prosa = self._prosa(datei, len(ids), skill)
        fett = self._ist_fett(datei, prosa)
        einzug = re.match(r"^\s*", datei.text(prosa[0].zeile - 1)).group(0) if prosa else ""
        if k < len(prosa):
            ziel_prosa = prosa[k].zeile - 1
        else:
            ziel_prosa = self._prosa_block(datei, prosa, len(prosa) - 1)[1]
        datei.fuege_ein(ziel_prosa, self._prosa_zeilen(einzug, k + 1, op["schritt"]["titel"], op["anweisung"], fett))
        eintraege = self._eintraege(datei)
        strich, feld = self._einzuege(datei)
        if k == 0:
            ziel = self._strich_zeile(datei, eintraege[0])
        else:
            ziel = eintraege[k - 1].letzte() + 1
        datei.fuege_ein(ziel, self._eintrag_zeilen(op["schritt"], strich, feld))
        self._nummeriere(datei)
        self.kit.schreibe_text(rel, datei)

    def _ohne_einfuegen(self, skill: str, rel: str, op: dict) -> None:
        datei = self.kit.textdatei(rel)
        if op["nach"] is not None:
            raise SchreibFehler(f"Schritt {op['nach']} gibt es in {skill} nicht.", ("schritt", skill, op["nach"]))
        if ablauf.prosa_schritte(datei.texte(), datei.text_ab() + 1):
            raise SchreibFehler(f"Die Skill {skill} hat schon nummerierte Zeilen im Text, aber keine Atlas-Angaben. "
                                f"Leg die Atlas-Angaben von Hand an oder lass den Ablauf mit /atlas:init ableiten.")
        fm = datei.frontmatter()
        werte = fm.werte()
        atlas_wert = werte.get("atlas")
        if atlas_wert is not None and not isinstance(atlas_wert, dict):
            raise ZeilenFehler("Das atlas-Feld der Skill ist keine Abbildung; die Werkstatt ändert es nicht.")
        hat_nutzen = isinstance(atlas_wert, dict) and isinstance(atlas_wert.get("nutzen"), str)
        if not hat_nutzen and not op.get("nutzen"):
            raise SchreibFehler(f"Die Skill {skill} hat noch keine Atlas-Angaben. Gib nutzen mit an.")
        if atlas_wert is None:
            fm.haenge_an(["atlas:", f"  nutzen: {z.skalar(op['nutzen'])}", "  schritte:",
                          *self._eintrag_zeilen(op["schritt"], 4, 6)])
        else:
            atlas = fm.wurzel.unter("atlas")
            if not hat_nutzen:
                atlas.setze("nutzen", z.skalar(op["nutzen"]))
                atlas = datei.frontmatter().wurzel.unter("atlas")
            atlas.setze_zeilen("schritte", ["schritte:", *self._eintrag_zeilen(op["schritt"], 2, 4)])
        ende = len(datei)
        anhang = ["## Ablauf", "", *self._prosa_zeilen("", 1, op["schritt"]["titel"], op["anweisung"], True)]
        if ende and not z.ist_leer(datei.zeilen[-1]):
            anhang = ["", *anhang]
        datei.fuege_ein(ende, anhang)
        self.kit.schreibe_text(rel, datei)


    def _konvention_schritt(self, skill: str, datei: Textdatei, sid: str) -> int:
        ids = [s.get("id") for s in self._schritt_werte(datei)]
        if sid not in ids:
            raise SchreibFehler(f"Schritt {sid} gibt es in {skill} nicht.", ("schritt", skill, sid))
        return ids.index(sid)


    def _konvention_aendern(self, skill: str, rel: str, sid: str, op: dict, anweisung: str | None) -> None:
        datei = self.kit.textdatei(rel)
        n = self._konvention_schritt(skill, datei, sid)
        vorher = dict(self._schritt_werte(datei)[n])
        prosa = self._prosa(datei, len(self._schritt_werte(datei)), skill)
        if anweisung is not None:
            anfang, ende = self._prosa_block(datei, prosa, n)
            fett = self._ist_fett(datei, prosa)
            zeile = datei.text(anfang)
            einzug = re.match(r"^\s*", zeile).group(0)
            titel = op.get("titel")
            if titel is None:
                m = re.match(r"^\s*\d+\. \*\*(.*?)\*\*", zeile) if fett else None
                titel = m.group(1).rstrip(".") if m else self._schritt_werte(datei)[n].get("titel", "")
            datei.ersetze(anfang, ende, self._prosa_zeilen(einzug, n + 1, titel, anweisung, fett))
        if "titel" in op:
            self._eintraege(datei)[n].setze("titel", z.skalar(op["titel"]))
        if "mensch" in op:
            if op["mensch"] is None:
                self._eintraege(datei)[n].entferne("mensch")
            else:
                self._eintraege(datei)[n].setze("mensch", op["mensch"])
        neu = {f: op[f] for f in AUSFUEHRENDE if op.get(f) is not None}
        if any(f in op for f in AUSFUEHRENDE):
            for feld in AUSFUEHRENDE:
                if feld not in neu and self._eintraege(datei)[n].paar(feld) is not None:
                    self._eintraege(datei)[n].entferne(feld)
            for zeile in self._ausfuehrender(neu):
                feld, wert = zeile.split(": ", 1)
                self._eintraege(datei)[n].setze(feld, wert)
        # Der Prosa-Block sagt danach dasselbe wie atlas.schritte (WS-W5): erzeugte Sätze, Namen, sonst Warnung.
        zeilen = list(range(*self._prosa_block(datei, self._prosa(datei, len(self._schritt_werte(datei)), skill), n)))
        alt = ausfuehrender(vorher)
        if any(f in op for f in AUSFUEHRENDE) and neu != alt:
            self._ausfuehrenden_tauschen(datei, zeilen, alt, neu)
            self._merke_weg(skill, rel, [], agenten_von(alt) - agenten_von(neu), warnen=True)
        if "mensch" in op and vorher.get("mensch") and op["mensch"] != vorher.get("mensch"):
            self._satz_tauschen(datei, zeilen, lambda t: finde_mensch_satz(t, vorher["mensch"]),
                                MENSCH_SATZ[op["mensch"]] if op["mensch"] else None)
        alter_titel = vorher.get("titel")
        if "titel" in op and isinstance(alter_titel, str) and alter_titel and op["titel"] != alter_titel:
            # Schleifen-Sätze anderer Schritte, die den alten Titel nennen, nennen den neuen (WS-W12).
            for i, s in enumerate(self._schritt_werte(datei)):
                bb = s.get("bei_befund")
                if i != n and isinstance(bb, dict) and bb.get("zurueck") == sid:
                    self._schleifen_satz(datei, skill, i, schleifen_satz(op["titel"], bb.get("max")), anhaengen=False,
                                         titel=[alter_titel])
        self.kit.schreibe_text(rel, datei)


    def _konvention_entfernen(self, skill: str, rel: str, sid: str) -> None:
        datei = self.kit.textdatei(rel)
        n = self._konvention_schritt(skill, datei, sid)
        werte = self._schritt_werte(datei)
        if len(werte) == 1:
            raise SchreibFehler(f"Schritt {sid} ist der einzige Schritt von {skill} und lässt sich nicht entfernen.")
        vorgaenger = werte[n - 1].get("id") if n > 0 else None
        for i, s in enumerate(werte):
            bb = s.get("bei_befund")
            if i != n and isinstance(bb, dict) and bb.get("zurueck") == sid:
                # Der Schleifen-Satz im Text zieht mit (WS-W12): zum Vorgänger umgelegt bzw. weg.
                if vorgaenger is not None and n - 1 < i:
                    self._eintraege(datei)[i].setze("bei_befund", z.bei_befund(vorgaenger, bb.get("max")))
                    titel = werte[n - 1].get("titel") or vorgaenger
                    self._schleifen_satz(datei, skill, i, schleifen_satz(titel, bb.get("max")), anhaengen=False)
                else:
                    self._eintraege(datei)[i].entferne("bei_befund")
                    self._schleifen_satz(datei, skill, i, None, anhaengen=False)
        prosa = self._prosa(datei, len(werte), skill)
        datei.entferne(*self._prosa_block(datei, prosa, n))
        eintrag = self._eintraege(datei)[n]
        datei.entferne(self._strich_zeile(datei, eintrag), eintrag.letzte() + 1)
        self._nummeriere(datei)
        self.kit.schreibe_text(rel, datei)


    def _konvention_schleife(self, skill: str, rel: str, op: dict) -> None:
        datei = self.kit.textdatei(rel)
        n = self._konvention_schritt(skill, datei, op["von"])
        hatte = self._schritt_werte(datei)[n].get("bei_befund")
        satz = None
        if op["zurueck"] is None:
            self._eintraege(datei)[n].entferne("bei_befund")
        else:
            ziel = self._konvention_schritt(skill, datei, op["zurueck"])
            if ziel >= n:
                raise SchreibFehler(f"Die Schleife von {op['von']} zurück zu {op['zurueck']} führt nicht zurück.")
            self._eintraege(datei)[n].setze("bei_befund", z.bei_befund(op["zurueck"], op.get("max")))
            titel = self._schritt_werte(datei)[ziel].get("titel") or op["zurueck"]
            satz = schleifen_satz(titel, op.get("max"))
        if hatte or op.get("satz"):
            self._schleifen_satz(datei, skill, n, satz, anhaengen=bool(op.get("satz")))
        self.kit.schreibe_text(rel, datei)

    def _schleifen_satz(self, datei: Textdatei, skill: str, n: int, satz: str | None, anhaengen: bool,
                        titel: list[str] | None = None) -> None:
        """WS-W5: Ein Schleifen-Satz im Prosa-Block des Schritts (auch ohne Anführungszeichen; mit titel nur einer zu
        diesen Titeln) wird ersetzt bzw. beim Entfernen gestrichen; mit satz und anhaengen (so schickt es die Seite von
        Hand) kommt er sonst ans Ende der letzten Zeile."""
        prosa = self._prosa(datei, len(self._schritt_werte(datei)), skill)
        anfang, ende = self._prosa_block(datei, prosa, n)
        if self._satz_tauschen(datei, list(range(anfang, ende)), lambda t: finde_schleifen_satz(t, titel),
                               satz) is not None:
            return
        if satz and anhaengen:
            letzte = ende - 1
            datei.ersetze(letzte, letzte + 1, [f"{datei.text(letzte).rstrip()} {satz}"])

    # ------------------------------------------------------------ neue Skill

    def skill_anlegen(self, op: dict, _eid: str) -> None:
        name = op["name"]
        rel = f"{self.kit.skills_ordner}/{name}/SKILL.md"
        if name in self.kit.skills() or self.kit.existiert(rel) or (self.kit.wurzel / self.kit.skills_ordner / name).exists():
            raise SchreibFehler(f"Skill {name} gibt es schon.")
        ids: list[str] = []
        for s in op["schritte"]:
            if s["id"] in ids:
                raise SchreibFehler(f"Schritt {s['id']} gibt es in {name} schon.")
            self._pruefe_ausfuehrende(s, name)
            bb = s.get("bei_befund")
            if bb and bb["zurueck"] not in ids:
                raise SchreibFehler(f"Die Schleife von {s['id']} zurück zu {bb['zurueck']} führt nicht zurück.")
            ids.append(s["id"])
        kopf = ["---", f"name: {z.skalar(name)}", f"description: {z.skalar(op['beschreibung'])}", "atlas:",
                f"  nutzen: {z.skalar(op['nutzen'])}"]
        for feld in ("eingabe", "ergebnis"):
            if op.get(feld):
                kopf.append(f"  {feld}: {z.flow_liste(op[feld])}")
        kopf.append("  schritte:")
        for s in op["schritte"]:
            kopf += self._eintrag_zeilen(s, 4, 6)
        prosa = []
        for i, s in enumerate(op["schritte"]):
            prosa += self._prosa_zeilen("", i + 1, s["titel"], s["anweisung"], True)
        zeilen = [*kopf, "---", "", "## Ablauf", "", *prosa]
        self.kit.schreibe(rel, ("\n".join(zeilen) + "\n").encode("utf-8"))

