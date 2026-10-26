from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402

import ableiten  # noqa: E402
import bauplan  # noqa: E402
import browser  # noqa: E402
import einrichtung  # noqa: E402
import ergebnis  # noqa: E402
import finden  # noqa: E402
import neues_kit  # noqa: E402
import plattform  # noqa: E402
import scanner_lauf  # noqa: E402
import seite  # noqa: E402
import start  # noqa: E402
import texte  # noqa: E402
import werkstatt  # noqa: E402
import zuletzt  # noqa: E402

LESEN = ("scan", "render")
ABLEITEN = ("analyse", "init")
WEITERFUEHREN = ("antwort", "abbruch")
WERKSTATT = "werkstatt"
NEU = "neu"
START_APP = ("app", "start")
BEFEHLE = LESEN + ABLEITEN + WEITERFUEHREN + (WERKSTATT, NEU) + START_APP
DATEN_SCHALTER = "CLAUDE_PLUGIN_DATA"
VORLAGE = Path("seite") / "vorlage.html"
SCANNER = Path("atlas") / "scan.py"
LEER_SAETZE = (texte.LEER_BEIDES, texte.LEER_SKILLS, texte.LEER_AGENTEN)
AUFRUF = ("Aufruf: atlas-kit scan|render|analyse|init|werkstatt [ordner | kit-name], atlas-kit werkstatt stopp|oeffnen|knopf, "
          "atlas-kit neu <name> [ordner], "
          "atlas-kit werkstatt warte|fortschritt|frage|ergebnis|abbruch … oder atlas-kit antwort|abbruch <lauf>, "
          "atlas-kit app [desktop], atlas-kit start")


class Abbruch(Exception):
    def __init__(self, zeilen: list[str]):
        super().__init__("\n".join(zeilen))
        self.zeilen = zeilen


def sage(zeile: str) -> None:
    text = ableiten.maskiere(zeile)
    if ableiten.STEUER.strip() in text:
        text = f" {text}"
    print(text, flush=True)


def steuere(zeilen: list[str]) -> None:
    for zeile in zeilen:
        print(zeile, flush=True)


class Eingelesen:
    def __init__(self, inhalt: bytes, bericht: dict):
        self.inhalt = inhalt
        self.atlas = json.loads(inhalt.decode("utf-8"))
        self.bericht = bericht


