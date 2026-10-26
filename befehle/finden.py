from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import texte

PLUGIN_JSON = Path(".claude-plugin") / "plugin.json"
MARKETPLACE_JSON = Path(".claude-plugin") / "marketplace.json"
INSTALLIERTE = Path("plugins") / "installed_plugins.json"
PROJEKT_SCOPES = ("project", "local")
PROJEKT_SKILLS = Path(".claude") / "skills"
PROJEKT_AGENTEN = Path(".claude") / "agents"
PLUGIN = "plugin"
PROJEKT = "projekt"


class KeinKit(Exception):
    def __init__(self, zeilen: list[str]):
        super().__init__("\n".join(zeilen))
        self.zeilen = zeilen


@dataclass
class Kit:
    ordner: Path
    kit_id: str
    art: str = PLUGIN


def tilde(pfad: Path, home: Path) -> str:
    for p, h in ((os.path.abspath(pfad), os.path.abspath(home)),
                 (os.path.realpath(pfad), os.path.realpath(home))):
        gross_klein = os.path.normcase(p)  # Windows: ohne Unterschied von Groß- und Kleinschreibung
        if h and h != os.sep and (gross_klein == os.path.normcase(h) or gross_klein.startswith(os.path.normcase(h + os.sep))):
            return "~" + p[len(h):].replace(os.sep, "/")
    return os.path.abspath(pfad)


def quelle(kit: Path, home: Path) -> str:
    return tilde(kit, home)


def ohne_anfuehrungszeichen(argument: str) -> str:
    wert = argument.strip()
    if len(wert) >= 2 and wert[0] == wert[-1] and wert[0] in "\"'":
        wert = wert[1:-1].strip()
    return wert


def ist_ordnerangabe(argument: str, arbeitsordner: Path) -> bool:
    if "/" in argument or "\\" in argument or argument.startswith((".", "~")):
        return True
    return (arbeitsordner / argument).is_dir()


def als_pfad(argument: str, arbeitsordner: Path, home: Path) -> Path:
    if argument == "~" or argument.startswith(("~/", "~\\")):
        argument = str(home) + argument[1:]
    pfad = Path(argument)
    if not pfad.is_absolute():
        pfad = arbeitsordner / pfad
    return Path(os.path.abspath(pfad))


def anzeige(argument: str, pfad: Path, home: Path) -> str:
    if Path(argument).is_absolute():
        return tilde(pfad, home)
    return argument


