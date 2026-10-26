from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping

import plattform
import texte
import werkstatt_auswertung as auswertung_form
import werkstatt_bereinigen as bereinigen
import werkstatt_einstellungen as einstellungen_modul
import werkstatt_gedaechtnis as gd
import werkstatt_kennzahlen as kennzahlen_modul
import werkstatt_lauf_sicht as sicht
import werkstatt_laeufe
import werkstatt_pfade as pfade
from werkstatt_auftraege import AUSWERTEN, AuftragFehler, skill_aus_bezug
from werkstatt_kern import WerkstattFehler, zeit_um_beim_anlegen

LAEUFE_HOECHSTENS = 20
EREIGNISSE_HOECHSTENS = 64 * 1024 * 1024
LAUF_JSON_HOECHSTENS = 256 * 1024
BAUPLAN_HOECHSTENS = 16 * 1024 * 1024
EREIGNISSE_DATEI_HOECHSTENS = 32 * 1024 * 1024
TEXT_HOECHSTENS = 2000
AUSWAHL_HOECHSTENS = 30
ZEIT_PRUEFUNG_S = 60.0
ZUKUNFT_MINUTEN = 5
ABLAUF_FEHLT = "Diesen Ablauf gibt es im Kit nicht."
WAHR_ODER_FALSCH = "Gib logs oder auswertung als wahr oder falsch an."
BEZUG_FORM = "bezug muss „Ablauf <skill>“ sein."
TEXT_LANG = f"text hat beim Auswerten höchstens {TEXT_HOECHSTENS} Zeichen."
AUSWAHL_FORM = (f"auswahl ist eine Liste mit 1 bis {AUSWAHL_HOECHSTENS} verschiedenen Memory-Einträgen dieser "
                f"Auswertung.")
LAUF_DATEIEN = (werkstatt_laeufe.LAUF_JSON, werkstatt_laeufe.BAUPLAN_JSON, werkstatt_laeufe.EREIGNISSE)


def auswertung_aus(skill: str) -> str:
    return f"Die Auswertung ist für {skill} aus. Schalte sie unter „Memory und Logs“ ein."


def ohne_logs(skill: str) -> str:
    return (f"Für {skill} gibt es keine Läufe mit Logs. Ohne Logs kann der Auswerter nichts bewerten. Starte die "
            f"Pipeline in der Werkstatt mit „Lauf starten“ (Logs an); Läufe außerhalb der Werkstatt werden nicht "
            f"protokolliert.")


def auftragstext(skill: str, text: str | None) -> str:
    satz = f"Werte die Läufe von {skill} aus."
    return f"{satz} Dazu: {text.strip()}" if text and text.strip() else satz


def _pruefe_frist(ende: float) -> None:
    if time.monotonic() >= ende:
        raise WerkstattFehler(503, {"fehler": zeit_um_beim_anlegen()})


def _protokoll(zeile: str) -> None:
    sys.stderr.write(zeile + "\n")
    sys.stderr.flush()


