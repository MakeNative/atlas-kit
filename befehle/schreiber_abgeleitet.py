"""Schreiber für Skills mit abgeleitetem Ablauf (WS-W6, WS-W7): Abschnitte an der Belegstelle, Belege in der Ableitung."""
from __future__ import annotations

import re

import ablauf
import zeilen as z
from ableitung import belegzeile
from schreiber_gleichschritt import agenten_von, ausfuehrender
from virtuelles_kit import SchreibFehler, mit_punkt, zeilen_der_anweisung
from vokabular import (AUSFUEHRENDE, MENSCH_SATZ, aufruf_kopf, finde_mensch_satz, finde_schleifen_satz, mehr_als_mensch_satz,
                       schleifen_satz)
from zeilen import Textdatei, ZeilenFehler


class AbgeleitetMixin:
    def _abgeleiteter_schritt(self, skill: str, sid: str) -> tuple[list, int]:
        schritte = self.kit.ableitung_von(skill)
        ids = [s.get("id") if isinstance(s, dict) else None for s in schritte]
        if sid not in ids:
            raise SchreibFehler(f"Schritt {sid} gibt es in {skill} nicht.", ("schritt", skill, sid))
        return schritte, ids.index(sid)

    @staticmethod
    def _nicht_der_einzige(skill: str, sid: str, schritte: list) -> None:
        """Wie in der Konvention (WS-W5): Der einzige Schritt einer Skill lässt sich nicht entfernen."""
        if len(schritte) == 1:
            raise SchreibFehler(f"Schritt {sid} ist der einzige Schritt von {skill} und lässt sich nicht entfernen.")

    # ------------------------------------------------------------ abgeleiteter Ablauf

    def _abschnitt(self, datei: Textdatei, schritt: dict, unklar: str) -> tuple[int, int, int]:
        """(Anfang, Ende exklusiv, Ebene) des Abschnitts, der mit der Belegzeile des Schritts beginnt."""
        zeile = belegzeile(schritt.get("beleg"))
        if zeile is None or not 1 <= zeile <= len(datei):
            raise SchreibFehler(unklar)
        ebene = z.ueberschrift(datei.zeilen[zeile - 1])
        if ebene is None:
            raise SchreibFehler(unklar)
        ende = len(datei)
        for i in range(zeile, len(datei)):
            andere = z.ueberschrift(datei.zeilen[i])
            if andere is not None and andere <= ebene:
                ende = i
                break
        return zeile - 1, ende, ebene

    def _verschiebe(self, schritte: list, ab: int, um: int) -> None:
        """Alle Belege mit Zeile > ab (1-basiert) um 'um' verschieben."""
        for schritt in schritte:
            if isinstance(schritt, dict):
                for beleg in self._alle_belege(schritt):
                    if (belegzeile(beleg) or 0) > ab:
                        beleg["zeile"] += um

    @staticmethod
    def _startsatz(schritt: dict, anweisung: str) -> list[str]:
        teile = zeilen_der_anweisung(anweisung)
        rest = teile[1:]
        erste = teile[0] if teile else ""
        kopf = aufruf_kopf(schritt)
        if kopf is None:
            return teile
        return [f"{kopf}: {erste}" if erste else f"{kopf}.", *rest]

    @staticmethod
    def _belege_fuer(schritt: dict, zeile: int, text: str) -> dict:
        beleg = {"zeile": zeile, "zitat": text.strip()}
        if schritt.get("agent"):
            return {"agent": beleg}
        if schritt.get("parallel"):
            return {"parallel": {n: dict(beleg) for n in schritt["parallel"]}}
        if schritt.get("werkzeug"):
            return {"werkzeug": beleg}
        if schritt.get("skill"):
            return {"skill": beleg}
        return {}

    def _abgeleitet_einfuegen(self, skill: str, rel: str, op: dict) -> None:
        schritte = self.kit.ableitung_von(skill)
        ids = [s.get("id") if isinstance(s, dict) else None for s in schritte]
        k = self._position(skill, ids, op["nach"], op["schritt"]["id"])
        datei = self.kit.textdatei(rel)
        fehlt = (f"Die Schritte von {skill} stehen nicht als eigene Abschnitte im Text. "
                 f"Einfügen geht hier mit KI: „Mit KI einfügen“ am Kasten oder Pfeil.")
        nachbarn = [schritte[i] for i in (k - 1, k) if 0 <= i < len(schritte)]
        abschnitte = [self._abschnitt(datei, s, fehlt) for s in nachbarn]
        ebene = abschnitte[-1][2]
        if k < len(schritte):
            ziel = abschnitte[-1][0]
        else:
            ziel = abschnitte[0][1]
        schritt = op["schritt"]
        hat_ausfuehrenden = any(schritt.get(f) for f in AUSFUEHRENDE)
        anweisung = op["anweisung"]
        mensch = [MENSCH_SATZ[schritt["mensch"]]] if schritt.get("mensch") else []
        if mensch:
            # WS-W6: der feste Satz steht einmal; die Anweisung nur mit dem, was sie mehr sagt.
            anweisung = mehr_als_mensch_satz(anweisung, schritt["mensch"])
        satz = self._startsatz(schritt, anweisung)
        # Ohne Ausführenden steht der Mensch-Satz vorn, sonst nach dem Startsatz.
        zeilen = [*mensch, *satz] if mensch and not hat_ausfuehrenden else [*satz, *mensch]
        block = ["#" * ebene + " " + schritt["titel"], "", *zeilen, ""]
        if ziel == len(datei) and ziel and not z.ist_leer(datei.zeilen[-1]):
            block = ["", *block[:-1]]
        datei.fuege_ein(ziel, block)
        versatz = 1 if block[0] == "" else 0
        self._verschiebe(schritte, ziel, len(block))
        kopf = ziel + versatz + 1
        neu = {"id": schritt["id"], "titel": schritt["titel"]}
        for feld in AUSFUEHRENDE:
            if schritt.get(feld):
                neu[feld] = schritt[feld]
        if schritt.get("mensch"):
            neu["mensch"] = schritt["mensch"]
        neu["beleg"] = {"zeile": kopf, "zitat": block[versatz]}
        belege = self._belege_fuer(schritt, kopf + 2, satz[0]) if satz and hat_ausfuehrenden else {}
        if mensch:
            belege["mensch"] = {"zeile": kopf + 2 + zeilen.index(mensch[0]), "zitat": mensch[0]}
        if belege:
            neu["belege"] = belege
        schritte.insert(k, neu)
        self.kit.schreibe_text(rel, datei)

    def _abgeleitet_entfernen(self, skill: str, rel: str, sid: str) -> None:
        schritte, n = self._abgeleiteter_schritt(skill, sid)
        self._nicht_der_einzige(skill, sid, schritte)
        datei = self.kit.textdatei(rel)
        unklar = f"Wo der Abschnitt von {sid} endet, ist nicht eindeutig. Entfernen geht hier mit KI: „Mit KI ändern“ am Kasten."
        anfang, ende, _ = self._abschnitt(datei, schritte[n], unklar)
        for i, anderer in enumerate(schritte):
            if i != n and isinstance(anderer, dict):
                if any(anfang < (belegzeile(b) or 0) <= ende for b in self._alle_belege(anderer)):
                    raise SchreibFehler(unklar)
        self._schleifen_anderer_nachziehen(skill, rel, datei, schritte, n)
        anfang, ende, _ = self._abschnitt(datei, schritte[n], unklar)  # der Text davor kann sich verschoben haben
        datei.entferne(anfang, ende)
        del schritte[n]
        self._verschiebe(schritte, ende, -(ende - anfang))
        self.kit.schreibe_text(rel, datei)

    @staticmethod
    def _schleifen_umlegen(schritte: list, n: int) -> None:
        """Schleifen auf den Schritt n zeigen danach auf seinen Vorgänger, wenn der vor dem Schleifen-Schritt liegt,
        sonst entfallen sie (WS-W5)."""
        sid = schritte[n].get("id")
        vorgaenger = schritte[n - 1].get("id") if n > 0 and isinstance(schritte[n - 1], dict) else None
        for i, anderer in enumerate(schritte):
            bb = anderer.get("bei_befund") if isinstance(anderer, dict) else None
            if i != n and isinstance(bb, dict) and bb.get("zurueck") == sid:
                if vorgaenger is not None and n - 1 < i:
                    bb["zurueck"] = vorgaenger
                else:
                    anderer.pop("bei_befund")
                    if isinstance(anderer.get("belege"), dict):
                        anderer["belege"].pop("zurueck", None)

    def _schleifen_anderer_nachziehen(self, skill: str, rel: str, datei: Textdatei, schritte: list, n: int) -> None:
        """Wie _schleifen_umlegen, dazu der Text im Gleichschritt (WS-W12): Der Schleifen-Satz eines umgelegten Schritts
        nennt danach den Vorgänger, der einer entfallenen Schleife fällt weg, Beleg zurueck jeweils mit."""
        sid = schritte[n].get("id")
        vorher = [(a, a.get("bei_befund")) for i, a in enumerate(schritte) if i != n and isinstance(a, dict)
                  and isinstance(a.get("bei_befund"), dict) and a["bei_befund"].get("zurueck") == sid]
        vorgaenger = schritte[n - 1] if n > 0 and isinstance(schritte[n - 1], dict) else None
        self._schleifen_umlegen(schritte, n)
        for anderer, hatte in vorher:
            bb = anderer.get("bei_befund")
            satz = schleifen_satz(vorgaenger.get("titel", bb["zurueck"]), bb.get("max")) if bb and vorgaenger else None
            unklar = f"Wo der Abschnitt von {anderer.get('id')} endet, ist nicht eindeutig. Entfernen geht hier mit KI: " \
                     f"„Mit KI ändern“ am Kasten."
            self._schleife_im_text(skill, rel, datei, schritte, anderer, hatte, satz, unklar)

    def _titel_in_schleifen(self, datei: Textdatei, schritte: list, sid: str, alt: str, neu: str) -> None:
        """Neuer Titel eines Schritts: Ein Schleifen-Satz anderer Schritte, der ihn mit dem alten Titel nennt, nennt den
        neuen (WS-W12), Beleg zurueck mit; ein Satz, der den Schritt anders nennt (Schritt 2), bleibt."""
        for anderer in schritte:
            bb = anderer.get("bei_befund") if isinstance(anderer, dict) else None
            if not isinstance(bb, dict) or bb.get("zurueck") != sid:
                continue
            satz = schleifen_satz(neu, bb.get("max"))
            treffer = self._satz_tauschen(datei, self._zeilen_des_schritts(datei, anderer),
                                          lambda t: finde_schleifen_satz(t, [alt]), satz)
            if treffer is None:
                continue
            self._nach_tausch(datei, schritte, treffer, None)
            if not isinstance(anderer.get("belege"), dict):
                anderer["belege"] = {}
            anderer["belege"]["zurueck"] = {"zeile": treffer.zeile + 1, "zitat": satz}

    def _haenge_an_abschnitt(self, datei: Textdatei, schritte: list, schritt: dict, zeilen: list[str],
                             unklar: str) -> int:
        """Hängt Zeilen ans Ende des Abschnitts (nach seiner letzten nicht leeren Zeile); 1-basierte erste Zeile."""
        anfang, ende, _ = self._abschnitt(datei, schritt, unklar)
        letzte = ende - 1
        while letzte > anfang and z.ist_leer(datei.zeilen[letzte]):
            letzte -= 1
        datei.fuege_ein(letzte + 1, zeilen)
        self._verschiebe(schritte, letzte + 1, len(zeilen))
        return letzte + 2

    def _zeilen_des_schritts(self, datei: Textdatei, schritt: dict) -> list[int]:
        """Zeilen (0-basiert), in denen der Text den Schritt beschreibt: sein Abschnitt, sonst die Zeilen seiner Belege."""
        try:
            anfang, ende, _ = self._abschnitt(datei, schritt, "")
            return list(range(anfang, ende))
        except SchreibFehler:
            return sorted({b["zeile"] - 1 for b in self._alle_belege(schritt)
                           if 1 <= (belegzeile(b) or 0) <= len(datei)})

    def _nach_tausch(self, datei: Textdatei, schritte: list, treffer, alt_text: str | None) -> None:
        """Belege nach einem Abgleich nachziehen: eine weggefallene Zeile rückt alle späteren nach, Zitate in einer
        geänderten Zeile bekommen den neuen Wortlaut."""
        if treffer.entfernt:
            self._verschiebe(schritte, treffer.zeile + 1, -1)
            return
        neu_text = datei.text(treffer.zeile)
        for anderer in schritte:
            for beleg in self._alle_belege(anderer) if isinstance(anderer, dict) else []:
                zitat = beleg.get("zitat")
                if belegzeile(beleg) == treffer.zeile + 1 and isinstance(zitat, str) and zitat.strip() not in neu_text:
                    if alt_text is not None and zitat.strip() in alt_text:
                        beleg["zitat"] = neu_text.strip()

    def _abgeleitet_aendern(self, skill: str, rel: str, sid: str, op: dict, anweisung: str | None) -> None:
        schritte, n = self._abgeleiteter_schritt(skill, sid)
        schritt = schritte[n]
        datei = self.kit.textdatei(rel)
        unklar = f"Wo der Abschnitt von {sid} endet, ist nicht eindeutig. Ändern geht hier mit KI: „Mit KI ändern“ am Kasten."
        if "titel" in op:
            alter_titel = schritt.get("titel")
            schritt["titel"] = op["titel"]
            if isinstance(alter_titel, str) and alter_titel and alter_titel != op["titel"]:
                self._titel_in_schleifen(datei, schritte, sid, alter_titel, op["titel"])
        if not isinstance(schritt.get("belege"), dict):
            schritt["belege"] = {}
        belege = schritt["belege"]
        neu = {f: op[f] for f in AUSFUEHRENDE if op.get(f) is not None}
        if any(f in op for f in AUSFUEHRENDE) and neu != ausfuehrender(schritt):
            alt = ausfuehrender(schritt)
            for feld in AUSFUEHRENDE:
                schritt.pop(feld, None)
                belege.pop(feld, None)
            zeilen = self._zeilen_des_schritts(datei, schritt)
            alte_texte = {i: datei.text(i) for i in zeilen}
            treffer = self._ausfuehrenden_tauschen(datei, zeilen, alt, neu)
            if treffer is not None:
                self._nach_tausch(datei, schritte, treffer, alte_texte.get(treffer.zeile))
            self._merke_weg(skill, rel, [], agenten_von(alt) - agenten_von(neu), warnen=True)
            if neu and (treffer is None or treffer.entfernt):
                satz = self._startsatz(neu, anweisung or "")
                erste = self._haenge_an_abschnitt(datei, schritte, schritt, satz, unklar)
                belege.update(self._belege_fuer(neu, erste, satz[0]))
            elif neu:
                belege.update(self._belege_fuer(neu, treffer.zeile + 1, datei.text(treffer.zeile)))
                if anweisung:
                    self._haenge_an_abschnitt(datei, schritte, schritt, zeilen_der_anweisung(anweisung), unklar)
            elif anweisung:
                self._haenge_an_abschnitt(datei, schritte, schritt, zeilen_der_anweisung(anweisung), unklar)
            schritt.update(neu)
        elif anweisung:
            self._haenge_an_abschnitt(datei, schritte, schritt, zeilen_der_anweisung(anweisung), unklar)
        if "mensch" in op and op["mensch"] != schritt.get("mensch"):
            self._abgeleitet_mensch(skill, rel, datei, schritte, schritt, op["mensch"], unklar)
        if not belege:
            schritt.pop("belege", None)
        self.kit.schreibe_text(rel, datei)

    def _abgeleitet_mensch(self, skill: str, rel: str, datei: Textdatei, schritte: list, schritt: dict,
                           wert: str | None, unklar: str) -> None:
        belege = schritt["belege"]
        alt = schritt.pop("mensch", None)
        alt_beleg = belege.pop("mensch", None)
        treffer = None
        if alt:
            treffer = self._satz_tauschen(datei, self._zeilen_des_schritts(datei, schritt),
                                          lambda t: finde_mensch_satz(t, alt), MENSCH_SATZ[wert] if wert else None)
        if treffer is not None:
            self._nach_tausch(datei, schritte, treffer, None)
        elif alt_beleg is not None:
            self._merke_weg(skill, rel, [alt_beleg], set(), "der Mensch-Schritt", schritt.get("id", ""), warnen=True)
        if wert is None:
            return
        satz = MENSCH_SATZ[wert]
        if treffer is not None and not treffer.entfernt:
            erste = treffer.zeile + 1
        else:
            erste = self._haenge_an_abschnitt(datei, schritte, schritt, [satz], unklar)
        schritt["mensch"] = wert
        belege["mensch"] = {"zeile": erste, "zitat": satz}

    def _abgeleitet_schleife(self, skill: str, rel: str, op: dict) -> None:
        schritte, n = self._abgeleiteter_schritt(skill, op["von"])
        schritt = schritte[n]
        satz = None
        if op["zurueck"] is not None:
            _, ziel = self._abgeleiteter_schritt(skill, op["zurueck"])
            if ziel >= n:
                raise SchreibFehler(f"Die Schleife von {op['von']} zurück zu {op['zurueck']} führt nicht zurück.")
            satz = schleifen_satz(schritte[ziel].get("titel", op["zurueck"]), op.get("max"))
        datei = self.kit.textdatei(rel)
        unklar = f"Wo der Abschnitt von {op['von']} endet, ist nicht eindeutig. Ändern geht hier mit KI: „Mit KI ändern“ am Kasten."
        hatte = schritt.pop("bei_befund", None)
        if satz is not None:
            grenze = op.get("max")
            schritt["bei_befund"] = {"zurueck": op["zurueck"], **({"max": grenze} if grenze is not None else {})}
        self._schleife_im_text(skill, rel, datei, schritte, schritt, hatte, satz, unklar)
        self.kit.schreibe_text(rel, datei)

    def _schleife_im_text(self, skill: str, rel: str, datei: Textdatei, schritte: list, schritt: dict,
                          hatte: dict | None, satz: str | None, unklar: str) -> None:
        """Schleifen-Satz des Schritts nach WS-W5: ersetzt bzw. entfernt, sonst (mit satz) angehängt; Beleg zurueck
        danach auf dem neuen Satz. Stand ein alter Beleg ohne erkennbaren Satz da, warnt die Vorschau."""
        if not isinstance(schritt.get("belege"), dict):
            schritt["belege"] = {}
        belege = schritt["belege"]
        alt_beleg = belege.pop("zurueck", None)
        treffer = self._satz_tauschen(datei, self._zeilen_des_schritts(datei, schritt), finde_schleifen_satz, satz) \
            if hatte is not None or satz is not None else None
        if treffer is not None:
            self._nach_tausch(datei, schritte, treffer, None)
        elif hatte is not None and alt_beleg is not None:
            self._merke_weg(skill, rel, [alt_beleg], set(), "die Schleife", schritt.get("id", ""), warnen=True)
        if satz is not None:
            if treffer is not None and not treffer.entfernt:
                erste = treffer.zeile + 1
            else:
                erste = self._haenge_an_abschnitt(datei, schritte, schritt, [satz], unklar)
            belege["zurueck"] = {"zeile": erste, "zitat": satz}
        if not belege:
            schritt.pop("belege")