class Aufruf:
    def __init__(self, befehl: str, argument: str, weitere: list[str] | None = None):
        self.befehl = befehl
        self.argument = argument
        # weitere Argumente nur für die Befehle der Werkstatt-Sitzung (WS-K4 bis WS-K8)
        self.weitere = list(weitere or [])
        self.paket = Path(__file__).resolve().parent.parent
        self.home = Path(os.path.expanduser("~"))
        self.arbeitsordner = Path(os.getcwd())
        daten = os.environ.get(DATEN_SCHALTER, "").strip()
        self.daten = Path(daten) if daten else None
        self.neu_angelegt = False
        self.unterbefehl = ""  # z. B. stopp oder knopf: steht dann in den Zeilen einer Auswahl (r21-6)

    @property
    def befehl_im_chat(self) -> str:
        return f"/atlas:{self.befehl}"

    def tilde(self, pfad: Path) -> str:
        return finden.tilde(pfad, self.home)

    def kit_im_arbeitsordner(self) -> finden.Kit:
        return finden.pruefe_ordner(self.arbeitsordner, self.home, self.befehl_im_chat)

    def datenordner(self) -> Path:
        return einrichtung.datenordner(self.paket, self.daten)

    def zuletzt_geoeffnet(self) -> list[finden.Kit]:
        """WS-NK8: die zuletzt in der Werkstatt geöffneten Kits, die es noch gibt."""
        return zuletzt.kits(self.datenordner(), self.home, self.befehl_im_chat)

    def kit_aus_argument(self) -> finden.Kit:
        eigene = self.zuletzt_geoeffnet() if self.befehl == WERKSTATT else None
        auswahl = f"{self.befehl_im_chat} {self.unterbefehl}" if self.unterbefehl else None
        return finden.finde_kit(self.argument, self.arbeitsordner, self.home, self.befehl_im_chat, eigene, auswahl)

    def gewaehltes_kit(self) -> finden.Kit:
        return self.kit_aus_argument() if self.argument else self.kit_im_arbeitsordner()

    def ergebnisordner_fuer(self, kit: finden.Kit) -> Path:
        ordner = ergebnis.ergebnisordner(self.arbeitsordner, kit.ordner)
        if ordner.parent == kit.ordner and finden.ist_installationsordner(kit.ordner, self.home):
            raise ergebnis.NichtSchreibbar(self.arbeitsordner)
        return ordner

    def lies_ein(self, kit: finden.Kit, ableitung: Path | None = None) -> Eingelesen:
        python = einrichtung.python_der_umgebung(self.paket, self.daten, self.home, sage)
        try:
            inhalt, bericht = scanner_lauf.scanne(python, self.paket / SCANNER, kit.ordner, ableitung)
        except scanner_lauf.ScanFehler as fehler:
            raise Abbruch([texte.einlesen_abgebrochen(fehler.zeile), texte.FEHLER_MELDEN])
        return Eingelesen(inhalt, bericht)

    def lies_mit_ableitung(self, kit: finden.Kit, ableitung: dict) -> Eingelesen:
        with tempfile.TemporaryDirectory(prefix="atlas-kit-ableitung-") as temp:
            datei = Path(temp) / "ableitung.json"
            datei.write_bytes(ableiten.als_text(ableitung))
            return self.lies_ein(kit, datei)

    def lege_ergebnisordner_an(self, ordner: Path) -> None:
        self.neu_angelegt = ergebnis.lege_an(ordner) or self.neu_angelegt

    def schreibe_plan(self, ordner: Path, eingelesen: Eingelesen) -> Path:
        plan = ordner / f"{ergebnis.dateiname(eingelesen.atlas['kit']['id'])}.atlas.json"
        ergebnis.schreibe_atomar(plan, eingelesen.inhalt)
        return plan

    def einlesen(self, kit: finden.Kit) -> tuple[dict, Path, list[str]]:
        ordner = self.ergebnisordner_fuer(kit)
        name = ergebnis.dateiname(kit.kit_id)
        datei = ableiten.ableitung_datei(ordner, name)
        try:
            vorhanden = ableiten.vorhandene(datei, finden.quelle(kit.ordner, self.home))
        except ableiten.AbleitungKaputt:
            vorhanden = None
            zusatz = [texte.ableitung_kaputt(name)]
        else:
            zusatz = []
        eingelesen = self.lies_ein(kit, datei if vorhanden is not None else None)
        self.lege_ergebnisordner_an(ordner)
        plan = self.schreibe_plan(ordner, eingelesen)
        if vorhanden is not None:
            zusatz = zeilen_zur_ableitung(eingelesen, vorhanden)
        return eingelesen.atlas, plan, zusatz

    def scan(self) -> int:
        atlas, _, zusatz = self.einlesen(self.gewaehltes_kit())
        for zeile in zeilen_nach_einlesen(atlas, zusatz):
            sage(zeile)
        sage(texte.WEITER)
        return 0

    def vorhandener_plan(self, kit: finden.Kit) -> Path | None:
        plan = self.ergebnisordner_fuer(kit) / f"{ergebnis.dateiname(kit.kit_id)}.atlas.json"
        try:
            atlas = json.loads(plan.read_text(encoding="utf-8"))
            quelle = atlas["kit"]["quelle"]
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
            return None
        return plan if quelle == finden.quelle(kit.ordner, self.home) else None

    def plan_ohne_kit(self) -> Path:
        ordner = ergebnis.ergebnisordner(self.arbeitsordner, None)
        plaene = sorted(ordner.glob("*.atlas.json")) if ordner.is_dir() else []
        if len(plaene) > 1:
            namen = [p.name[:-len(".atlas.json")] for p in plaene]
            raise Abbruch([texte.MEHRERE_BAUPLAENE, *(texte.bauplan_render(n) for n in namen)])
        if plaene:
            return plaene[0]
        raise finden.KeinKit(finden.kein_kit_zeilen(self.arbeitsordner, self.home, self.befehl_im_chat))

    def plan_namens_argument(self) -> Path | None:
        if finden.ist_ordnerangabe(self.argument, self.arbeitsordner):
            return None
        if finden.installiertes_kit(self.argument, self.arbeitsordner, self.home) is not None:
            return None
        plan = ergebnis.ergebnisordner(self.arbeitsordner, None) / f"{self.argument}.atlas.json"
        return plan if plan.is_file() else None

    def waehle_plan(self) -> tuple[Path | None, finden.Kit | None]:
        if not self.argument:
            if finden.ist_kit_oder_projekt(self.arbeitsordner, self.home, self.befehl_im_chat):
                kit = self.kit_im_arbeitsordner()
                return self.vorhandener_plan(kit), kit
            return self.plan_ohne_kit(), None
        plan = self.plan_namens_argument()
        if plan is not None:
            return plan, None
        kit = self.kit_aus_argument()
        return self.vorhandener_plan(kit), kit

    def einlesen_vor_der_seite(self, kit: finden.Kit) -> Path:
        atlas, plan, zusatz = self.einlesen(kit)
        sage(zeile_eingelesen(atlas))
        for zeile in zusatz:
            sage(zeile)
        sage(zeile_befunde(atlas))
        if zeile_zum_inhalt(atlas) == ohne_atlas(atlas):
            sage(ohne_atlas(atlas))
        return plan

    def baue_seite(self, plan: Path, eingelesen: bool, mit_leer_zeile: bool = True) -> int:
        try:
            atlas = bauplan.pruefe(plan, self.tilde(plan))
        except bauplan.BauplanFehler as fehler:
            raise Abbruch([str(fehler), texte.NEU_EINLESEN])
        if not eingelesen:
            sage(verwendet(atlas.get("erzeugt")))
        try:
            html = seite.fuelle((self.paket / VORLAGE).read_text(encoding="utf-8"), atlas)
        except (OSError, seite.VorlageFehler) as fehler:
            raise Abbruch([str(fehler), texte.FEHLER_MELDEN])
        datei = plan.parent / f"{ergebnis.dateiname(atlas['kit']['id'])}.html"
        ergebnis.schreibe_atomar(datei, html.encode("utf-8"))
        absolut = Path(os.path.realpath(datei))
        sage(texte.seite_gebaut(str(absolut)))
        wie = browser.oeffne(absolut)
        sage(texte.BROWSER_OFFEN if wie == browser.SICHER else texte.BROWSER_UNSICHER if wie == browser.UNSICHER
             else texte.BROWSER_NICHT)
        befunde = atlas["befunde"]
        if befunde:
            sage(texte.befunde_auf_der_seite(len(befunde), ort_und_text(befunde[0])))
        leer = leer_zeile(atlas)
        if leer and mit_leer_zeile:
            sage(leer)
        return 0

    def render(self) -> int:
        plan, kit = self.waehle_plan()
        if plan is not None:
            return self.baue_seite(plan, eingelesen=False)
        return self.baue_seite(self.einlesen_vor_der_seite(kit), eingelesen=True)

    def ableiten(self) -> int:
        kit = self.gewaehltes_kit()
        ordner = self.ergebnisordner_fuer(kit)
        eingelesen = self.lies_ein(kit)
        self.lege_ergebnisordner_an(ordner)
        if self.neu_angelegt:
            sage(texte.NEU_ANGELEGT)
            self.neu_angelegt = False
        atlas = eingelesen.atlas
        kit_id = atlas["kit"]["id"]
        ableiten.raeume_auf(ordner, ergebnis.dateiname(kit_id))
        offen = eingelesen.bericht["offen"]
        if not _anzahl(atlas, "skill") and not _anzahl(atlas, "agent"):
            plan = self.schreibe_plan(ordner, eingelesen)
            sage(leer_zeile(atlas))
            if self.befehl == "init":
                return self.baue_seite(plan, eingelesen=True, mit_leer_zeile=False)
            return 0
        quelle = finden.quelle(kit.ordner, self.home)
        if not offen["skills"] and not offen["agenten"]:
            sage(texte.nichts_abzuleiten(kit_id))
            daten = ableiten.ableitung(kit_id, quelle, {}, {}, None)
            return self.schliesse_ab(kit, ordner, daten, eingelesen.bericht["eigene_schritte"], None, mit_zahlen=False)
        sage(texte.ankuendigung(kit_id, len(offen["skills"]), len(offen["agenten"])))
        lauf = ableiten.Lauf.beginne(self.befehl, kit, ordner, kit_id, quelle, eingelesen.bericht)
        steuere(lauf.block())
        return 0

    def lauf(self) -> ableiten.Lauf:
        lauf = ableiten.Lauf.lade(self.argument, self.arbeitsordner)
        self.befehl = lauf.befehl
        return lauf

    def antwort(self) -> int:
        lauf = self.lauf()
        if lauf.nimm_antwort():
            steuere(lauf.block())
            return 0
        eingelesen = self.lies_mit_ableitung(lauf.kit, lauf.teil_ableitung())
        for zeile in fortschritt(lauf, eingelesen.bericht["ableitung"]):
            sage(zeile)
        if lauf.naechstes_paket():
            steuere(lauf.block())
            return 0
        lauf.speichere()
        return self.schliesse_lauf_ab(lauf, None)

    def abbruch(self) -> int:
        lauf = self.lauf()
        bei = lauf.verwirf_aktuelles_paket()
        return self.schliesse_lauf_ab(lauf, bei)

    def schliesse_lauf_ab(self, lauf: ableiten.Lauf, abgebrochen_bei: str | None) -> int:
        daten = lauf.teil_ableitung(abgebrochen_bei)
        kit = lauf.kit
        ordner = lauf.ergebnisordner
        return self.schliesse_ab(kit, ordner, daten, lauf.eigene_schritte, abgebrochen_bei, lauf=lauf)

    def schliesse_ab(self, kit: finden.Kit, ordner: Path, daten: dict, eigene: list[str],
                     abgebrochen_bei: str | None, lauf: ableiten.Lauf | None = None, mit_zahlen: bool = True) -> int:
        eingelesen = self.lies_mit_ableitung(kit, daten)
        zahlen = (eingelesen.bericht["ableitung"] or {}).get("skills", {})
        for name, eintrag in daten["skills"].items():
            eintrag["entfallen"] = zahlen.get(name, {}).get("entfallen", 0)
        name = ergebnis.dateiname(daten["kit"]["id"])
        ergebnis.schreibe_atomar(ableiten.ableitung_datei(ordner, name), ableiten.als_text(daten))
        plan = self.schreibe_plan(ordner, eingelesen)
        if lauf is not None:
            lauf.entferne()
        atlas = eingelesen.atlas
        mit_ablauf = abgeleitete_ablaeufe(atlas)
        if mit_zahlen:
            weggelassen = sum(e["entfallen"] for e in daten["skills"].values())
            sage(texte.abgeleitet(len(mit_ablauf), abgeleitete_rollen(atlas), weggelassen))
        if eigene:
            sage(texte.eigene_bleiben(sorted(eigene)))
        ohne = sorted(n for n in daten["skills"] if f"skill:{n}" not in mit_ablauf)
        if ohne:
            sage(texte.ohne_ablauf(ohne))
        if abgebrochen_bei is not None:
            sage(texte.abgebrochen(abgebrochen_bei, self.befehl_im_chat))
        if self.befehl == "init":
            return self.baue_seite(plan, eingelesen=True)
        befunde = atlas["befunde"]
        if befunde:
            sage(texte.befunde_beim_einlesen(len(befunde), ort_und_text(befunde[0])))
        sage(texte.WEITER)
        return 0

    def fuehre_aus(self) -> int:
        if self.befehl == WERKSTATT:
            return werkstatt.fuehre_aus(self, sage, steuere)
        if self.befehl == NEU:
            return neues_kit.fuehre_aus(self, sage, steuere)
        if self.befehl == "app":
            return start.app(self, sage)
        if self.befehl == "start":
            return start.starte(self, sage)
        if self.befehl in ABLEITEN:
            return self.ableiten()
        if self.befehl == "antwort":
            return self.antwort()
        if self.befehl == "abbruch":
            return self.abbruch()
        return self.scan() if self.befehl == "scan" else self.render()


