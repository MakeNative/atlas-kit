---
name: scan
description: Liest ein Claude-Code-Kit oder ein Projekt mit eigenen Skills und Agenten ein und schreibt seinen Bauplan nach atlas-kit/ im Arbeitsordner. Nutze es, wenn jemand wissen will, welche Skills, Agenten und Abläufe ein Kit hat, oder als ersten Schritt vor /atlas:render. Ohne Angabe liest es das Kit oder Projekt im Arbeitsordner, sonst den genannten Ordner oder das installierte Kit dieses Namens.
argument-hint: "[ordner | kit-name]"
---

Führ genau diesen Befehl aus, mit einem Timeout von 600000 ms (der erste Aufruf richtet ein Hilfsprogramm ein und kann etwa eine Minute dauern):

```
CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" sh "${CLAUDE_PLUGIN_ROOT}/bin/atlas-kit" scan '$ARGUMENTS'
```

Das Argument steht in einfachen Anführungszeichen und geht unverändert als ein einziges Shell-Argument weiter, auch mit `$`, Backticks oder Leerzeichen darin. Ist es leer, bleibt es `''`. Ersetze darin nur jedes einfache Anführungszeichen `'` durch `'\''`, sonst nichts.

Gib danach die Ausgabe des Befehls wörtlich wieder, Zeile für Zeile, ohne etwas wegzulassen oder zu ergänzen. Rechne nichts nach, deute keine Hinweise, lies das Kit nicht selbst und stell keine Rückfragen. Endet der Befehl mit einer Meldung (kein Kit gefunden, mehrere Kits, Python fehlt), ist diese Meldung die ganze Antwort.
