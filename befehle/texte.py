from __future__ import annotations

ISSUES_URL = "https://github.com/MakeNative/atlas-kit/issues"

ANKUENDIGUNG = ("Erster Aufruf: atlas-kit richtet sein Hilfsprogramm zum Lesen ein. "
                "Das dauert etwa eine Minute und braucht einmal Internet.")
KEIN_INTERNET = "Kein Internet: PyYAML ließ sich nicht laden. Verbinde dich und ruf den Befehl noch einmal auf."
KEIN_VENV = "Python kann keine eigene Umgebung anlegen: sudo apt install python3-venv, dann noch einmal."
PIP_FEHLER = ("PyYAML ließ sich nicht installieren. Die Meldung von pip steht darunter; "
              "behebe die Ursache und ruf den Befehl noch einmal auf.")
UMGEBUNG_FEHLER = ("Python konnte keine eigene Umgebung für atlas-kit anlegen. Die Meldung steht darunter; "
                   "behebe die Ursache und ruf den Befehl noch einmal auf.")
ANDERE_SITZUNG = "atlas-kit wird gerade in einer anderen Sitzung eingerichtet. Ruf den Befehl gleich noch einmal auf."

ANALYSE = "/atlas:analyse"
INIT = "/atlas:init"


def nenn_einen(befehl: str) -> str:
    return f"Nenn einen Ordner oder den Namen eines installierten Kits, z. B. {befehl} ~/mein-kit oder {befehl} folien-kit."



NEU_ANGELEGT = ("Neu angelegt: der Ordner atlas-kit/ für die Ergebnisse. "
                "Soll er nicht ins Repo, trag ihn in .gitignore ein.")
WEITER = "Weiter mit /atlas:render."
KEINE_AUFFAELLIGKEITEN = "Keine Auffälligkeiten."
LEER_BEIDES = "Im Kit stehen keine Skills und keine Agenten."
LEER_SKILLS = "Im Kit stehen keine Skills."
LEER_AGENTEN = "Im Kit stehen keine Agenten."
OHNE_ATLAS = ("Das Kit hat keine Atlas-Angaben. Mit /atlas:init leitet Claude die Abläufe aus den Beschreibungen ab "
              "und baut die Seite.")
FEHLER_MELDEN = f"Das ist ein Fehler in atlas-kit. Wenn du magst, melde ihn unter {ISSUES_URL}."
MEHRERE_BAUPLAENE = "In atlas-kit/ liegen Baupläne mehrerer Kits. Nenn eines:"
NEU_EINLESEN = "Lies das Kit mit /atlas:scan neu ein."
BROWSER_OFFEN = "Im Browser geöffnet."
BROWSER_NICHT = "Der Browser hat sich nicht von selbst geöffnet. Öffne die Datei per Doppelklick."
VERWENDET_OHNE_DATUM = "Verwendet den vorhandenen Bauplan. Nach Änderungen am Kit zuerst /atlas:scan."
DAUERT = "Das dauert je nach Größe einige Minuten."
ABLEITUNG_WEITER = "/atlas:analyse leitet neu ab."


def im_projekt(satz: str) -> str:
    return satz.replace("Im Kit", "Im Projekt").replace("Das Kit", "Das Projekt")


def kein_kit(pfad: str, befehl: str) -> str:
    return (f"In {pfad} liegt weder ein Kit noch ein Projekt mit eigenen Skills oder Agenten: "
            f"Erwartet wird ein Ordner mit .claude-plugin/plugin.json oder mit .claude/skills bzw. .claude/agents. "
            f"{nenn_einen(befehl)}")


def ordner_fehlt(pfad: str, befehl: str) -> str:
    return f"Den Ordner {pfad} gibt es nicht. {nenn_einen(befehl)}"


def nichts_namens(argument: str, befehl: str) -> str:
    return f"Es gibt keinen Ordner und kein installiertes Kit namens {argument}. {nenn_einen(befehl)}"


def mehrere_installiert(name: str, voll: list[str]) -> str:
    return (f"Mehrere installierte Kits heißen {name}: {', '.join(voll)}. "
            f"Nenn eines davon, z. B. /atlas:scan {voll[0]}.")


def marktplatz(pfad: str) -> str:
    return f"In {pfad} liegt ein Marktplatz, kein einzelnes Kit. Nenn eines seiner Kits:"


def marktplatz_kit(befehl: str, pfad: str) -> str:
    return f"{befehl} {pfad}"


def plugin_unlesbar(pfad: str, grund: str) -> str:
    return f"Die Datei .claude-plugin/plugin.json in {pfad} ist nicht lesbar: {grund}."


def nicht_schreibbar(pfad: str) -> str:
    return f"In {pfad} kann atlas-kit nicht schreiben. Ruf den Befehl in einem Ordner auf, in dem du schreiben darfst."


def daten_nicht_schreibbar(pfad: str) -> str:
    return (f"In {pfad} kann atlas-kit nicht schreiben und deshalb sein Hilfsprogramm nicht einrichten. "
            f"Gib dir dort Schreibrecht und ruf den Befehl noch einmal auf.")


def einlesen_abgebrochen(meldung: str) -> str:
    return f"Einlesen abgebrochen: {meldung}"


def bauplan_render(name: str) -> str:
    return f"/atlas:render {name}"


def verwendet(datum: str) -> str:
    return f"Verwendet den Bauplan vom {datum}. Nach Änderungen am Kit zuerst /atlas:scan."