erste_zeile = scanner_lauf.erste_zeile


def _anzahl(atlas: dict, typ: str) -> int:
    return sum(1 for k in atlas["knoten"] if isinstance(k, dict) and k.get("typ") == typ)


def ablaeufe(atlas: dict) -> int:
    return len({k.get("skill") for k in atlas["knoten"] if isinstance(k, dict) and k.get("typ") == "schritt"})


def abgeleitete_ablaeufe(atlas: dict) -> set[str]:
    return {k.get("skill") for k in atlas["knoten"]
            if isinstance(k, dict) and k.get("typ") == "schritt" and k.get("herkunft") == "abgeleitet"}


def abgeleitete_rollen(atlas: dict) -> int:
    return sum(1 for k in atlas["knoten"] if isinstance(k, dict) and k.get("rolle_herkunft") == "abgeleitet")


def ist_projekt(atlas: dict) -> bool:
    return atlas["kit"].get("art") == finden.PROJEKT


def ort_und_text(befund: dict) -> str:
    datei = befund.get("datei")
    zeile = befund.get("zeile")
    text = befund.get("text") or ""
    if not datei:
        return text
    ort = datei if zeile is None else f"{datei}:{zeile}"
    return f"{ort}: {text}"


def fuer_den_ordner(atlas: dict, satz: str) -> str:
    return texte.im_projekt(satz) if ist_projekt(atlas) else satz