def _plugin_name(ordner: Path, home: Path) -> str:
    datei = ordner / PLUGIN_JSON
    try:
        daten = json.loads(datei.read_text(encoding="utf-8-sig"))  # BOM: PowerShell 5 unter Windows
    except UnicodeDecodeError:
        raise KeinKit([texte.plugin_unlesbar(tilde(ordner, home), "nicht als UTF-8 lesbar")])
    except json.JSONDecodeError as fehler:
        grund = f"kein gültiges JSON (Zeile {fehler.lineno}: {fehler.msg})"
        raise KeinKit([texte.plugin_unlesbar(tilde(ordner, home), grund)])
    except OSError as fehler:
        raise KeinKit([texte.plugin_unlesbar(tilde(ordner, home), fehler.strerror or "nicht lesbar")])
    name = daten.get("name") if isinstance(daten, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise KeinKit([texte.plugin_unlesbar(tilde(ordner, home), "kein Feld name")])
    return name


def _kits_im_marktplatz(ordner: Path, home: Path, befehl: str) -> list[str]:
    try:
        daten = json.loads((ordner / MARKETPLACE_JSON).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    plugins = daten.get("plugins") if isinstance(daten, dict) else None
    zeilen = []
    for eintrag in plugins if isinstance(plugins, list) else []:
        quelle_ = eintrag.get("source") if isinstance(eintrag, dict) else None
        if not isinstance(quelle_, str) or not quelle_.strip():
            continue
        kit = Path(os.path.abspath(ordner / quelle_))
        if (kit / PLUGIN_JSON).is_file():
            zeilen.append(texte.marktplatz_kit(befehl, tilde(kit, home)))
    return zeilen


def _marktplatz_zeilen(ordner: Path, home: Path, befehl: str) -> list[str]:
    kits = _kits_im_marktplatz(ordner, home, befehl) if (ordner / MARKETPLACE_JSON).is_file() else []
    return [texte.marktplatz(tilde(ordner, home)), *kits] if kits else []


def ist_projekt(ordner: Path, home: Path) -> bool:
    if os.path.realpath(ordner) == os.path.realpath(home):
        return False
    skills = ordner / PROJEKT_SKILLS
    if skills.is_dir() and any((unter / "SKILL.md").is_file() for unter in skills.iterdir() if unter.is_dir()):
        return True
    agenten = ordner / PROJEKT_AGENTEN
    return agenten.is_dir() and any(pfad.is_file() for pfad in agenten.glob("*.md"))


def ist_kit_oder_projekt(ordner: Path, home: Path, befehl: str) -> bool:
    if (ordner / PLUGIN_JSON).is_file():
        return True
    return not _marktplatz_zeilen(ordner, home, befehl) and ist_projekt(ordner, home)


def kein_kit_zeilen(ordner: Path, home: Path, befehl: str) -> list[str]:
    return _marktplatz_zeilen(ordner, home, befehl) or [texte.kein_kit(tilde(ordner, home), befehl)]


def pruefe_ordner(ordner: Path, home: Path, befehl: str) -> Kit:
    if (ordner / PLUGIN_JSON).is_file():
        return Kit(ordner, _plugin_name(ordner, home), PLUGIN)
    marktplatz = _marktplatz_zeilen(ordner, home, befehl)
    if marktplatz:
        raise KeinKit(marktplatz)
    if ist_projekt(ordner, home):
        return Kit(ordner, Path(os.path.realpath(ordner)).name, PROJEKT)
    raise KeinKit([texte.kein_kit(tilde(ordner, home), befehl)])


def _gleicher_ordner(a: str, b: Path) -> bool:
    """Gleicher Ordner über die Identität (st_dev, st_ino), damit Groß-/Kleinschreibung nichts umgeht."""
    try:
        return os.path.samefile(a, b)
    except (OSError, TypeError, ValueError):
        try:
            return os.path.realpath(a) == os.path.realpath(b)
        except (TypeError, ValueError):
            return False


def _installationsordner(eintraege: object, arbeitsordner: Path) -> str | None:
    if not isinstance(eintraege, list):
        return None
    passend = [e for e in eintraege
               if isinstance(e, dict) and isinstance(e.get("installPath"), str) and Path(e["installPath"]).is_dir()]
    for eintrag in passend:
        if eintrag.get("scope") in PROJEKT_SCOPES and isinstance(eintrag.get("projectPath"), str) \
                and _gleicher_ordner(eintrag["projectPath"], arbeitsordner):
            return eintrag["installPath"]
    for eintrag in passend:
        if eintrag.get("scope") == "user":
            return eintrag["installPath"]
    return None


def _installierte_eintraege(home: Path) -> dict:
    konfiguration = os.environ.get("CLAUDE_CONFIG_DIR") or str(home / ".claude")
    try:
        daten = json.loads((Path(konfiguration) / INSTALLIERTE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    plugins = daten.get("plugins") if isinstance(daten, dict) else None
    return plugins if isinstance(plugins, dict) else {}


def _eltern(pfad: Path) -> list[Path]:
    kette: list[Path] = []
    for start in (Path(os.path.abspath(pfad)), Path(os.path.realpath(pfad))):
        kette += [start, *start.parents]
    return kette


def liegt_unter(pfad: Path, ordner: Path) -> bool:
    """Ist pfad gleich ordner oder liegt darin? Geprüft über die Identität der Ordner (st_dev, st_ino) entlang
    des angegebenen UND des aufgelösten Pfads (realpath), nicht über die Schreibweise."""
    ziele = []
    for kandidat in (ordner, Path(os.path.realpath(ordner))):
        try:
            ziele.append(os.stat(kandidat))
        except OSError:
            continue
    if not ziele:
        return False
    for kandidat in _eltern(pfad):
        try:
            info = os.stat(kandidat)
        except OSError:
            continue
        if any(os.path.samestat(info, ziel) for ziel in ziele):
            return True
    return False


def ist_installierte_kopie(ordner: Path, home: Path, konfiguration: Path | None = None) -> bool:
    """WS-S4: unter <HOME>/.claude/plugins bzw. <CLAUDE_CONFIG_DIR>/plugins (über die Identität der Ordner, nicht die
    Schreibweise) oder ein Installationsordner aus installed_plugins.json. Für Server (schreibbar) und knopf."""
    konfiguration = konfiguration or Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    if any(liegt_unter(ordner, wurzel) for wurzel in (home / ".claude" / "plugins", konfiguration / "plugins")):
        return True
    return ist_installationsordner(ordner, home)


def ist_installationsordner(ordner: Path, home: Path) -> bool:
    for eintraege in _installierte_eintraege(home).values():
        for eintrag in eintraege if isinstance(eintraege, list) else []:
            pfad = eintrag.get("installPath") if isinstance(eintrag, dict) else None
            if isinstance(pfad, str) and _gleicher_ordner(pfad, ordner):
                return True
    return False


def installierte_kits(arbeitsordner: Path, home: Path) -> dict[str, Path]:
    kits = {}
    for voll, eintraege in _installierte_eintraege(home).items():
        ordner = _installationsordner(eintraege, arbeitsordner)
        if ordner is not None:
            kits[voll] = Path(ordner)
    return kits


def installiertes_kit(name: str, arbeitsordner: Path, home: Path) -> Path | None:
    kits = installierte_kits(arbeitsordner, home)
    if "@" in name:
        return kits.get(name)
    treffer = sorted(voll for voll in kits if voll.split("@", 1)[0] == name)
    if len(treffer) > 1:
        raise KeinKit([texte.mehrere_installiert(name, treffer)])
    return kits[treffer[0]] if treffer else None


def _installierte_namens(name: str, arbeitsordner: Path, home: Path) -> list[tuple[str, Path]]:
    kits = installierte_kits(arbeitsordner, home)
    if "@" in name:
        return [(name, kits[name])] if name in kits else []
    return [(voll, kits[voll]) for voll in sorted(kits) if voll.split("@", 1)[0] == name]


def _mit_eigenen(name: str, arbeitsordner: Path, home: Path, befehl: str, eigene: list[Kit], auswahl: str) -> Kit:
    """WS-NK9: eigene, zuletzt geöffnete Kits (kit.id oder Ordnername) und installierte Kits dieses Namens; auswahl ist
    der Befehl vor jeder Zeile der Auswahl (mit Unterbefehl, z. B. '/atlas:werkstatt stopp')."""
    treffer = [k for k in eigene if "@" not in name and (k.kit_id == name or k.ordner.name == name)]
    installiert = [(voll, ordner) for voll, ordner in _installierte_namens(name, arbeitsordner, home)
                   if not any(_gleicher_ordner(str(ordner), k.ordner) for k in treffer)]
    if len(treffer) + len(installiert) > 1:
        raise KeinKit([texte.mehrere_kits(name), *(f"{auswahl} {tilde(k.ordner, home)}" for k in treffer),
                       *(f"{auswahl} {voll}" for voll, _ in installiert)])
    if treffer:
        return treffer[0]
    if installiert:
        return pruefe_ordner(Path(os.path.abspath(installiert[0][1])), home, befehl)
    raise KeinKit([texte.nichts_namens(name, befehl)])


def finde_kit(argument: str, arbeitsordner: Path, home: Path, befehl: str, eigene: list[Kit] | None = None,
              auswahl: str | None = None) -> Kit:
    """eigene: zuletzt geöffnete Kits, die über den Namen mit gefunden werden (nur die Werkstatt, WS-NK9); auswahl: der
    Befehl in den Zeilen einer Auswahl, ohne Angabe befehl."""
    if ist_ordnerangabe(argument, arbeitsordner):
        ordner = als_pfad(argument, arbeitsordner, home)
        if not ordner.is_dir():
            raise KeinKit([texte.ordner_fehlt(anzeige(argument, ordner, home), befehl)])
        return pruefe_ordner(ordner, home, befehl)
    if eigene is not None:
        return _mit_eigenen(argument, arbeitsordner, home, befehl, eigene, auswahl or befehl)
    installiert = installiertes_kit(argument, arbeitsordner, home)
    if installiert is None:
        raise KeinKit([texte.nichts_namens(argument, befehl)])
    return pruefe_ordner(Path(os.path.abspath(installiert)), home, befehl)
