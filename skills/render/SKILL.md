---
name: render
description: Baut aus dem Bauplan eines Claude-Code-Kits oder eines Projekts die Seite als eine einzelne HTML-Datei in atlas-kit/ und öffnet sie im Browser. Nutze es, wenn jemand die Seite eines Kits sehen oder weitergeben will. Gibt es noch keinen passenden Bauplan, liest es das Kit zuerst selbst ein.
argument-hint: "[ordner | kit-name]"
---

Führ genau diesen Befehl aus, mit einem Timeout von 600000 ms (muss das Kit erst eingelesen werden, kann der erste Aufruf etwa eine Minute dauern):

```
CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" sh "${CLAUDE_PLUGIN_ROOT}/bin/atlas-kit" render '$ARGUMENTS'
```

Das Argument steht in einfachen Anführungszeichen und geht unverändert als ein einziges Shell-Argument weiter, auch mit `$`, Backticks oder Leerzeichen darin. Ist es leer, bleibt es `''`. Ersetze darin nur jedes einfache Anführungszeichen `'` durch `'\''`, sonst nichts.

Gib danach die Ausgabe des Befehls wörtlich wieder, Zeile für Zeile, ohne etwas wegzulassen oder zu ergänzen. Den Pfad hinter „Seite gebaut:“ gibst du vollständig wieder. Öffne die Datei nicht selbst, rechne nichts nach, deute keine Hinweise und stell keine Rückfragen. Endet der Befehl mit einer Meldung (kein Kit gefunden, mehrere Baupläne, unbrauchbarer Bauplan), ist diese Meldung die ganze Antwort.
