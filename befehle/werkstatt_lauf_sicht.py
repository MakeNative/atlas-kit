from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

ARTEN = ("sitzung_start", "sitzung_ende", "eingabe", "stopp", "agent_start", "agent_ende", "agent_fehler",
         "subagent_start", "subagent_stop", "skill", "werkzeug", "werkzeug_fehler", "frage", "frage_beantwortet",
         "freigabe", "hinweis", "blockiert", "voll")
STARTET, LAEUFT, WARTET = "startet", "laeuft", "wartet"
BEENDET, ABGEBROCHEN, ABGELEGT = "beendet", "abgebrochen", "abgelegt"
OFFEN = (STARTET, LAEUFT, WARTET)
GRUND_ABGESCHLOSSEN = "von dir abgeschlossen"
GRUND_ABGELEGT = "ohne Ende abgelegt"
NICHT_IM_KIT = "gehört nicht zum Kit"
OHNE_ABLAUF = "Diese Skill hat keinen Ablauf; nur die Zeitleiste zeigt den Lauf."
TYP_FEHLT = "Typ des Agenten fehlt; nicht zuzuordnen."
ENDE_OHNE_START = "Ende eines Agentenlaufs ohne passenden Start; nicht zuzuordnen."
HINTERGRUND = "im Hintergrund gestartet; Dauer nicht messbar"
ENDE_NICHT_GEMELDET = "Ende nicht gemeldet"
STILL_START, STILL_RUHE = "start", "ruhe"
AUSFUEHRENDE = ("agent:", "skill:", "werkzeug:")
SKILL_EBENEN = 3
NICHT_ZUSTANDSWIRKSAM = ("hinweis", "voll")
BEENDEN_DAS_WARTEN_AUF_ANTWORT = ("werkzeug", "werkzeug_fehler", "agent_start", "agent_ende", "agent_fehler", "skill",
                                  "eingabe", "frage_beantwortet", "blockiert", "stopp", "frage", "freigabe",
                                  "sitzung_start")
SCHMALE_ARTEN = ("sitzung_start", "sitzung_ende", "frage", "freigabe", "stopp", "eingabe", "blockiert",
                 "werkzeug_fehler")


def geteilt_satz(nummern: list[int], name: str) -> str:
    return f"Schritte {', '.join(str(n) for n in nummern)} starten {name}."


def freigabe_satz(text: str | None) -> str:
    um_was = f": {text}" if text else ""
    return (f"Claude bittet im Terminal um Freigabe{um_was}. Hast du schon freigegeben, arbeitet Claude jetzt daran; "
            f"die Werkstatt sieht das erst beim nächsten Schritt.")


def kein_schritt(name: str) -> str:
    return f"Kein Schritt dieses Ablaufs startet {name}."