def ohne_atlas(atlas: dict) -> str:
    return fuer_den_ordner(atlas, texte.OHNE_ATLAS)


def leer_zeile(atlas: dict) -> str | None:
    skills = _anzahl(atlas, "skill")
    agenten = _anzahl(atlas, "agent")
    if not skills and not agenten:
        return fuer_den_ordner(atlas, texte.LEER_BEIDES)
    if not skills:
        return fuer_den_ordner(atlas, texte.LEER_SKILLS)
    if not agenten:
        return fuer_den_ordner(atlas, texte.LEER_AGENTEN)
    return None


def zeile_zum_inhalt(atlas: dict) -> str | None:
    leer = leer_zeile(atlas)
    if leer:
        return leer
    if atlas["kit"].get("konvention") is False and ablaeufe(atlas) == 0 and "ableitung" not in atlas["kit"]:
        return ohne_atlas(atlas)
    return None


def zeile_eingelesen(atlas: dict) -> str:
    kit = atlas["kit"]
    return texte.eingelesen(kit["id"], kit.get("version"), _anzahl(atlas, "skill"), _anzahl(atlas, "agent"),
                            ablaeufe(atlas))


def zeile_befunde(atlas: dict) -> str:
    befunde = atlas["befunde"]
    if befunde:
        return texte.befunde_beim_einlesen(len(befunde), ort_und_text(befunde[0]))
    return texte.KEINE_AUFFAELLIGKEITEN


