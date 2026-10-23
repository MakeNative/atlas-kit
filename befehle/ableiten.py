from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

import ergebnis
import finden

SCHEMA = "atlas-ableitung/0.1"
SKILLS_JE_PAKET = 8
ROLLEN_JE_PAKET = 20
ENDUNG = ".ableitung"
LAUF_DATEI = "lauf.json"
QUELLEN = "quellen"
STEUER = "@atlas "
ROLLEN_PAKET = "rollen"
JSON_BLOCK = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.S)
STEUERZEICHEN_KATEGORIEN = ("Cc", "Cf", "Zl", "Zp")
ZUSTAND_FELDER = {"lauf": str, "befehl": str, "kit": dict, "name": str, "namen": dict, "eigene_schritte": list,
                  "dateien": dict, "pakete": list, "nr": int, "versuch": int, "offen": dict, "skills": dict,
                  "agenten": dict, "unlesbar": list, "agenten_offen": int}


class LaufFehler(Exception):
    pass


class AbleitungKaputt(Exception):
    pass


def ist_steuerzeichen(zeichen: str) -> bool:
    return unicodedata.category(zeichen) in STEUERZEICHEN_KATEGORIEN


def maskiere(text: str) -> str:
    return "".join("?" if ist_steuerzeichen(zeichen) else zeichen for zeichen in text)


def ableitung_datei(ergebnisordner: Path, name: str) -> Path:
    return ergebnisordner / f"{name}.ableitung.json"


def arbeitsordner(ergebnisordner: Path, name: str) -> Path:
    return ergebnisordner / f".{name}{ENDUNG}"


def _entferne(pfad: Path) -> None:
    if pfad.is_symlink() or pfad.is_file():
        pfad.unlink()
    elif pfad.is_dir():
        shutil.rmtree(pfad)


def raeume_auf(ergebnisordner: Path, name: str) -> None:
    _entferne(arbeitsordner(ergebnisordner, name))


def jetzt() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def aufbau_fehler(daten: object) -> str | None:
    if not isinstance(daten, dict):
        return "oberste Ebene ist kein Objekt"
    if daten.get("schema") != SCHEMA:
        return f"schema ist nicht {SCHEMA}"
    kit = daten.get("kit")
    if not isinstance(kit, dict) or not isinstance(kit.get("quelle"), str):
        return "kit.quelle fehlt"
    for feld in ("lauf", "skills", "agenten"):
        if not isinstance(daten.get(feld), dict):
            return f"{feld} fehlt oder ist kein Objekt"
    return None


def vorhandene(pfad: Path, quelle: str) -> dict | None:
    if not pfad.exists():
        return None
    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise AbleitungKaputt()
    if aufbau_fehler(daten) is not None:
        raise AbleitungKaputt()
    return daten if daten["kit"]["quelle"] == quelle else None


