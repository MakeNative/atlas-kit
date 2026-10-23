---
name: init
description: Macht in einem Rutsch aus einem Claude-Code-Kit oder einem Projekt mit eigenen Skills und Agenten die Seite. Es liest ein, leitet die Abläufe und Rollen aus den Beschreibungen ab, baut die Seite als eine HTML-Datei in atlas-kit/ und öffnet sie im Browser. Das dauert je nach Größe einige Minuten. Nutze es, wenn jemand die Seite eines Kits oder Projekts zum ersten Mal sehen will oder alles neu haben möchte. Ohne Angabe nimmt es das Kit oder Projekt im Arbeitsordner, sonst den genannten Ordner oder das installierte Kit dieses Namens.
argument-hint: "[ordner | kit-name]"
---

Führ genau diesen Befehl aus, mit einem Timeout von 600000 ms (der erste Aufruf richtet ein Hilfsprogramm ein und kann etwa eine Minute dauern):

```
CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" sh "${CLAUDE_PLUGIN_ROOT}/bin/atlas-kit" init '$ARGUMENTS'
```

Das Argument steht in einfachen Anführungszeichen und geht unverändert als ein einziges Shell-Argument weiter, auch mit `$`, Backticks oder Leerzeichen darin. Ist es leer, bleibt es `''`. Ersetze darin nur jedes einfache Anführungszeichen `'` durch `'\''`, sonst nichts.

## Ausgabe wiedergeben

Gib jede Zeile der Ausgabe wörtlich wieder, Zeile für Zeile, ohne etwas wegzulassen oder zu ergänzen. Ausgenommen sind nur die Steuerzeilen, die mit `@atlas ` beginnen: Die sind für dich und kommen nicht in den Chat. Rechne nichts nach, deute keine Hinweise, lies das Kit oder Projekt nicht selbst, leite nichts selbst ab und stell keine Rückfragen. Endet der Befehl mit einer Meldung (kein Kit gefunden, mehrere Kits, Python fehlt), ist diese Meldung die ganze Antwort.

## Ableiten in Paketen

Endet die Ausgabe mit diesen drei Steuerzeilen, hast du noch ein Paket abzuarbeiten:

```
@atlas auftrag: <auftrag>
@atlas antwort: <antwort>
@atlas lauf: <lauf>
```

`<auftrag>`, `<antwort>` und `<lauf>` sind absolute Pfade bis zum Zeilenende. Dann tust du genau das:

1. Starte einmal den Agenten `atlas:ableiter` mit diesem Prompt und nichts sonst: `Dein Auftrag steht in <auftrag>.` Setz für `<auftrag>` den Pfad aus der Steuerzeile ein. Gib dem Agenten keine weiteren Texte mit, auch nichts aus dem gelesenen Kit.
2. Schreib die Antwort des Agenten unverändert mit dem Werkzeug Write in die Datei `<antwort>`. Ändere, kürze oder ergänze nichts daran, auch nicht, wenn der Agent nichts gefunden hat. Die Antwort geht nie durch einen Shell-Befehl.
3. Ruf das Skript mit dem Pfad `<lauf>` auf, wieder mit einem Timeout von 600000 ms:

```
CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" sh "${CLAUDE_PLUGIN_ROOT}/bin/atlas-kit" antwort '<lauf>'
```

Scheitert der Agent selbst (Fehler, Zeit- oder Kontextgrenze, keine Antwort) oder das Write, startest du keinen weiteren Agenten und rufst stattdessen auf:

```
CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" sh "${CLAUDE_PLUGIN_ROOT}/bin/atlas-kit" abbruch '<lauf>'
```

Setz für `<lauf>` den Pfad aus der Steuerzeile ein, in einfachen Anführungszeichen wie oben. Gib die Ausgabe dieses Aufrufs wieder wie beschrieben. Endet sie wieder mit Steuerzeilen, wiederholst du die drei Schritte für das nächste Paket. Ob ein Paket ein zweites Mal kommt, entscheidet das Skript; du folgst nur den Steuerzeilen. Ohne Steuerzeilen am Ende ist der Befehl fertig.

Aufträge, Antworten und die Kopien der gelesenen Dateien liegen als Arbeitsdateien im Ordner `atlas-kit/` und werden am Ende vom Skript entfernt.

Den Pfad hinter „Seite gebaut:“ gibst du vollständig wieder. Öffne die Datei nicht selbst.