def zeilen_nach_einlesen(atlas: dict, zusatz: list[str]) -> list[str]:
    zeilen = [zeile_eingelesen(atlas), *zusatz, zeile_befunde(atlas)]
    dritte = zeile_zum_inhalt(atlas)
    if dritte:
        zeilen.append(dritte)
    return zeilen


def datum(erzeugt: object) -> str | None:
    if not isinstance(erzeugt, str):
        return None
    try:
        zeitpunkt = datetime.fromisoformat(erzeugt.replace("Z", "+00:00"))
    except ValueError:
        return None
    return zeitpunkt.strftime("%d.%m.%Y, %H:%M")


def verwendet(erzeugt: object) -> str:
    angabe = datum(erzeugt)
    return texte.verwendet(angabe) if angabe else texte.VERWENDET_OHNE_DATUM


def _vermerkt(eintrag: object) -> int:
    entfallen = eintrag.get("entfallen") if isinstance(eintrag, dict) else None
    return entfallen if isinstance(entfallen, int) and not isinstance(entfallen, bool) else 0


def zeilen_zur_ableitung(eingelesen: Eingelesen, ableitung: dict) -> list[str]:
    atlas = eingelesen.atlas
    zeilen = [texte.mit_abgeleiteten(datum(ableitung.get("erzeugt")), len(abgeleitete_ablaeufe(atlas)),
                                     abgeleitete_rollen(atlas))]
    zahlen = (eingelesen.bericht.get("ableitung") or {}).get("skills", {})
    nicht_mehr = sum(max(0, z["entfallen"] - _vermerkt(ableitung["skills"].get(name))) for name, z in zahlen.items())
    if nicht_mehr:
        zeilen.append(texte.nicht_mehr_belegt(nicht_mehr))
    lauf = ableitung["lauf"]
    if lauf.get("vollstaendig") is False:
        bei = lauf.get("abgebrochen_bei")
        zeilen.append(texte.unvollstaendig(bei if isinstance(bei, str) else "?"))
    return zeilen