def als_text(daten: dict) -> bytes:
    return (json.dumps(daten, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def ableitung(kit_id: str, quelle: str, skills: dict, agenten: dict, abgebrochen_bei: str | None) -> dict:
    return {
        "schema": SCHEMA,
        "erzeugt": jetzt(),
        "kit": {"id": kit_id, "quelle": quelle},
        "lauf": {"vollstaendig": abgebrochen_bei is None, "abgebrochen_bei": abgebrochen_bei},
        "skills": skills,
        "agenten": agenten,
    }


def pakete(skills: list[str], agenten: list[str]) -> list[dict]:
    skill_teile = [skills[i:i + SKILLS_JE_PAKET] for i in range(0, len(skills), SKILLS_JE_PAKET)]
    rollen_teile = [agenten[i:i + ROLLEN_JE_PAKET] for i in range(0, len(agenten), ROLLEN_JE_PAKET)]
    liste = [{"skills": teil, "agenten": []} for teil in skill_teile]
    if liste and rollen_teile:
        liste[-1]["agenten"] = rollen_teile.pop(0)
    liste += [{"skills": [], "agenten": teil} for teil in rollen_teile]
    return liste


def json_block(text: str) -> object | None:
    treffer = JSON_BLOCK.search(text)
    roh = treffer.group(1) if treffer else text.strip()
    try:
        return json.loads(roh)
    except (ValueError, RecursionError):
        return None


def _skill_lesbar(eintrag: object) -> bool:
    return isinstance(eintrag, dict) and isinstance(eintrag.get("schritte"), list)


class Lauf:
    def __init__(self, ordner: Path, zustand: dict):
        self.ordner = ordner
        self.zustand = zustand

    @classmethod
    def beginne(cls, befehl: str, kit: finden.Kit, ergebnisordner: Path, kit_id: str, quelle: str,
                bericht: dict) -> "Lauf":
        name = ergebnis.dateiname(kit_id)
        ordner = arbeitsordner(ergebnisordner, name)
        raeume_auf(ergebnisordner, name)
        ordner.mkdir()
        ordner = Path(os.path.realpath(ordner))
        offen = bericht["offen"]
        dateien = {"skills": {s["name"]: s["datei"] for s in offen["skills"]},
                   "agenten": {a["name"]: a["datei"] for a in offen["agenten"]}}
        liste = pakete([s["name"] for s in offen["skills"]], [a["name"] for a in offen["agenten"]])
        lauf = cls(ordner, {
            "lauf": str(ordner),
            "befehl": befehl,
            "kit": {"ordner": str(kit.ordner), "id": kit_id, "art": kit.art, "quelle": quelle},
            "name": name,
            "namen": bericht["namen"],
            "eigene_schritte": bericht["eigene_schritte"],
            "dateien": dateien,
            "pakete": liste,
            "nr": 1,
            "versuch": 1,
            "offen": liste[0],
            "skills": {},
            "agenten": {},
            "unlesbar": [],
            "agenten_offen": len(offen["agenten"]),
        })
        for nr, paket in enumerate(liste, start=1):
            lauf._kopiere_quellen(nr, paket)
        lauf.schreibe_auftrag()
        lauf.speichere()
        return lauf

    @classmethod
    def lade(cls, angabe: str, arbeitsordner_: Path) -> "Lauf":
        if not angabe:
            raise LaufFehler(angabe)
        pfad = Path(angabe)
        if not pfad.is_absolute():
            pfad = arbeitsordner_ / pfad
        if ".." in pfad.parts or pfad.is_symlink():
            raise LaufFehler(angabe)
        echt = Path(os.path.realpath(pfad))
        if not (echt.name.startswith(".") and echt.name.endswith(ENDUNG) and echt.parent.name == ergebnis.ORDNER_NAME
                and echt.is_dir()):
            raise LaufFehler(angabe)
        datei = echt / LAUF_DATEI
        if datei.is_symlink() or not datei.is_file():
            raise LaufFehler(angabe)
        try:
            zustand = json.loads(datei.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            raise LaufFehler(angabe)
        if not isinstance(zustand, dict) or zustand.get("lauf") != str(echt):
            raise LaufFehler(angabe)
        if any(not isinstance(zustand.get(feld), art) for feld, art in ZUSTAND_FELDER.items()):
            raise LaufFehler(angabe)
        return cls(echt, zustand)

    @property
    def befehl(self) -> str:
        return self.zustand["befehl"]

    @property
    def kit(self) -> finden.Kit:
        kit = self.zustand["kit"]
        return finden.Kit(Path(kit["ordner"]), kit["id"], kit["art"])

    @property
    def kit_id(self) -> str:
        return self.zustand["kit"]["id"]

    @property
    def quelle(self) -> str:
        return self.zustand["kit"]["quelle"]

    @property
    def ergebnisordner(self) -> Path:
        return self.ordner.parent

    @property
    def paket(self) -> dict:
        return self.zustand["pakete"][self.zustand["nr"] - 1]

    @property
    def eigene_schritte(self) -> list[str]:
        return self.zustand["eigene_schritte"]

    @property
    def agenten_offen(self) -> int:
        return self.zustand["agenten_offen"]

    @property
    def unlesbar(self) -> list[str]:
        return self.zustand["unlesbar"]

    def _quelle(self, nr: int, datei: str) -> Path:
        return self.ordner / QUELLEN / str(nr) / datei

    def _kopiere_quellen(self, nr: int, paket: dict) -> None:
        kit = Path(self.zustand["kit"]["ordner"])
        for art in ("skills", "agenten"):
            for name in paket[art]:
                datei = self.zustand["dateien"][art][name]
                ziel = self._quelle(nr, datei)
                ziel.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(kit / datei, ziel)

    def _auftrag_datei(self) -> Path:
        return self.ordner / f"auftrag-{self.zustand['nr']}.json"

    def _antwort_datei(self) -> Path:
        return self.ordner / f"antwort-{self.zustand['nr']}-{self.zustand['versuch']}.txt"

    def schreibe_auftrag(self) -> None:
        nr = self.zustand["nr"]
        offen = self.zustand["offen"]
        dateien = self.zustand["dateien"]
        auftrag = {
            "art": self.zustand["kit"]["art"],
            "id": self.kit_id,
            "namen": self.zustand["namen"],
            "skills": {n: str(self._quelle(nr, dateien["skills"][n])) for n in offen["skills"]},
            "agenten": {n: str(self._quelle(nr, dateien["agenten"][n])) for n in offen["agenten"]},
        }
        ergebnis.schreibe_atomar(self._auftrag_datei(), als_text(auftrag))

    def speichere(self) -> None:
        ergebnis.schreibe_atomar(self.ordner / LAUF_DATEI, als_text(self.zustand))

    def _eigener_pfad(self, pfad: Path) -> bool:
        name = self.zustand["name"]
        return (name == ergebnis.dateiname(name) and self.ordner.name == f".{name}{ENDUNG}"
                and self.ordner.parent.name == ergebnis.ORDNER_NAME and self.ordner.is_absolute()
                and (pfad == self.ordner or pfad.parent == self.ordner)
                and STEUER.strip() not in str(pfad)
                and not any(ist_steuerzeichen(zeichen) for zeichen in str(pfad)))

    def block(self) -> list[str]:
        werte = [("auftrag", self._auftrag_datei()), ("antwort", self._antwort_datei()), ("lauf", self.ordner)]
        if not all(self._eigener_pfad(pfad) for _, pfad in werte):
            raise LaufFehler(str(self.ordner))
        return [f"{STEUER}{schluessel}: {pfad}" for schluessel, pfad in werte]

    def _antwort(self) -> object | None:
        try:
            text = self._antwort_datei().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return json_block(text)

    def _nachfassen(self, skills: list[str], agenten: list[str]) -> bool:
        self.zustand["versuch"] = 2
        self.zustand["offen"] = {"skills": skills, "agenten": agenten}
        self.schreibe_auftrag()
        self.speichere()
        return True

    def nimm_antwort(self) -> bool:
        offen = self.zustand["offen"]
        erster_versuch = self.zustand["versuch"] == 1
        daten = self._antwort()
        if not isinstance(daten, dict):
            if erster_versuch:
                return self._nachfassen(offen["skills"], offen["agenten"])
            self.zustand["unlesbar"] += offen["skills"]
            return False
        skills = daten.get("skills") if isinstance(daten.get("skills"), dict) else {}
        agenten = daten.get("agenten") if isinstance(daten.get("agenten"), dict) else {}
        fehlende_skills = []
        for name in offen["skills"]:
            if _skill_lesbar(skills.get(name)):
                self.zustand["skills"][name] = skills[name]
            else:
                fehlende_skills.append(name)
        fehlende_agenten = []
        for name in offen["agenten"]:
            if isinstance(agenten.get(name), dict):
                self.zustand["agenten"][name] = agenten[name]
            else:
                fehlende_agenten.append(name)
        if (fehlende_skills or fehlende_agenten) and erster_versuch:
            return self._nachfassen(fehlende_skills, fehlende_agenten)
        self.zustand["unlesbar"] += fehlende_skills
        return False

    def ist_letztes_rollen_paket(self) -> bool:
        nr = self.zustand["nr"]
        mit_rollen = [i for i, paket in enumerate(self.zustand["pakete"], start=1) if paket["agenten"]]
        return bool(mit_rollen) and mit_rollen[-1] == nr

    def naechstes_paket(self) -> bool:
        nr = self.zustand["nr"] + 1
        if nr > len(self.zustand["pakete"]):
            return False
        self.zustand["nr"] = nr
        self.zustand["versuch"] = 1
        self.zustand["offen"] = self.paket
        self.schreibe_auftrag()
        self.speichere()
        return True

    def verwirf_aktuelles_paket(self) -> str:
        paket = self.paket
        for name in paket["skills"]:
            self.zustand["skills"].pop(name, None)
            if name in self.zustand["unlesbar"]:
                self.zustand["unlesbar"].remove(name)
        for name in paket["agenten"]:
            self.zustand["agenten"].pop(name, None)
        return paket["skills"][0] if paket["skills"] else ROLLEN_PAKET

    def teil_skills(self) -> dict[str, dict]:
        dateien = self.zustand["dateien"]["skills"]
        skills = {}
        for name in sorted(set(self.zustand["skills"]) | set(self.zustand["unlesbar"])):
            if name in self.zustand["unlesbar"]:
                skills[name] = {"datei": dateien[name], "lesbar": False, "entfallen": 0, "schritte": []}
                continue
            eintrag = {"datei": dateien[name], "lesbar": True, "entfallen": 0}
            for schluessel, wert in self.zustand["skills"][name].items():
                eintrag.setdefault(schluessel, wert)
            skills[name] = eintrag
        return skills

    def teil_agenten(self) -> dict[str, dict]:
        dateien = self.zustand["dateien"]["agenten"]
        agenten = {}
        for name in sorted(self.zustand["agenten"]):
            eintrag = {"datei": dateien[name]}
            for schluessel, wert in self.zustand["agenten"][name].items():
                eintrag.setdefault(schluessel, wert)
            agenten[name] = eintrag
        return agenten

    def teil_ableitung(self, abgebrochen_bei: str | None = None) -> dict:
        return ableitung(self.kit_id, self.quelle, self.teil_skills(), self.teil_agenten(), abgebrochen_bei)

    def entferne(self) -> None:
        shutil.rmtree(self.ordner, ignore_errors=True)
