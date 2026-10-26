---
name: app
description: Legt das Programm Atlas Werkstatt an, auf macOS in ~/Applications (Dock, Launchpad, Spotlight), auf Windows im Startmenü und mit desktop auch auf dem Desktop. Ein Klick darauf öffnet ein eigenes Fenster, in dem man ein neues Kit anlegt oder ein vorhandenes in der Werkstatt öffnet, ohne Befehle zu tippen. Nutze es, wenn jemand die Werkstatt wie ein normales Programm starten will, und nach einem Update von atlas-kit, damit das Programm wieder aktuell ist.
argument-hint: "[desktop]"
---

Führ genau diesen Befehl aus, mit einem Timeout von 120000 ms:

```
CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" sh "${CLAUDE_PLUGIN_ROOT}/bin/atlas-kit" app '$ARGUMENTS'
```

Das Argument steht in einfachen Anführungszeichen und geht unverändert als ein einziges Shell-Argument weiter. Ist es leer, bleibt es `''`. Ersetze darin nur jedes einfache Anführungszeichen `'` durch `'\''`, sonst nichts. Will der Nutzer das Programm auf Windows zusätzlich auf dem Desktop, ist das Argument `desktop`.

Gib danach die Ausgabe des Befehls wörtlich wieder, Zeile für Zeile, ohne etwas wegzulassen oder zu ergänzen. Öffne das Programm nicht selbst und stell keine Rückfragen. Endet der Befehl mit einer Meldung, ist diese Meldung die ganze Antwort.