def fortschritt(lauf: ableiten.Lauf, zahlen: dict | None) -> list[str]:
    zahlen = zahlen or {"skills": {}, "rollen": {"abgeleitet": 0, "entfallen": 0}}
    zeilen = []
    for name in sorted(lauf.paket["skills"]):
        if name in lauf.unlesbar:
            zeilen.append(texte.nicht_lesbar(name))
            continue
        skill = zahlen["skills"].get(name, {"schritte": 0, "entfallen": 0})
        zeilen.append(texte.fortschritt(name, skill["schritte"], skill["entfallen"]))
    if lauf.agenten_offen and lauf.ist_letztes_rollen_paket():
        rollen = zahlen["rollen"]
        zeilen.append(texte.rollen(rollen["abgeleitet"], lauf.agenten_offen, rollen["entfallen"]))
    return zeilen


def main(argv: list[str]) -> int:
    argv = plattform.msys_zurueck(argv)  # Windows/Git Bash: '~/…' kommt umgeschrieben an (docs/windows.md Fund 31)
    for strom in (sys.stdout, sys.stderr):
        strom.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] not in BEFEHLE:
        sage(AUFRUF)
        return 1
    # neu trennt Name und Ordner selbst und nimmt erst danach die Anführungszeichen je Teil weg (r21-10).
    roh = argv[1] if len(argv) > 1 else ""
    argument = roh if argv[0] == NEU else finden.ohne_anfuehrungszeichen(roh)
    aufruf = Aufruf(argv[0], argument, argv[2:] if argv[0] in (WERKSTATT, NEU) else None)
    try:
        exit_code = aufruf.fuehre_aus()
    except (finden.KeinKit, einrichtung.EinrichtungFehler, Abbruch) as fehler:
        exit_code = melde(fehler.zeilen)
    except ergebnis.NichtSchreibbar as fehler:
        exit_code = melde([texte.nicht_schreibbar(aufruf.tilde(fehler.ordner))])
    except ableiten.LaufFehler as fehler:
        exit_code = melde([texte.kein_lauf(str(fehler))])
    if aufruf.neu_angelegt:
        sage(texte.NEU_ANGELEGT)
    return exit_code


def melde(zeilen: list[str]) -> int:
    for zeile in zeilen:
        sage(zeile)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
