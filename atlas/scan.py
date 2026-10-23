"""Scanner: liest ein Claude-Code-Kit oder ein Projekt und schreibt seinen Bauplan als atlas.json.

Aufruf: python atlas/scan.py <ordner> <ausgabe> [--ableitung <datei>] [--bericht <datei>]

Der Ordner wird nur gelesen. Was der Scanner nicht belegen kann, wird ein Befund
(specs/scanner.yaml). Exit 0 nach erfolgreichem Scan, auch mit Befunden;
Exit 2 bei falschem Aufruf, unbrauchbarem Kit oder fehlendem PyYAML.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HILFE = """\
Aufruf: python atlas/scan.py <ordner> <ausgabe> [--ableitung <datei>] [--bericht <datei>]

  ordner       Pfad zum Kit-Ordner (Claude-Code-Plugin mit .claude-plugin/plugin.json)
               oder zu einem Projekt mit .claude/skills bzw. .claude/agents
  ausgabe      Pfad der zu schreibenden atlas.json (nicht innerhalb des Ordners, außer direkt in <ordner>/atlas-kit/)
  --ableitung  Ableitung (atlas-ableitung/0.1), die geprüft und übernommen wird
  --bericht    Pfad einer JSON-Datei mit offenen Skills und Agenten und den Zahlen der Prüfung (für atlas-kit)