class Auswerten:
    def __init__(self, werkstatt, laeufe: werkstatt_laeufe.Laeufe, einstellungen: einstellungen_modul.Einstellungen,
                 register: gd.Register, sende: Callable[[str, dict], None],
                 umgebung: Mapping[str, str] | None = None) -> None:
        self.werkstatt = werkstatt
        self.laeufe = laeufe
        self.einstellungen = einstellungen
        self.register = register
        self.sende = sende
        self.umgebung = os.environ if umgebung is None else umgebung
        self._daten: dict[str, auswertung_form.Arbeitsdaten] = {}
        self._sperre = threading.Lock()
        self._takt_sperre = threading.RLock()
        self._zuletzt_geprueft = time.monotonic()
        self._zeit_sperre = threading.Lock()
        self._an_der_grenze: set[str] = set()

    @property
    def auftraege(self):
        return self.werkstatt.auftraege

    def _skills(self, bauplan: dict | None = None) -> list[str]:
        bauplan = bauplan if bauplan is not None else self.werkstatt.bauplan()
        return [k["name"] for k in bauplan.get("knoten", [])
                if isinstance(k, dict) and k.get("typ") == "skill" and isinstance(k.get("name"), str)]

    @staticmethod
    def _schluessel(lauf: werkstatt_laeufe.Abgeschlossen) -> tuple[datetime, str]:
        return lauf.gestartet_zeit(), lauf.id

    def _grenze(self, laeufe: list[werkstatt_laeufe.Abgeschlossen], bis: str) -> tuple[datetime, str] | None:
        bekannt = next((lauf for lauf in laeufe if lauf.id == bis), None)
        if bekannt is not None:
            return self._schluessel(bekannt)
        try:
            return datetime.strptime(bis[1:16], "%Y%m%d-%H%M%S").astimezone(), bis
        except ValueError:
            return None

    def _juenger(self, laeufe: list[werkstatt_laeufe.Abgeschlossen],
                 bis: str | None) -> list[werkstatt_laeufe.Abgeschlossen]:
        grenze = self._grenze(laeufe, bis) if bis is not None else None
        if bis is not None and grenze is None:
            return list(laeufe)
        return [lauf for lauf in laeufe if grenze is None or self._schluessel(lauf) > grenze]

    def _neue(self, laeufe: list[werkstatt_laeufe.Abgeschlossen], bis: str | None) -> int:
        return len(self._juenger(laeufe, bis))

    def _zaehlende(self, skill: str) -> list[werkstatt_laeufe.Abgeschlossen]:
        return [lauf for lauf in self.laeufe.abgeschlossene(skill)
                if lauf.zustand == sicht.BEENDET and lauf.agentenlaeufe >= 1]

    def _offen(self, pipeline: einstellungen_modul.Pipeline,
               zaehlende: list[werkstatt_laeufe.Abgeschlossen]) -> list[werkstatt_laeufe.Abgeschlossen]:
        if pipeline.ausgeloest_fehlt:
            return []
        return self._juenger(zaehlende, pipeline.ausgeloest_bis)

    @staticmethod
    def _in_der_zukunft(pipeline: einstellungen_modul.Pipeline, jetzt: datetime) -> bool:
        zuletzt = einstellungen_modul.zeit_von(pipeline.zuletzt_gestellt)
        return zuletzt is not None and zuletzt > jetzt + timedelta(minutes=ZUKUNFT_MINUTEN)

    def _zuletzt(self, pipeline: einstellungen_modul.Pipeline, jetzt: datetime) -> datetime | None:
        if self._in_der_zukunft(pipeline, jetzt):
            return jetzt
        return einstellungen_modul.zeit_von(pipeline.zuletzt_gestellt)

    def _naechste(self, pipeline: einstellungen_modul.Pipeline, offen: int, jetzt: datetime) -> dict | None:
        art = pipeline.takt["art"]
        if not pipeline.auswertung or art == "klick":
            return None
        if art == "jeder_lauf":
            return {"art": "lauf"}
        if art == "alle_n":
            return {"art": "laeufe", "fehlen": max(1, pipeline.takt["n"] - offen)}
        zuletzt = self._zuletzt(pipeline, jetzt)
        ab = zuletzt + timedelta(hours=pipeline.takt["stunden"]) if zuletzt is not None else None
        return {"art": "zeit", "ab": sicht.zeit_text(ab) if ab is not None and ab > jetzt else None}

    def pipelines(self) -> dict:
        skills = self._skills()
        alle = self.einstellungen.alle()
        jetzt = datetime.now().astimezone()
        stand = {}
        for skill in skills:
            pipeline = alle.get(skill, einstellungen_modul.Pipeline())
            laeufe = self.laeufe.abgeschlossene(skill)
            offen = len(self._offen(pipeline, self._zaehlende(skill)))
            stand[skill] = {"logs": pipeline.logs, "auswertung": pipeline.auswertung, "takt": dict(pipeline.takt),
                            "naechste": self._naechste(pipeline, offen, jetzt),
                            "laeufe_mit_protokoll": len(laeufe), "neue_laeufe": self._neue(laeufe, pipeline.ausgewertet_bis),
                            "noch_offen": offen, "auswertung_offen": self.auftraege.offene_auswertung(skill)}
        gedaechtnis, nicht_lesbar = self.werkstatt.agenten_memory()
        return {"skills": stand, "gedaechtnis": gedaechtnis, "nicht_lesbar": nicht_lesbar,
                "auto_memory_aus": einstellungen_modul.auto_memory_aus(self.umgebung, self.werkstatt.konfiguration),
                "schreibbar": self.werkstatt.schreibbar}

    def _juengster(self, skill: str) -> str | None:
        zaehlende = self._zaehlende(skill)
        return max(zaehlende, key=self._schluessel).id if zaehlende else None

    def schalte(self, anfrage: dict) -> dict:
        skills = self._skills()
        skill = anfrage.get("skill")
        if not isinstance(skill, str) or skill not in skills:
            raise WerkstattFehler(404, {"fehler": ABLAUF_FEHLT})
        werte = {k: anfrage[k] for k in ("logs", "auswertung") if k in anfrage}
        if not werte and "takt" not in anfrage or not all(isinstance(w, bool) for w in werte.values()):
            raise WerkstattFehler(400, {"fehler": WAHR_ODER_FALSCH})
        if "takt" in anfrage:
            takt = einstellungen_modul.gueltiger_takt(anfrage["takt"])
            if takt is None:
                raise WerkstattFehler(400, {"fehler": einstellungen_modul.TAKT_FEHLER})
            werte["takt"] = takt
        if ("auswertung" in werte or "takt" in werte) and not self.werkstatt.schreibbar:
            raise WerkstattFehler(403, {"fehler": texte.WERKSTATT_NUR_LESEN})
        with self._takt_sperre:
            vorher = self.einstellungen.fuer(skill)
            if werte.get("auswertung") is True and not vorher.auswertung:
                werte["ausgeloest_bis"] = self._juengster(skill)
            self._setze(skill, set(skills), **werte)
        return {"ok": True}

    def _setze(self, skill: str, skills: set[str], **werte) -> None:
        try:
            self.einstellungen.setze(skill, skills, **werte)
        except einstellungen_modul.EinstellungFehler as fehler:
            raise WerkstattFehler(fehler.status, {"fehler": fehler.text})

    def pruefe_auftrag(self, bezug: object, text: object) -> tuple[str, str]:
        skill = skill_aus_bezug(bezug)
        if skill is None:
            raise AuftragFehler(400, BEZUG_FORM)
        if text is not None and (not isinstance(text, str) or len(text) > TEXT_HOECHSTENS):
            raise AuftragFehler(400, TEXT_LANG)
        if skill not in self._skills():
            raise AuftragFehler(404, ABLAUF_FEHLT)
        if not self.werkstatt.schreibbar:
            raise AuftragFehler(403, texte.WERKSTATT_NUR_LESEN)
        if not self.einstellungen.fuer(skill).auswertung:
            raise AuftragFehler(409, auswertung_aus(skill))
        if not self.laeufe.abgeschlossene(skill):
            raise AuftragFehler(409, ohne_logs(skill))
        return skill, auftragstext(skill, text)

    def stelle(self, bezug: object, text: object) -> dict:
        skill, auftrag_text = self.pruefe_auftrag(bezug, text)
        skills = set(self._skills())
        with self._takt_sperre:
            auftrag = self.auftraege.stelle_ein(AUSWERTEN, auftrag_text, f"Ablauf {skill}")
            try:
                self.einstellungen.setze(skill, skills, zuletzt_gestellt=sicht.zeit_text(datetime.now().astimezone()))
            except einstellungen_modul.EinstellungFehler as fehler:
                _protokoll(f"Einstellungen {einstellungen_modul.DATEI}: zuletzt_gestellt nicht gespeichert "
                           f"({fehler.text}).")
        self.sende("pipelines", {})
        return auftrag

    def bei_lauf_ende(self, lauf: werkstatt_laeufe.Abgeschlossen) -> None:
        threading.Thread(target=self.pruefe_takt, args=(lauf.skill,), daemon=True).start()

    def beim_start(self) -> None:
        threading.Thread(target=self._alle_pruefen, daemon=True).start()

    def nach_zeit(self) -> None:
        if time.monotonic() - self._zuletzt_geprueft < ZEIT_PRUEFUNG_S:
            return
        self._zuletzt_geprueft = time.monotonic()
        if not self.werkstatt.schreibbar:
            return
        mit_abstand = [skill for skill, pipeline in self.einstellungen.schnappschuss().items()
                       if pipeline.auswertung and pipeline.takt["art"] == "abstand"]
        if not mit_abstand or not self._zeit_sperre.acquire(blocking=False):
            return
        threading.Thread(target=self._pruefe_nach_zeit, args=(mit_abstand,), daemon=True).start()

    def _pruefe_nach_zeit(self, mit_abstand: list[str]) -> None:
        try:
            for skill in mit_abstand:
                self.pruefe_takt(skill, nur_abstand=True)
        finally:
            self._zeit_sperre.release()

    def _alle_pruefen(self) -> None:
        try:
            skills = self._skills()
        except WerkstattFehler as fehler:
            _protokoll(f"Takt der Auswertung nicht geprüft: {fehler.daten.get('fehler')}")
            return
        alle = self.einstellungen.alle()
        for skill in skills:
            pipeline = alle.get(skill)
            if pipeline is not None and pipeline.auswertung:
                self.pruefe_takt(skill)

    def pruefe_takt(self, skill: str, nur_abstand: bool = False) -> None:
        if not self.werkstatt.schreibbar:
            return
        with self._takt_sperre:
            pipeline = self.einstellungen.fuer(skill)
            if not pipeline.auswertung or pipeline.takt["art"] == "klick" \
                    or nur_abstand and pipeline.takt["art"] != "abstand":
                return
            try:
                skills = self._skills()
            except WerkstattFehler as fehler:
                _protokoll(f"Takt der Auswertung für {skill} nicht geprüft: {fehler.daten.get('fehler')}")
                return
            if skill not in skills:
                return
            if pipeline.ausgeloest_fehlt:
                self._versuche(skill, skills, ausgeloest_bis=self._juengster(skill))
                return
            jetzt = datetime.now().astimezone()
            if pipeline.takt["art"] == "abstand" and self._in_der_zukunft(pipeline, jetzt):
                if not self._versuche(skill, skills, zuletzt_gestellt=sicht.zeit_text(jetzt)):
                    return
                _protokoll(f"Einstellungen {einstellungen_modul.DATEI}: zuletzt_gestellt {pipeline.zuletzt_gestellt} "
                           f"lag in der Zukunft und gilt als jetzt.")
                pipeline = self.einstellungen.fuer(skill)
            offen = sorted(self._offen(pipeline, self._zaehlende(skill)), key=self._schluessel)
            grund = self._grund(pipeline, offen, jetzt)
            if grund is None or self.auftraege.offene_auswertung(skill) is not None:
                return
            if self.auftraege.voll():
                if skill not in self._an_der_grenze:
                    self._an_der_grenze.add(skill)
                    _protokoll(f"Auswertung von {skill} nicht gestellt: Es warten schon zu viele Aufträge.")
                return
            self._an_der_grenze.discard(skill)
            geschrieben = {"ausgeloest_bis": offen[-1].id, "zuletzt_gestellt": sicht.zeit_text(jetzt)}
            if not self._versuche(skill, skills, **geschrieben):
                return
            try:
                auftrag = self.auftraege.stelle_ein(AUSWERTEN, auftragstext(skill, None), f"Ablauf {skill}")
            except AuftragFehler as fehler:
                _protokoll(f"Auswertung von {skill} nicht gestellt: {fehler.text}")
                self._setze_zurueck(skill, skills, geschrieben, pipeline)
                return
        self.auftraege.zeile(auftrag["id"], f"Von selbst gestellt: {grund}.")
        self.sende("pipelines", {})

    def _setze_zurueck(self, skill: str, skills: list[str], geschrieben: dict[str, str],
                       vorher: einstellungen_modul.Pipeline) -> None:
        jetzt = self.einstellungen.fuer(skill)
        zurueck = {feld: getattr(vorher, feld) for feld, wert in geschrieben.items() if getattr(jetzt, feld) == wert}
        if zurueck:
            self._versuche(skill, skills, **zurueck)

    def _versuche(self, skill: str, skills: list[str], **werte) -> bool:
        try:
            self.einstellungen.setze(skill, set(skills), **werte)
        except einstellungen_modul.EinstellungFehler as fehler:
            _protokoll(f"Einstellungen {einstellungen_modul.DATEI} für {skill} nicht gespeichert, keine Auswertung "
                       f"gestellt: {fehler.text}.")
            return False
        return True

    def _grund(self, pipeline: einstellungen_modul.Pipeline, offen: list[werkstatt_laeufe.Abgeschlossen],
               jetzt: datetime) -> str | None:
        art = pipeline.takt["art"]
        if not offen:
            return None
        if art == "jeder_lauf":
            return f"nach dem Lauf vom {offen[-1].gestartet_zeit():%d.%m.%Y, %H:%M}"
        if art == "alle_n":
            return f"{len(offen)} neue Läufe seit der letzten Auswertung" if len(offen) >= pipeline.takt["n"] else None
        neue = f"{len(offen)} {'neuer Lauf' if len(offen) == 1 else 'neue Läufe'}"
        zuletzt = self._zuletzt(pipeline, jetzt)
        if zuletzt is None:
            return f"erste Auswertung, {neue}"
        stunden = pipeline.takt["stunden"]
        if zuletzt + timedelta(hours=stunden) > jetzt:
            return None
        dauer = (f"{stunden // 24} {'Tag' if stunden == 24 else 'Tagen'}" if stunden % 24 == 0
                 else f"{stunden} {'Stunde' if stunden == 1 else 'Stunden'}")
        return f"{neue}, letzte Auswertung vor mehr als {dauer}"

    def _lies_lauf(self, lid: str) -> tuple[str | None, dict[str, bytes]]:
        ordner = self.laeufe.ordner / lid
        for pfad in (self.laeufe.ergebnis, self.laeufe.werkstatt_ordner, self.laeufe.ordner, ordner):
            if plattform.ist_verweis(pfad):
                return "symlink", {}
        dateien: dict[str, bytes] = {}
        for name in LAUF_DATEIEN:
            pfad = ordner / name
            try:
                info = os.lstat(pfad)
            except OSError:
                return "nicht_lesbar", {}
            if plattform.ist_verweis_info(info):
                return "symlink", {}
            if not stat.S_ISREG(info.st_mode):
                return "nicht_lesbar", {}
            hoechstens = {werkstatt_laeufe.LAUF_JSON: LAUF_JSON_HOECHSTENS,
                          werkstatt_laeufe.BAUPLAN_JSON: BAUPLAN_HOECHSTENS}.get(name, EREIGNISSE_DATEI_HOECHSTENS)
            try:
                dateien[name] = pfade.lies_regulaer(pfad, hoechstens)
            except pfade.LeseFehler:
                return "nicht_lesbar", {}
        return None, dateien

    def _verloren(self, lid: str) -> int:
        try:
            return sum(1 for n in os.listdir(self.laeufe.ordner / lid) if n.startswith(werkstatt_laeufe.VERLOREN))
        except OSError:
            return 0

    def _ort(self, text: object) -> Path | None:
        if not isinstance(text, str) or not text:
            return None
        if text == "~" or text.startswith(("~/", "~\\")):
            return Path(self.werkstatt.home) / text[2:] if len(text) > 1 else Path(self.werkstatt.home)
        return Path(text)

    def lege_arbeitsdaten_an(self, aid: str, ordner: Path, skill: str, ende: float, kopie: Path,
                             kopie_stand: dict[str, str]) -> dict:
        werkstatt = self.werkstatt
        bauplan = werkstatt.bauplan()
        jetzt = datetime.now().astimezone()
        kandidaten = self.laeufe.kandidaten(skill)
        kopien, roh, weggelassen, uebersprungen = self._waehle_laeufe(kandidaten, ordner, jetzt, ende)
        self._schreibe_kopien(ordner, kopien, roh, jetzt)
        kennzahlen = kennzahlen_modul.kennzahlen(kopien, bauplan, skill, werkstatt.kit_id, jetzt)
        kennzahlen_datei = ordner / "kennzahlen.json"
        kennzahlen_datei.write_text(json.dumps(kennzahlen, ensure_ascii=False, indent=1), encoding="utf-8")
        neueste = sorted(kopien, key=lambda k: (sicht.zeit_von(k.lauf.get("gestartet")) or jetzt, k.id), reverse=True)
        _pruefe_frist(ende)
        arbeitsordner = {lauf.id: lauf.arbeitsordner for lauf in kandidaten}
        orte, gedaechtnis, ueber_gedaechtnis = self._gedaechtnis(ordner, bauplan, skill, neueste, arbeitsordner)
        uebersprungen += ueber_gedaechtnis
        daten = auswertung_form.Arbeitsdaten(
            skill=skill, laeufe={k.id: k for k in kopien}, kennzahlen=kennzahlen, kopie=Path(kopie),
            kopie_stand=dict(kopie_stand), bauplan=bauplan, gedaechtnis={g["nr"]: g.pop("gid") for g in gedaechtnis},
            gedaechtnis_dateien={g["nr"]: [d["datei"] for d in g["dateien"]] for g in gedaechtnis},
            gedaechtnis_kopie={g["nr"]: Path(g["ordner"]) for g in gedaechtnis},
            gedaechtnis_stand={(g["nr"], datei): stand for g in gedaechtnis for datei, stand in g.pop("staende").items()},
            weggelassen=weggelassen, home=Path(werkstatt.home))
        with self._sperre:
            self._daten[aid] = daten
        laeufe = [{"id": k.id, "ordner": k.ordner, "gestartet": k.lauf.get("gestartet"), "ende": k.kurz.get("ende"),
                   "dauer_s": k.kurz.get("dauer_s"), "zustand": k.kurz.get("zustand"), "kit_geaendert": k.kit_geaendert,
                   "agentenlaeufe": k.kurz.get("agenten")} for k in reversed(neueste)]
        return {"laeufe": laeufe, "weggelassen": weggelassen, "kennzahlen": str(kennzahlen_datei),
                "orte": [{"nr": nr, "anzeige": gd.tilde(ort, werkstatt.home)} for nr, ort in enumerate(orte, 1)],
                "gedaechtnis": gedaechtnis, "uebersprungen": uebersprungen}

    def _waehle_laeufe(self, kandidaten: list[werkstatt_laeufe.Abgeschlossen], ordner: Path, jetzt: datetime,
                       ende: float) -> tuple[list[kennzahlen_modul.LaufKopie], dict[str, dict[str, bytes]], int,
                                             list[dict]]:
        signatur = werkstatt_laeufe.signatur_text(self.werkstatt.signatur())
        uebersprungen: list[dict] = []
        kopien: list[kennzahlen_modul.LaufKopie] = []
        roh: dict[str, dict[str, bytes]] = {}
        groesse = 0
        for i, lauf in enumerate(kandidaten):
            _pruefe_frist(ende)
            if len(kopien) >= LAEUFE_HOECHSTENS:
                return kopien, roh, len(kandidaten) - i, uebersprungen
            grund, dateien = self._lies_lauf(lauf.id)
            if grund is not None:
                uebersprungen.append({"datei": f"laeufe/{lauf.id}", "grund": grund, "typ": "ordner"})
                continue
            zeilen, gueltig, ungueltig = kennzahlen_modul.zerlege(dateien[werkstatt_laeufe.EREIGNISSE])
            if not gueltig:
                continue
            if groesse + len(dateien[werkstatt_laeufe.EREIGNISSE]) > EREIGNISSE_HOECHSTENS:
                return kopien, roh, len(kandidaten) - i, uebersprungen
            try:
                lauf_json = json.loads(dateien[werkstatt_laeufe.LAUF_JSON].decode("utf-8"))
                lauf_bauplan = json.loads(dateien[werkstatt_laeufe.BAUPLAN_JSON].decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                lauf_json = lauf_bauplan = None
            if not isinstance(lauf_json, dict) or lauf_json.get("id") != lauf.id or not isinstance(lauf_bauplan, dict):
                uebersprungen.append({"datei": f"laeufe/{lauf.id}", "grund": "nicht_lesbar", "typ": "ordner"})
                continue
            groesse += len(dateien[werkstatt_laeufe.EREIGNISSE])
            kopie_lauf = kennzahlen_modul.LaufKopie(
                lauf.id, lauf_json, lauf_bauplan, zeilen, gueltig, ungueltig, self._verloren(lauf.id), lauf.marken,
                lauf_json.get("signatur") != signatur, str(ordner / "laeufe" / lauf.id))
            kopie_lauf.kurz = sicht.kurzform(lauf_json, sicht.fasse_zusammen(kopie_lauf.ereignisse, lauf.marken), jetzt)
            kopien.append(kopie_lauf)
            roh[lauf.id] = dateien
        return kopien, roh, 0, uebersprungen

    def _schreibe_kopien(self, ordner: Path, kopien: list[kennzahlen_modul.LaufKopie],
                         roh: dict[str, dict[str, bytes]], jetzt: datetime) -> None:
        laeufe_ordner = pfade.sicherer_ordner(ordner, "laeufe")
        for kopie_lauf in kopien:
            ziel = pfade.sicherer_ordner(laeufe_ordner, kopie_lauf.id)
            for name, inhalt in roh[kopie_lauf.id].items():
                (ziel / name).write_bytes(inhalt)
            ansicht = sicht.sicht(kopie_lauf.lauf, kopie_lauf.bauplan, kopie_lauf.ereignisse, kopie_lauf.marken, jetzt,
                                  kopie_lauf.kit_id, self.laeufe.still_s, self.laeufe.ruhe_s)
            zeitleiste = {k: ansicht[k] for k in ("zeitleiste", "schritte", "kosten")}
            (ziel / "zeitleiste.json").write_text(json.dumps(zeitleiste, ensure_ascii=False, indent=1), encoding="utf-8")

    def _orte(self, neueste: list[kennzahlen_modul.LaufKopie], arbeitsordner: dict[str, str | None]) -> list[Path]:
        verboten = self.werkstatt.verboten
        if self.werkstatt.art != "plugin":
            return gd.orte([Path(self.werkstatt.kit)], verboten)
        pfade_: list[Path] = []
        for kopie_lauf in neueste:
            text = arbeitsordner.get(kopie_lauf.id)
            ort = self._ort(text)
            if ort is None or not os.path.isabs(ort) or ".." in ort.parts or not os.path.isdir(ort):
                _protokoll(f"Lauf {kopie_lauf.id}: Arbeitsordner {text!r} ist kein Ort für Memory.")
                continue
            pfade_.append(ort)
        return gd.orte(pfade_, verboten)

    def _gedaechtnis(self, ordner: Path, bauplan: dict, skill: str, neueste: list[kennzahlen_modul.LaufKopie],
                     arbeitsordner: dict[str, str | None]) -> tuple[list[Path], list[dict], list[dict]]:
        orte = self._orte(neueste, arbeitsordner)
        felder, _ = self.werkstatt.agenten_memory()
        verboten = self.werkstatt.verboten
        ziele: list[gd.Ziel] = []
        for agent in kennzahlen_modul.agenten_der_pipeline(bauplan, skill):
            bereich = felder.get(agent)
            if bereich not in gd.BEREICHE:
                continue
            if bereich == "user":
                anker = Path(os.path.realpath(self.werkstatt.konfiguration))
                kandidaten = [(anker, None)] if gd.anker_verboten(anker, verboten) is None else []
            else:
                kandidaten = [(ort, nr) for nr, ort in enumerate(orte, 1)]
            for anker, nr in kandidaten:
                ziel = self.register.ziel(agent, bereich, anker, nr)
                if ziel is not None and gd.ordner_verboten(ziel.ordner, verboten) is None:
                    ziele.append(ziel)
        nach = ordner / "gedaechtnis"
        nach.mkdir(mode=0o700, exist_ok=True)
        gedaechtnis, uebersprungen = gd.kopiere(ziele, nach, self.register)
        return orte, gedaechtnis, uebersprungen

    def arbeitsdaten(self, aid: str) -> auswertung_form.Arbeitsdaten | None:
        with self._sperre:
            return self._daten.get(aid)

    def vergiss(self, aid: str) -> None:
        with self._sperre:
            self._daten.pop(aid, None)

    def nach_auswertung(self, daten: auswertung_form.Arbeitsdaten) -> None:
        if not daten.laeufe:
            return
        juengster = max(daten.laeufe.values(),
                        key=lambda k: (sicht.zeit_von(k.lauf.get("gestartet")) or datetime.fromtimestamp(0).astimezone(),
                                       k.id))
        werte = {"ausgewertet_bis": juengster.id}
        zaehlende = [k for k in daten.laeufe.values()
                     if k.kurz.get("zustand") == sicht.BEENDET and (k.kurz.get("agenten") or 0) >= 1]
        try:
            with self._takt_sperre:
                if zaehlende:
                    kopiert = max(zaehlende, key=lambda k: (sicht.zeit_von(k.lauf.get("gestartet")) or
                                                            datetime.fromtimestamp(0).astimezone(), k.id))
                    bisher = self.einstellungen.fuer(daten.skill).ausgeloest_bis
                    laeufe = self.laeufe.abgeschlossene(daten.skill)
                    grenze_bisher = self._grenze(laeufe, bisher) if bisher is not None else None
                    grenze_kopiert = self._grenze(laeufe, kopiert.id)
                    if grenze_bisher is None or grenze_kopiert is not None and grenze_kopiert > grenze_bisher:
                        werte["ausgeloest_bis"] = kopiert.id
                self.einstellungen.setze(daten.skill, set(self._skills()), **werte)
        except (einstellungen_modul.EinstellungFehler, WerkstattFehler) as fehler:
            _protokoll(f"Einstellungen {einstellungen_modul.DATEI}: ausgewertet_bis nicht gespeichert "
                       f"({getattr(fehler, 'text', fehler)}).")
        self.sende("pipelines", {})

    def bereinige(self, aid: object, auswahl: object) -> dict:
        if not self.werkstatt.schreibbar:
            raise WerkstattFehler(403, {"fehler": texte.WERKSTATT_NUR_LESEN})
        auswertung, intern = self.auftraege.auswertung_mit_intern(aid)
        eintraege = [{**{k: e[k] for k in ("id", "aktion", "datei", "agent", "bereich", "zeilentext")},
                      **intern["gedaechtnis"][e["id"]]} for e in auswertung["gedaechtnis"]]
        ids = {e["id"] for e in eintraege}
        if (not isinstance(auswahl, list) or not 1 <= len(auswahl) <= AUSWAHL_HOECHSTENS
                or not all(isinstance(a, str) and a in ids for a in auswahl) or len(set(auswahl)) != len(auswahl)):
            raise WerkstattFehler(400, {"fehler": AUSWAHL_FORM})
        try:
            satz, meldungen = bereinigen.baue_satz(eintraege, set(auswahl), self.register, self.werkstatt.verboten)
        except gd.GedaechtnisFehler as fehler:
            raise WerkstattFehler(fehler.status, {"fehler": fehler.text})
        if len(meldungen) == len(auswahl):
            raise WerkstattFehler(409, {"fehler": bereinigen.KEINER_GEHT, "meldungen": meldungen})
        vid = self.werkstatt.lege_vor_gedaechtnis(satz)
        return {"ok": True, "vorschlag": vid, "eintraege": len(satz["eintraege"])}