def seite_gebaut(pfad: str) -> str:
    return f"Seite gebaut: {pfad}"


def _zahl(n: int, eins: str, viele: str) -> str:
    return f"{n} {eins if n == 1 else viele}"


def eingelesen(kit_id: str, version: str | None, skills: int, agenten: int, ablaeufe: int) -> str:
    kopf = kit_id if version is None else f"{kit_id} {version}"
    return (f"{kopf} eingelesen: {_zahl(skills, 'Skill', 'Skills')}, "
            f"{_zahl(agenten, 'Agent', 'Agenten')}, {_zahl(ablaeufe, 'Ablauf', 'Abläufe')}.")


def befunde_beim_einlesen(anzahl: int, erster: str) -> str:
    if anzahl == 1:
        return f"1 Hinweis zum Bauplan: {erster}"
    return f"{anzahl} Hinweise zum Bauplan, z. B. {erster}"


def befunde_auf_der_seite(anzahl: int, erster: str) -> str:
    if anzahl == 1:
        return f"1 Hinweis zum Bauplan steht unten auf der Seite: {erster}"
    return f"{anzahl} Hinweise zum Bauplan stehen unten auf der Seite, z. B. {erster}"


def ankuendigung(kit_id: str, skills: int, agenten: int) -> str:
    anzahlen = [t for t in (_zahl(skills, "Skill", "Skills") if skills else None,
                            _zahl(agenten, "Agent", "Agenten") if agenten else None) if t]
    was = [t for t in ("Abläufe" if skills else None, "Rollen" if agenten else None) if t]
    return (f"{kit_id}: {' und '.join(anzahlen)} ohne eigene Atlas-Angaben. "
            f"Claude leitet ihre {' und '.join(was)} jetzt aus den Beschreibungen ab. {DAUERT}")


def nichts_abzuleiten(kit_id: str) -> str:
    return f"{kit_id}: Alle Skills und Agenten haben eigene Atlas-Angaben. Es gibt nichts abzuleiten."


def fortschritt(skill: str, schritte: int, weggelassen: int) -> str:
    if schritte == 0 and weggelassen == 0:
        return f"{skill}: nichts Belegbares gefunden"
    if schritte == 0:
        return f"{skill}: nichts belegt, {_zahl(weggelassen, 'Schritt', 'Schritte')} ohne Beleg weggelassen"
    zeile = f"{skill}: {_zahl(schritte, 'Schritt', 'Schritte')} abgeleitet"
    return zeile if weggelassen == 0 else f"{zeile}, {weggelassen} ohne Beleg weggelassen"


def nicht_lesbar(skill: str) -> str:
    return f"{skill}: Antwort nicht lesbar, kein Ablauf"


def rollen(abgeleitet: int, agenten: int, weggelassen: int) -> str:
    zeile = f"Rollen: {abgeleitet} von {_zahl(agenten, 'Agent', 'Agenten')} abgeleitet"
    return zeile if weggelassen == 0 else f"{zeile}, {weggelassen} ohne Beleg weggelassen"


def abgeleitet(ablaeufe: int, rollen_: int, weggelassen: int) -> str:
    zeile = f"Abgeleitet: {_zahl(ablaeufe, 'Ablauf', 'Abläufe')}, {_zahl(rollen_, 'Rolle', 'Rollen')}."
    if weggelassen == 0:
        return zeile
    return f"{zeile} {_zahl(weggelassen, 'Schritt', 'Schritte')} mangels Beleg weggelassen."


def eigene_bleiben(skills: list[str]) -> str:
    return f"Eigene Atlas-Angaben bleiben: {', '.join(skills)}."


def ohne_ablauf(skills: list[str]) -> str:
    return f"Ohne Ablauf: {', '.join(skills)}."


def _abgebrochen_bei(bei: str) -> str:
    return "den Rollen" if bei == "rollen" else bei


def abgebrochen(bei: str, befehl: str) -> str:
    return (f"Abgebrochen bei {_abgebrochen_bei(bei)}. Übernommen ist, was bis dahin belegt war. "
            f"Ruf {befehl} noch einmal auf, um neu abzuleiten.")


def mit_abgeleiteten(datum: str | None, ablaeufe: int, rollen_: int) -> str:
    vom = f" vom {datum}" if datum else ""
    return f"Mit abgeleiteten Abläufen{vom}: {_zahl(ablaeufe, 'Ablauf', 'Abläufe')}, {_zahl(rollen_, 'Rolle', 'Rollen')}."


def nicht_mehr_belegt(schritte: int) -> str:
    return f"{schritte} abgeleitete Schritte sind nicht mehr belegt und fehlen. {ABLEITUNG_WEITER}"


def unvollstaendig(bei: str) -> str:
    return f"Die Ableitung ist unvollständig (abgebrochen bei {_abgebrochen_bei(bei)})."


def ableitung_kaputt(name: str) -> str:
    return (f"Die abgeleiteten Abläufe in atlas-kit/{name}.ableitung.json sind nicht lesbar und bleiben außen vor. "
            f"{ABLEITUNG_WEITER}")


def kein_lauf(pfad: str) -> str:
    return (f"{pfad} ist kein Arbeitsordner einer laufenden Ableitung von atlas-kit. "
            f"Ruf {ANALYSE} oder {INIT} noch einmal auf.")