"""

PYYAML_HINWEIS = """\
Fehler: PyYAML ist nicht installiert. Der Scanner liest Frontmatter mit PyYAML.
Installation im Projekt-venv:
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
oder direkt: pip install "PyYAML>=6.0,<7"
"""


def _abbruch(text: str) -> int:
    sys.stderr.write(text if text.endswith("\n") else text + "\n")
    return 2


def _liegt_in(pfad: Path, ordner: Path) -> bool:
    for p, o in ((Path(os.path.abspath(pfad)), Path(os.path.abspath(ordner))),
                 (pfad.resolve(), ordner.resolve())):
        if p == o or o in p.parents:
            return True
    return False


ERGEBNISORDNER_DES_PAKETS = "atlas-kit"


def _im_ergebnisordner_des_pakets(pfad: Path, kit: Path) -> bool:
    absolut = Path(os.path.abspath(pfad))
    ordner = absolut.parent
    return ordner.name == ERGEBNISORDNER_DES_PAKETS and ordner.parent == kit


SCHALTER = ("--ableitung", "--bericht")


def _argumente(argv: list[str]) -> tuple[str, str, dict[str, str]] | None:
    if len(argv) < 2:
        return None
    schalter: dict[str, str] = {}
    rest = argv[2:]
    if len(rest) % 2:
        return None
    for name, wert in zip(rest[::2], rest[1::2]):
        if name not in SCHALTER or name in schalter:
            return None
        schalter[name] = wert
    return argv[0], argv[1], schalter


def main(argv: list[str]) -> int:
    argumente = _argumente(argv)
    if argumente is None:
        return _abbruch(HILFE)
    ordner_arg, ausgabe_arg, schalter = argumente
    kit = Path(ordner_arg).expanduser()
    ausgabe = Path(ausgabe_arg).expanduser()
    if not kit.is_dir():
        return _abbruch(f"Fehler: Ordner {ordner_arg} gibt es nicht oder er ist kein Ordner.")
    kit = Path(os.path.abspath(kit))
    if _liegt_in(ausgabe, kit) and not _im_ergebnisordner_des_pakets(ausgabe, kit):
        return _abbruch(f"Fehler: Die Ausgabe {ausgabe_arg} liegt im gelesenen Ordner. "
                        f"Er wird nur gelesen; bitte einen Pfad außerhalb wählen oder direkt in <ordner>/atlas-kit/.")
    if "--bericht" in schalter:
        bericht_pfad = Path(schalter["--bericht"]).expanduser()
        if _liegt_in(bericht_pfad, kit) and not _im_ergebnisordner_des_pakets(bericht_pfad, kit):
            return _abbruch(f"Fehler: Der Bericht {schalter['--bericht']} liegt im gelesenen Ordner. "
                            f"Er wird nur gelesen; bitte einen Pfad außerhalb wählen oder direkt in <ordner>/atlas-kit/.")

    try:
        import yaml  # noqa: F401  (SC-Y2: erst nach der Argumentprüfung)
    except ImportError:
        return _abbruch(PYYAML_HINWEIS)

    import ablauf
    import ableitung as ableitung_modul
    import agenten
    import ausgabe as ausgabe_modul
    import belege
    import hooks
    import kit as kit_modul
    import ordner as ordner_modul
    import skills
    import werkzeuge
    from befunde import Befunde

    try:
        quelle = ordner_modul.erkenne(kit)
    except ordner_modul.OrdnerFehler as e:
        return _abbruch(str(e))
    ableitung = None
    if "--ableitung" in schalter:
        try:
            ableitung = ableitung_modul.lies(Path(schalter["--ableitung"]).expanduser())
        except ableitung_modul.AbleitungFehler as e:
            return _abbruch(f"Fehler: {e}")

    befunde = Befunde()
    try:
        kit_angaben = kit_modul.lies_kit(quelle, befunde)
    except kit_modul.KitFehler as e:
        return _abbruch(f"Fehler: {e}")

    agent_knoten = agenten.lies_agenten(quelle, befunde)
    werkzeug_knoten = werkzeuge.lies_werkzeuge(quelle, befunde)
    skill_paare = skills.lies_skills(quelle, kit_angaben["id"],
                                     {a["name"] for a in agent_knoten}, befunde)
    hook_knoten, bewacht_kanten = hooks.lies_hooks(quelle, befunde)
    bericht = ableitung_modul.offene(skill_paare, agent_knoten)

    knoten_ids = ({a["id"] for a in agent_knoten} | {w["id"] for w in werkzeug_knoten}
                  | {s["id"] for s, _ in skill_paare})
    eigene = {skill["name"]: ablauf.schritte_und_kanten(skill, fm, knoten_ids, befunde)
              for skill, fm in skill_paare}
    abgeleitet = None
    if ableitung is not None:
        abgeleitet = ableitung_modul.uebernimm(ableitung, skill_paare, agent_knoten, knoten_ids, kit, befunde)

    knoten: list[dict] = []
    kanten: list[dict] = []
    for skill, _ in skill_paare:
        knoten.append(skill)
        schritt_knoten, schritt_kanten = eigene[skill["name"]]
        knoten.extend(schritt_knoten)
        kanten.extend(schritt_kanten)
        if abgeleitet is not None:
            knoten.extend(abgeleitet.knoten.get(skill["name"], []))
            kanten.extend(abgeleitet.kanten.get(skill["name"], []))
    knoten.extend(agent_knoten)
    knoten.extend(hook_knoten)
    knoten.extend(werkzeug_knoten)
    kanten.extend(bewacht_kanten)

    kit_angaben["konvention"] = any(k.get("_hat_atlas") for k in knoten)
    if ableitung is not None:
        kit_angaben["ableitung"] = ableitung_modul.kit_feld(ableitung)
    knoten, kanten = belege.pruefe_belege(kit, knoten, kanten, befunde)

    atlas = ausgabe_modul.baue_atlas(kit_angaben, knoten, kanten, befunde)
    try:
        ausgabe_modul.schreibe(ausgabe, atlas)
    except OSError as e:
        return _abbruch(f"Fehler: {ausgabe_arg} lässt sich nicht schreiben ({e.strerror}).")
    if "--bericht" in schalter:
        bericht["namen"] = {
            "agenten": [a["name"] for a in agent_knoten],
            "werkzeuge": [w["datei"] for w in werkzeug_knoten],
            "skills": [s["name"] for s, _ in skill_paare],
        }
        bericht["ableitung"] = abgeleitet.bericht() if abgeleitet is not None else None
        try:
            ausgabe_modul.schreibe(Path(schalter["--bericht"]).expanduser(), bericht)
        except OSError as e:
            return _abbruch(f"Fehler: {schalter['--bericht']} lässt sich nicht schreiben ({e.strerror}).")
    entfallen = None if abgeleitet is None else sum(z["entfallen"] for z in abgeleitet.skills.values())
    sys.stdout.write(ausgabe_modul.zusammenfassung(atlas, ausgabe, entfallen))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