def zeit_von(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        zeit = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return zeit if zeit.tzinfo is not None else zeit.astimezone()


def zeit_text(zeit: datetime) -> str:
    return zeit.astimezone().isoformat(timespec="milliseconds")


def gueltig(zeile: object) -> bool:
    if not isinstance(zeile, dict) or zeile.get("v") != 1 or zeile.get("art") not in ARTEN:
        return False
    if zeit_von(zeile.get("zeit")) is None or not isinstance(zeile.get("daten"), dict):
        return False
    return all(zeile.get(feld) is None or isinstance(zeile.get(feld), str)
               for feld in ("sitzung", "agent", "agent_typ", "id"))


@dataclass
class Marken:
    abgebrochen: datetime | None = None
    abgeschlossen: datetime | None = None


@dataclass
class Zustand:
    zustand: str
    wartet: dict | None
    grund: str | None
    ende: datetime | None


def _sekunden(von: datetime, bis: datetime) -> int:
    return max(0, int(round((bis - von).total_seconds())))


def _mit_zeit(ereignisse: list[dict]) -> list[tuple[datetime, dict]]:
    return [(zeit_von(e["zeit"]), e) for e in ereignisse]


def _aus_ereignissen(geschrieben: list[tuple[datetime, dict]]) -> Zustand:
    enden = [t for t, e in geschrieben if e["art"] == "sitzung_ende"]
    if enden:
        return Zustand(BEENDET, None, None, max(enden))
    wartet = None
    for _, e in geschrieben:
        art = e["art"]
        if art in NICHT_ZUSTANDSWIRKSAM:
            continue
        if wartet is not None and wartet["art"] == "eingabe" or art in BEENDEN_DAS_WARTEN_AUF_ANTWORT:
            wartet = None
        if art == "frage":
            wartet = {"art": "frage", "text": e["daten"].get("text")}
        elif art == "freigabe":
            teile = [e["daten"].get("werkzeug"), e["daten"].get("kurz")]
            text = " ".join(t for t in teile if isinstance(t, str) and t) or None
            wartet = {"art": "freigabe", "text": text, "satz": freigabe_satz(text)}
        elif art == "stopp":
            wartet = {"art": "eingabe", "text": None}
    if wartet is not None:
        return Zustand(WARTET, wartet, None, None)
    return Zustand(LAEUFT if geschrieben else STARTET, None, None, None)


def zustand(ereignisse: list[dict], marken: Marken) -> Zustand:
    geschrieben = _mit_zeit(ereignisse)
    if marken.abgeschlossen is not None:
        vorher = _aus_ereignissen([(t, e) for t, e in geschrieben if t <= marken.abgeschlossen])
        if vorher.zustand == BEENDET:
            return vorher
        if vorher.zustand == WARTET and vorher.wartet["art"] == "eingabe":
            return Zustand(BEENDET, None, GRUND_ABGESCHLOSSEN, marken.abgeschlossen)
        return Zustand(ABGELEGT, None, GRUND_ABGELEGT, marken.abgeschlossen)
    if marken.abgebrochen is not None:
        return Zustand(ABGEBROCHEN, None, None, marken.abgebrochen)
    return _aus_ereignissen(geschrieben)


def still(lauf: dict, zuletzt: datetime | None, stand: Zustand, jetzt: datetime, still_s: float,
          ruhe_s: float) -> dict | None:
    if stand.zustand not in OFFEN:
        return None
    if zuletzt is None:
        gestartet = zeit_von(lauf.get("gestartet"))
        seit = (jetzt - gestartet).total_seconds() if gestartet else 0
        return {"art": STILL_START, "seit_s": int(seit)} if seit >= still_s else None
    seit = (jetzt - zuletzt).total_seconds()
    return {"art": STILL_RUHE, "seit_s": int(seit)} if seit >= ruhe_s else None


@dataclass
class Zusammenfassung:
    stand: Zustand
    zuletzt: datetime | None
    agenten: int
    tokens: int | None
    tokens_unbekannt: int


def fasse_zusammen(ereignisse: list[dict], marken: Marken) -> Zusammenfassung:
    mit_zeit = sorted(((zeit_von(e["zeit"]), i, e) for i, e in enumerate(ereignisse)), key=lambda t: (t[0], t[1]))
    offen: set[str] = set()
    agenten = geendet = 0
    bekannt: list[int] = []
    for _, _, e in mit_zeit:
        if e["art"] == "agent_start":
            agenten += 1
            if isinstance(e.get("id"), str):
                offen.add(e["id"])
        elif e["art"] in ("agent_ende", "agent_fehler") and e.get("id") in offen:
            offen.discard(e["id"])
            geendet += 1
            wert = e["daten"].get("tokens") if e["art"] == "agent_ende" else None
            if isinstance(wert, int) and not isinstance(wert, bool) and wert >= 0:
                bekannt.append(wert)
    zuletzt = mit_zeit[-1][0] if mit_zeit else None
    return Zusammenfassung(zustand(ereignisse, marken), zuletzt, agenten, sum(bekannt) if bekannt else None,
                           geendet - len(bekannt))


# ---------------------------------------------------------------- Bauplan des Laufs

@dataclass
class Zuordnung:
    knoten: str | None
    schritt: dict | None = None
    ueber_skill: str | None = None
    schritte: list[str] = field(default_factory=list)
    satz: str | None = None
    zaehlt: bool = True

    @property
    def zaehlende_schritte(self) -> list[str]:
        if not self.zaehlt:
            return []
        return list(self.schritte) if self.schritte else ([self.schritt["knoten"]] if self.schritt else [])


class Ablaufplan:
    def __init__(self, bauplan: dict, skill: str):
        knoten = [k for k in bauplan.get("knoten", []) if isinstance(k, dict) and isinstance(k.get("id"), str)]
        self.knoten = {k["id"]: k for k in knoten}
        self.skill_id = f"skill:{skill}"
        self.schritte_je_skill: dict[str, list[dict]] = {}
        for k in knoten:
            if k.get("typ") == "schritt" and isinstance(k.get("skill"), str):
                self.schritte_je_skill.setdefault(k["skill"], []).append(k)
        for liste in self.schritte_je_skill.values():
            liste.sort(key=lambda k: k.get("nr") if isinstance(k.get("nr"), int) else 0)
        self.schritte = self.schritte_je_skill.get(self.skill_id, [])
        self.ziele: dict[str, list[str]] = {}
        self.aufrufe: dict[str, list[str]] = {}
        for kante in bauplan.get("kanten", []):
            if not isinstance(kante, dict) or kante.get("typ") not in ("startet", "parallel") \
                    or not isinstance(kante.get("von"), str) or not isinstance(kante.get("nach"), str) \
                    or not kante["nach"].startswith(AUSFUEHRENDE):
                continue
            ziele = self.ziele.setdefault(kante["von"], [])
            if kante["nach"] not in ziele:
                ziele.append(kante["nach"])
            if kante["typ"] == "startet" and kante["nach"].startswith("skill:"):
                self.aufrufe.setdefault(kante["von"], []).append(kante["nach"])
        self.werkzeuge = [(k["id"], k["datei"]) for k in knoten
                          if k.get("typ") == "werkzeug" and isinstance(k.get("datei"), str) and k["datei"]]

    def mit_ausfuehrendem(self) -> list[dict]:
        return [s for s in self.schritte if self.ziele.get(s["id"])]

    def _skill_name(self, skill_id: str) -> str:
        return self.knoten.get(skill_id, {}).get("name") or skill_id.split(":", 1)[1]

    def _weg(self, schritt_id: str, knoten: str, tiefe: int, besucht: frozenset) -> bool:
        if knoten in self.ziele.get(schritt_id, []):
            return True
        if tiefe >= SKILL_EBENEN:
            return False
        for skill_id in self.aufrufe.get(schritt_id, []):
            if skill_id in besucht or skill_id not in self.knoten:
                continue
            if any(self._weg(u["id"], knoten, tiefe + 1, besucht | {skill_id})
                   for u in self.schritte_je_skill.get(skill_id, [])):
                return True
        return False

    def _erreicht(self, schritt_id: str, knoten: str) -> tuple[bool, str | None]:
        if knoten in self.ziele.get(schritt_id, []):
            return True, None
        start = frozenset({self.skill_id})
        for skill_id in self.aufrufe.get(schritt_id, []):
            if skill_id in start or skill_id not in self.knoten:
                continue
            if any(self._weg(u["id"], knoten, 1, start | {skill_id}) for u in self.schritte_je_skill.get(skill_id, [])):
                return True, self._skill_name(skill_id)
        return False, None

    def ordne_zu(self, knoten: str | None, name: str | None, nur_direkt: bool = False) -> Zuordnung:
        if knoten is None or knoten not in self.knoten:
            return Zuordnung(None, satz=NICHT_IM_KIT)
        if not self.schritte:
            return Zuordnung(knoten, satz=OHNE_ABLAUF)
        passende = []
        for s in self.schritte:
            if nur_direkt:
                treffer, ueber = knoten in self.ziele.get(s["id"], []), None
            else:
                treffer, ueber = self._erreicht(s["id"], knoten)
            if treffer:
                passende.append((s, ueber))
        if len(passende) == 1:
            s, ueber = passende[0]
            return Zuordnung(knoten, {"knoten": s["id"], "nr": s.get("nr"), "titel": s.get("titel")}, ueber)
        if passende:
            return Zuordnung(knoten, schritte=[s["id"] for s, _ in passende],
                             satz=geteilt_satz([s.get("nr") for s, _ in passende], name or knoten))
        return Zuordnung(knoten, satz=kein_schritt(name or knoten))

    def werkzeug_fuer(self, befehl: object) -> str | None:
        if not isinstance(befehl, str):
            return None
        treffer = [kid for kid, datei in self.werkzeuge if datei in befehl]
        return treffer[0] if len(treffer) == 1 else None


def _name(typ: object, kit_id: str, plugin: bool) -> str | None:
    if not isinstance(typ, str) or not typ:
        return None
    praefix = f"{kit_id}:"
    return typ[len(praefix):] if plugin and typ.startswith(praefix) else typ


# ---------------------------------------------------------------- Läufe einzelner Ausführender

@dataclass
class Agentenlauf:
    index: int
    zeit: datetime
    ereignis: dict
    name: str | None
    zuordnung: Zuordnung
    runde: int | None
    aufrufer: dict
    ende: tuple[datetime, dict] | None = None
    ende_index: int | None = None
    hintergrund_ende: datetime | None = None
    log: list[dict] = field(default_factory=list)

    @property
    def hintergrund(self) -> bool:
        return self.ereignis["daten"].get("hintergrund") is True

    @property
    def hintergrund_agent(self) -> str | None:
        if self.ende is None:
            return None
        agent = self.ende[1]["daten"].get("agent_id")
        return agent if isinstance(agent, str) and agent else None

    @property
    def fertig(self) -> bool:
        return self.hintergrund_ende is not None if self.hintergrund else self.geendet

    @property
    def geendet(self) -> bool:
        return self.ende is not None

    @property
    def schritt(self) -> dict | None:
        return self.zuordnung.schritt

    @property
    def fehler(self) -> str | None:
        if self.ende is None or self.ende[1]["art"] != "agent_fehler":
            return None
        fehler = self.ende[1]["daten"].get("fehler")
        return fehler if isinstance(fehler, str) else ""

    @property
    def tokens(self) -> int | None:
        if self.ende is None or self.ende[1]["art"] != "agent_ende":
            return None
        wert = self.ende[1]["daten"].get("tokens")
        return wert if isinstance(wert, int) and not isinstance(wert, bool) and wert >= 0 else None


@dataclass
class Einzellauf:
    art: str
    index: int
    zeit: datetime
    ereignis: dict
    name: str | None
    zuordnung: Zuordnung
    runde: int | None
    aufrufer: dict
    ende: datetime | None = None

    @property
    def schritt(self) -> dict | None:
        return self.zuordnung.schritt


class Sicht:
    def __init__(self, lauf: dict, bauplan: dict, ereignisse: list[dict], marken: Marken, jetzt: datetime,
                 kit_id: str):
        self.lauf = lauf
        self.jetzt = jetzt
        self.kit_id = kit_id
        kit = lauf.get("kit") if isinstance(lauf.get("kit"), dict) else {}
        self.plugin = kit.get("art", "plugin") == "plugin"
        self.plan = Ablaufplan(bauplan if isinstance(bauplan, dict) else {}, str(lauf.get("skill", "")))
        self.geordnet = sorted(enumerate(ereignisse), key=lambda paar: zeit_von(paar[1]["zeit"]))
        self.stand = zustand(ereignisse, marken)
        self.abgeschlossen = self.stand.zustand not in OFFEN
        self.runden: dict[str, int] = {}
        self.runden_je_schritt: dict[str, dict[str, int]] = {}
        self.aufrufer = self._aufrufer(ereignisse)
        self.agentenlaeufe: list[Agentenlauf] = []
        self.einzellaeufe: list[Einzellauf] = []
        self.ohne_start: list[tuple[int, datetime, dict]] = []
        self.hauptsitzung: list[dict] = []
        self.uneindeutig: dict[str, list[dict]] = {}
        self._offen_je_typ: dict[str, list[Agentenlauf]] = {}
        self._im_hintergrund: dict[str, Agentenlauf] = {}
        self._baue()

    def _aufrufer(self, ereignisse: list[dict]) -> dict[int, dict]:
        zuletzt: dict[str | None, tuple[str | None, str]] = {}
        antwort: dict[int, dict] = {}
        for index, e in enumerate(ereignisse):
            if e["art"] in ("agent_start", "skill", "werkzeug"):
                agent = e.get("agent")
                skill = zuletzt.get((e.get("sitzung"), agent))
                antwort[index] = {"wer": "hauptsitzung" if agent is None else "agent",
                                  "name": None if agent is None else _name(e.get("agent_typ"), self.kit_id, self.plugin),
                                  "skill": skill[0] if skill else None, "skill_zeit": skill[1] if skill else None}
            if e["art"] == "skill":
                zuletzt[(e.get("sitzung"), e.get("agent"))] = (_name(e["daten"].get("name"), self.kit_id, self.plugin),
                                                                 e["zeit"])
        return antwort

    def _runde(self, knoten: str | None, zuordnung: Zuordnung) -> int | None:
        if knoten is None:
            return None
        for schritt_id in zuordnung.zaehlende_schritte:
            je = self.runden_je_schritt.setdefault(schritt_id, {})
            je[knoten] = je.get(knoten, 0) + 1
        if not zuordnung.zaehlt or zuordnung.schritte:
            return None
        if zuordnung.schritt is not None:
            return self.runden_je_schritt[zuordnung.schritt["knoten"]][knoten]
        self.runden[knoten] = self.runden.get(knoten, 0) + 1
        return self.runden[knoten]

    def _baue(self) -> None:
        offen: dict[str, Agentenlauf] = {}
        werkzeug_offen: list[Einzellauf] = []
        for index, e in self.geordnet:
            zeit = zeit_von(e["zeit"])
            for w in list(werkzeug_offen):
                if w.ende is None and w.ereignis.get("sitzung") == e.get("sitzung") \
                        and w.ereignis.get("agent") == e.get("agent") and w.index != index:
                    w.ende = zeit
                    werkzeug_offen.remove(w)
            art = e["art"]
            if art == "agent_start":
                self._agent_start(index, zeit, e, offen)
            elif art in ("agent_ende", "agent_fehler"):
                lauf = offen.pop(e["id"], None) if isinstance(e.get("id"), str) else None
                if lauf is None:
                    self.ohne_start.append((index, zeit, e))
                else:
                    lauf.ende = (zeit, e)
                    lauf.ende_index = index
                    if lauf.hintergrund and lauf.hintergrund_agent is not None:
                        self._im_hintergrund[lauf.hintergrund_agent] = lauf
                    else:
                        self._nicht_mehr_offen(lauf)
            elif art == "subagent_stop" and e.get("agent") in self._im_hintergrund:
                lauf = self._im_hintergrund.pop(e["agent"])
                lauf.hintergrund_ende = zeit
                self._nicht_mehr_offen(lauf)
            elif art == "skill":
                self._skill(index, zeit, e)
            elif art == "werkzeug":
                werkzeuglauf = self._werkzeug(index, zeit, e)
                if werkzeuglauf is not None:
                    werkzeug_offen.append(werkzeuglauf)
                else:
                    self._log_zeile(zeit, e)

    def _agent_start(self, index: int, zeit: datetime, e: dict, offen: dict[str, Agentenlauf]) -> None:
        name = _name(e["daten"].get("subagent_type"), self.kit_id, self.plugin)
        if name is None:
            lauf = Agentenlauf(index, zeit, e, None, Zuordnung(None, satz=TYP_FEHLT), None, self.aufrufer[index])
        else:
            zuordnung = self.plan.ordne_zu(f"agent:{name}", name)
            lauf = Agentenlauf(index, zeit, e, name, zuordnung, self._runde(zuordnung.knoten or f"name:{name}",
                                                                          zuordnung), self.aufrufer[index])
        self.agentenlaeufe.append(lauf)
        typ = e["daten"].get("subagent_type")
        if isinstance(typ, str):
            self._offen_je_typ.setdefault(typ, []).append(lauf)
        if isinstance(e.get("id"), str):
            offen[e["id"]] = lauf

    def _skill(self, index: int, zeit: datetime, e: dict) -> None:
        name = _name(e["daten"].get("name"), self.kit_id, self.plugin)
        if name is None or name == self.lauf.get("skill"):
            return
        zuordnung = self.plan.ordne_zu(f"skill:{name}", name, nur_direkt=True)
        if zuordnung.schritt is None and not zuordnung.schritte and zuordnung.knoten is not None \
                and self.plan.schritte:
            erreicht = self.plan.ordne_zu(f"skill:{name}", name)
            if erreicht.schritt is not None or erreicht.schritte:
                erreicht.zaehlt = False
                zuordnung = erreicht
        self.einzellaeufe.append(Einzellauf("skill", index, zeit, e, name, zuordnung,
                                            self._runde(zuordnung.knoten or f"skill-name:{name}", zuordnung),
                                            self.aufrufer[index], zeit))

    def _werkzeug(self, index: int, zeit: datetime, e: dict) -> Einzellauf | None:
        if e["daten"].get("werkzeug") != "Bash":
            return None
        knoten = self.plan.werkzeug_fuer(e["daten"].get("kurz"))
        if knoten is None:
            return None
        name = self.plan.knoten[knoten].get("name") or knoten.split(":", 1)[1]
        zuordnung = self.plan.ordne_zu(knoten, name)
        lauf = Einzellauf("werkzeug", index, zeit, e, name, zuordnung, self._runde(knoten, zuordnung),
                          self.aufrufer[index])
        self.einzellaeufe.append(lauf)
        return lauf

    def _nicht_mehr_offen(self, lauf: Agentenlauf) -> None:
        typ = lauf.ereignis["daten"].get("subagent_type")
        if lauf in self._offen_je_typ.get(typ, []):
            self._offen_je_typ[typ].remove(lauf)

    def _log_zeile(self, zeit: datetime, e: dict) -> None:
        zeile = {"zeit": e["zeit"], "werkzeug": e["daten"].get("werkzeug"), "kurz": e["daten"].get("kurz")}
        if e.get("agent") is None:
            self.hauptsitzung.append(zeile)
            return
        typ = e.get("agent_typ")
        if e["agent"] in self._im_hintergrund:
            self._im_hintergrund[e["agent"]].log.append(zeile)
            return
        offen = self._offen_je_typ.get(typ, []) if isinstance(typ, str) else []
        if typ is not None and len(offen) == 1:
            offen[0].log.append(zeile)
        else:
            self.uneindeutig.setdefault(_name(typ, self.kit_id, self.plugin) or "?", []).append(zeile)

    # ------------------------------------------------------------ Dauer

    def dauer_s(self, a: Agentenlauf) -> int | None:
        return self._dauer_agent(a)[0]

    def _dauer_agent(self, a: Agentenlauf) -> tuple[int | None, str | None, bool]:
        if a.hintergrund:
            bekannt = not a.geendet or a.hintergrund_agent is not None
            return None, HINTERGRUND, bekannt and not a.fertig and not self.abgeschlossen
        if a.geendet:
            return _sekunden(a.zeit, a.ende[0]), None, False
        if self.abgeschlossen:
            return None, ENDE_NICHT_GEMELDET, False
        return _sekunden(a.zeit, self.jetzt), None, True

    def _dauer_einzeln(self, w: Einzellauf) -> tuple[int | None, bool]:
        if w.ende is not None:
            return _sekunden(w.zeit, w.ende), False
        if self.abgeschlossen:
            return None, False
        return _sekunden(w.zeit, self.jetzt), True

    # ------------------------------------------------------------ Antworten

    def _geteilt(self, schritt_id: str) -> dict | None:
        laeufe = [(a.zeit, a.index, a.name, a.zuordnung.schritte) for a in self.agentenlaeufe
                  if a.zuordnung.zaehlt and schritt_id in a.zuordnung.schritte]
        laeufe += [(w.zeit, w.index, w.name, w.zuordnung.schritte) for w in self.einzellaeufe
                   if w.zuordnung.zaehlt and schritt_id in w.zuordnung.schritte]
        if not laeufe:
            return None
        laeufe.sort(key=lambda t: (t[0], t[1]))
        nummer = {s["id"]: s.get("nr") for s in self.plan.schritte}
        auch = sorted({nummer[k] for t in laeufe for k in t[3] if k != schritt_id and isinstance(nummer.get(k), int)})
        return {"laeufe": len(laeufe), "agenten": list(dict.fromkeys(t[2] for t in laeufe)), "auch": auch}

    def schritte(self) -> list[dict]:
        antwort = []
        for s in self.plan.mit_ausfuehrendem():
            agenten = [a for a in self.agentenlaeufe if s["id"] in a.zuordnung.zaehlende_schritte]
            werkzeuge = [w for w in self.einzellaeufe
                         if w.art == "werkzeug" and s["id"] in w.zuordnung.zaehlende_schritte]
            aufgerufen = any(w.art == "skill" and s["id"] in w.zuordnung.zaehlende_schritte for w in self.einzellaeufe)
            laeuft = [self._dauer_agent(a)[2] for a in agenten] + [self._dauer_einzeln(w)[1] for w in werkzeuge]
            geendet = [a.fertig for a in agenten] + [w.ende is not None for w in werkzeuge]
            if any(laeuft):
                stand = "aktiv"
            elif geendet and all(geendet):
                stand = "erledigt"
            elif aufgerufen:
                stand = "aufgerufen"
            else:
                stand = None
            antwort.append({"knoten": s["id"], "zustand": stand,
                            "runden": max(self.runden_je_schritt.get(s["id"], {}).values(), default=0),
                            "fehler": any(a.fehler is not None for a in agenten), "geteilt": self._geteilt(s["id"])})
        return antwort

    def _agent_eintrag(self, a: Agentenlauf) -> dict:
        dauer, satz, laeuft = self._dauer_agent(a)
        antwort = a.ende[1]["daten"].get("antwort") if a.ende is not None and a.ende[1]["art"] == "agent_ende" else None
        gekuerzt = a.ende is not None and a.ende[1]["daten"].get("gekuerzt") is True
        typ = a.ereignis["daten"].get("subagent_type")
        return {"art": "agent", "zeit": a.ereignis["zeit"], "id": a.ereignis.get("id"), "name": a.name,
                "typ": typ if isinstance(typ, str) else None, **self._zuordnung(a.zuordnung, a.aufrufer),
                "runde": a.runde, "dauer_s": dauer, "dauer_satz": satz, "laeuft": laeuft,
                "tokens": a.tokens, "auftrag": a.ereignis["daten"].get("beschreibung"),
                "antwort": antwort if isinstance(antwort, str) else None, "gekuerzt": gekuerzt, "fehler": a.fehler,
                "log": a.log}

    def _einzel_eintrag(self, w: Einzellauf) -> dict:
        eintrag = {"art": w.art, "zeit": w.ereignis["zeit"], "id": w.ereignis.get("id"), "name": w.name}
        if w.art == "werkzeug":
            dauer, laeuft = self._dauer_einzeln(w)
            eintrag.update({"kurz": w.ereignis["daten"].get("kurz"), **self._zuordnung(w.zuordnung, w.aufrufer),
                            "runde": w.runde, "dauer_s": dauer, "laeuft": laeuft})
        else:
            eintrag.update({**self._zuordnung(w.zuordnung, w.aufrufer), "runde": w.runde})
        return eintrag

    @staticmethod
    def _zuordnung(z: Zuordnung, aufrufer: dict) -> dict:
        return {"knoten": z.knoten, "schritt": z.schritt, "ueber_skill": z.ueber_skill, "schritte": list(z.schritte),
                "satz": z.satz, "aufrufer": aufrufer}

    def _schmal(self, e: dict) -> dict:
        d = e["daten"]
        art = e["art"]
        eintrag = {"art": art, "zeit": e["zeit"]}
        if art == "sitzung_start":
            eintrag["quelle"] = d.get("quelle")
        elif art == "sitzung_ende":
            eintrag["grund"] = d.get("grund")
        elif art == "frage":
            eintrag.update({"text": d.get("text"), "fragen": d.get("fragen")})
        elif art == "freigabe":
            eintrag.update({"werkzeug": d.get("werkzeug"), "kurz": d.get("kurz")})
        elif art == "blockiert":
            eintrag["werkzeug"] = d.get("werkzeug")
        elif art == "werkzeug_fehler":
            eintrag.update({"werkzeug": d.get("werkzeug"), "fehler": d.get("fehler"), "agent": e.get("agent")})
        return eintrag

    def zeitleiste(self) -> list[dict]:
        eintraege: list[tuple[datetime, int, dict]] = []
        for a in self.agentenlaeufe:
            eintraege.append((a.zeit, a.index, self._agent_eintrag(a)))
            if a.ende is not None and a.ende[1]["art"] == "agent_fehler":
                ende = a.ende[1]
                eintraege.append((a.ende[0], a.ende_index, {
                    "art": "agent_fehler", "zeit": ende["zeit"], "id": ende.get("id"), "name": a.name,
                    "fehler": a.fehler}))
        for w in self.einzellaeufe:
            eintraege.append((w.zeit, w.index, self._einzel_eintrag(w)))
        for index, zeit, e in self.ohne_start:
            eintraege.append((zeit, index, {"art": "ende_ohne_start", "zeit": e["zeit"], "id": e.get("id"),
                                            "satz": ENDE_OHNE_START}))
        for index, e in self.geordnet:
            if e["art"] in SCHMALE_ARTEN:
                eintraege.append((zeit_von(e["zeit"]), index, self._schmal(e)))
        eintraege.sort(key=lambda t: (t[0], t[1]))
        liste = [e for _, _, e in eintraege]
        if self.hauptsitzung:
            liste.append({"art": "hauptsitzung", "log": self.hauptsitzung})
        for typ, log in self.uneindeutig.items():
            liste.append({"art": "uneindeutig", "typ": typ, "log": log})
        return liste

    def kosten(self, dauer_s: int) -> dict:
        je: dict[str, dict] = {}
        for a in self.agentenlaeufe:
            if a.name is None:
                continue
            zeile = je.setdefault(a.name, {"name": a.name, "knoten": a.zuordnung.knoten, "laeufe": 0, "dauer_s": None,
                                           "tokens": None, "tokens_unbekannt": 0})
            zeile["laeufe"] += 1
            if not a.geendet:
                continue
            dauer, _, _ = self._dauer_agent(a)
            if dauer is not None:
                zeile["dauer_s"] = (zeile["dauer_s"] or 0) + dauer
            if a.tokens is None:
                zeile["tokens_unbekannt"] += 1
            else:
                zeile["tokens"] = (zeile["tokens"] or 0) + a.tokens
        agenten = sorted(je.values(), key=lambda z: (z["dauer_s"] is None, -(z["dauer_s"] or 0), z["name"]))
        tokens, unbekannt = self.tokens()
        return {"agenten": agenten, "gesamt": {"dauer_s": dauer_s, "tokens": tokens, "tokens_unbekannt": unbekannt,
                                               "bisher": not self.abgeschlossen}}

    def tokens(self) -> tuple[int | None, int]:
        geendet = [a for a in self.agentenlaeufe if a.geendet]
        bekannt = [a.tokens for a in geendet if a.tokens is not None]
        return (sum(bekannt) if bekannt else None), len(geendet) - len(bekannt)


def kurzform(lauf: dict, zusammen: Zusammenfassung, jetzt: datetime, still_s: float = 120,
             ruhe_s: float = 1800) -> dict:
    stand = zusammen.stand
    gestartet = zeit_von(lauf.get("gestartet"))
    bis = stand.ende or jetzt
    return {"id": lauf.get("id"), "skill": lauf.get("skill"), "argumente": lauf.get("argumente", ""),
            "arbeitsordner": lauf.get("arbeitsordner"), "gestartet": lauf.get("gestartet"),
            "ende": zeit_text(stand.ende) if stand.ende is not None else None,
            "dauer_s": _sekunden(gestartet, bis) if gestartet is not None else None,
            "zustand": stand.zustand, "grund": stand.grund, "wartet": stand.wartet,
            "still": still(lauf, zusammen.zuletzt, stand, jetzt, still_s, ruhe_s), "tokens": zusammen.tokens,
            "tokens_unbekannt": zusammen.tokens_unbekannt, "agenten": zusammen.agenten,
            "plattform": lauf.get("plattform")}


def sicht(lauf: dict, bauplan: dict, ereignisse: list[dict], marken: Marken, jetzt: datetime, kit_id: str,
          still_s: float = 120, ruhe_s: float = 1800) -> dict:
    s = Sicht(lauf, bauplan, ereignisse, marken, jetzt, kit_id)
    kurz = kurzform(lauf, fasse_zusammen(ereignisse, marken), jetzt, still_s, ruhe_s)
    return {"lauf": kurz, "schritte": s.schritte(), "zeitleiste": s.zeitleiste(), "kosten": s.kosten(kurz["dauer_s"])}
