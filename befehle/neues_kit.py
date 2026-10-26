"""bin/atlas-kit neu '<name> [ordner]' (specs/werkstatt.yaml WS-NK1 bis WS-NK4).

Legt das Kit mit kit_anlegen an und startet die Werkstatt dort mit dem Ziel „Neue Pipeline“ (werkstatt.starte), so als
wäre das neue Kit der Arbeitsordner: atlas-kit/ liegt im neuen Kit. Die Ausgabe endet wie bei /atlas:werkstatt mit der
Steuerzeile der Werkstatt-Sitzung. Nur Standardbibliothek.
"""
from __future__ import annotations

from pathlib import Path

import ergebnis
import finden
import kit_anlegen
import texte
import werkstatt


def name_und_ordner(roh: str) -> tuple[str, str]:
    """Erstes Wort = Name, Rest = Ordner; Anführungszeichen erst danach je Teil weg, auch um Name und Ordner einzeln
    ("mein-kit" "~/Meine Kits") oder um beides zusammen (r21-10)."""
    text = roh.strip()
    if len(text) >= 2 and text[0] in "\"'" and text[0] == text[-1] and text.count(text[0]) == 2:
        text = text[1:-1].strip()
    if text[:1] in ("\"", "'") and text.find(text[0], 1) > 0:
        ende = text.find(text[0], 1)
        name, rest = text[1:ende].strip(), text[ende + 1:]
    else:
        teile = text.split(None, 1)
        name, rest = (teile[0], teile[1] if len(teile) > 1 else "") if teile else ("", "")
    return name, finden.ohne_anfuehrungszeichen(rest)


def fuehre_aus(aufruf, sage, steuere) -> int:
    name, ordner_angabe = name_und_ordner(" ".join([aufruf.argument, *aufruf.weitere]))
    if not name:
        sage(texte.NEU_OHNE_NAME)
        return 1
    if not kit_anlegen.name_gueltig(name):
        sage(texte.neu_ungueltig(name))
        return 1
    ordner = finden.als_pfad(ordner_angabe, aufruf.arbeitsordner, aufruf.home) if ordner_angabe else aufruf.arbeitsordner
    try:
        angelegt = kit_anlegen.lege_an(ordner, name)
    except kit_anlegen.OrdnerFehlt:
        sage(texte.neu_ordner_fehlt(finden.anzeige(ordner_angabe, ordner, aufruf.home)))
        return 1
    except kit_anlegen.SchonDa:
        sage(texte.neu_schon_da(aufruf.tilde(ordner / name)))
        return 1
    except kit_anlegen.NichtAnlegbar as fehler:
        raise ergebnis.NichtSchreibbar(fehler.ordner)
    ziel = aufruf.tilde(angelegt.ordner)
    sage(texte.neu_angelegt(name, ziel))
    if angelegt.git == kit_anlegen.GIT_COMMIT:
        sage(texte.NEU_GIT_OK)
    elif angelegt.git == kit_anlegen.GIT_FEHLT:
        sage(texte.NEU_GIT_FEHLT)
    else:
        sage(texte.neu_ohne_commit(angelegt.grund))
    # Die Werkstatt läuft für das neue Kit, als wäre es der Arbeitsordner (WS-NK4).
    aufruf.arbeitsordner = Path(angelegt.ordner)
    kit = finden.pruefe_ordner(angelegt.ordner, aufruf.home, "/atlas:werkstatt")
    knopf = angelegt.knopf.name if angelegt.knopf is not None else None
    return werkstatt.starte(aufruf, sage, steuere, kit=kit, ziel=werkstatt.ZIEL_NEUE_PIPELINE,
                            vor_dem_warten=[texte.wieder_oeffnen(ziel, knopf)])
