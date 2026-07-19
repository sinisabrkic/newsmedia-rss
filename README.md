# newsmedia.report RSS Feed

Automatischer RSS-2.0-Teaser-Feed für [newsmedia.report](https://www.newsmedia.report/).

## Funktionen

- erkennt Webador-Artikel anhand ihrer eindeutigen nummerierten URL
- verwendet Sitemap und Startseite als voneinander unabhängige Fundstellen
- liest Titel, Veröffentlichungszeit, Vorspann, Autor, Rubrik und Titelbild
- erzeugt einen RSS-2.0-Feed mit Atom-, Media-RSS-, Dublin-Core- und Content-Modul
- veröffentlicht maximal 50 aktuelle Beiträge
- aktualisiert sich alle zehn Minuten über GitHub Actions
- speichert ältere Metadaten zwischen, damit Webador nicht bei jedem Lauf vollständig abgefragt wird
- schreibt nur dann einen neuen Commit, wenn sich Feed-Inhalte tatsächlich ändern

## Repository

`https://github.com/sinisabrkic/newsmedia-rss`

## Feed-Adresse

`https://rss.newsmedia.report/rss.xml`

## 1. Dateien hochladen

Den Inhalt dieses Ordners in das Stammverzeichnis des Repositorys hochladen. Die Datei muss anschließend genau hier sichtbar sein:

`.github/workflows/update-rss.yml`

Versteckte Ordner wie `.github` und Dateien wie `.nojekyll` müssen mit hochgeladen werden.

## 2. Ersten Testlauf starten

Im Repository:

1. `Actions` öffnen.
2. Links `Update RSS Feed` auswählen.
3. `Run workflow` anklicken.
4. Den Lauf abwarten und öffnen.
5. Alle Schritte müssen ein grünes Häkchen anzeigen.

Nach dem ersten erfolgreichen Lauf enthält `rss.xml` die aktuellen Artikel und `feed_state.json` den Zwischenspeicher.

## 3. Schreibrechte prüfen

Falls der Schritt `git push` mit einem Berechtigungsfehler endet:

1. `Settings` öffnen.
2. `Actions` > `General` öffnen.
3. Unter `Workflow permissions` die Option `Read and write permissions` wählen.
4. Speichern und den Workflow erneut starten.

## 4. GitHub Pages einschalten

Unter `Settings` > `Pages`:

- Source: `Deploy from a branch`
- Branch: `main`
- Folder: `/ (root)`

Danach `Save` anklicken.

## 5. Eigene Subdomain eintragen

Unter `Settings` > `Pages` bei `Custom domain` eintragen:

`rss.newsmedia.report`

Dann speichern.

## 6. DNS bei easyname

Nur diesen neuen Eintrag anlegen:

- Typ: `CNAME`
- Name/Host: `rss`
- Ziel/Wert: `sinisabrkic.github.io`
- TTL: Standard

Der Zielwert darf den Repository-Namen nicht enthalten.

Bestehende Einträge für `@`, `www`, E-Mail oder Webador bleiben unverändert.

## 7. HTTPS einschalten

Sobald GitHub die DNS-Prüfung abgeschlossen hat, unter `Settings` > `Pages` die Option `Enforce HTTPS` aktivieren.

## 8. Feed in Webador bekannt machen

In Webador unter `Einstellungen` > `Erweitert` > `Benutzerdefinierten HTML-Code hinzufügen` die Position `Head` wählen und den Inhalt von `WEBADOR-HEAD.html` einfügen.

## Anpassungen

Die wichtigsten Werte stehen in `config.json`:

- `max_items`: Anzahl der Feed-Beiträge
- `candidate_limit`: Zahl der untersuchten aktuellen Artikeladressen
- `refresh_newest`: Zahl der Beiträge, die bei jedem Lauf neu geprüft werden
- `feed_title`, `feed_description`, `copyright`

Das Zeitintervall steht in `.github/workflows/update-rss.yml`.

## Fehlerdiagnose

- `No Webador article URLs were discovered`: Sitemap oder Seitenstruktur war vorübergehend nicht erreichbar.
- `No publication date found`: Webador hat die Datumsdarstellung eines Artikels geändert.
- `Permission denied` beim Push: Schreibrechte für GitHub Actions aktivieren.
- `DNS check unsuccessful`: easyname-CNAME kontrollieren und widersprüchliche Einträge für `rss` entfernen.

Bei einem Fehler beendet sich der Generator, ohne eine vorhandene gültige `rss.xml` durch eine leere Datei zu ersetzen.
