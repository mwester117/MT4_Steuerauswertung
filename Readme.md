# CFD/Trading Steuer-Auswertung

Ein Python-Skript zur Auswertung von CFD-/Forex-Trades aus einem **MetaTrader 4 (MT4) HTML-Statement** für die deutsche Steuererklärung (Anlage KAP). Das Skript berechnet automatisch die in Fremdwährung (z. B. USD) erzielten Gewinne/Verluste, rechnet sie anhand der offiziellen **EZB-Referenzkurse** in EUR um und berücksichtigt dabei auch eine eventuell anfallende **Performance Fee** eines Trading-Bots korrekt getrennt von den eigentlichen Handelsergebnissen.

> ⚠️ **Haftungsausschluss:** Dieses Skript ersetzt keine steuerliche Beratung. Die Ergebnisse sollten vor Abgabe der Steuererklärung von einem Steuerberater geprüft werden.

## Voraussetzungen

- Python 3.9 oder neuer
- Ein MT4-Statement im HTML-Format (Export über "Save as Report" in MetaTrader 4)
- Internetzugang (für den Abruf der EZB-Wechselkurse)

## Installation

Die folgenden Python-Pakete werden benötigt:

```bash
pip install pandas requests lxml
```

| Paket | Zweck |
|---|---|
| `pandas` | Einlesen und Verarbeiten der HTML-Tabelle, CSV-Export |
| `requests` | Abruf der EZB-Wechselkurse über die Frankfurter-API |
| `lxml` | HTML-Parser-Backend für `pandas.read_html()` |

## Verwendung

```bash
python cfd_steuer_auswertung.py --input statement.html --year 2025
```

Optional kann ein Ausgabeverzeichnis angegeben werden (Standard ist das aktuelle Verzeichnis):

```bash
python cfd_steuer_auswertung.py --input statement.html --output ./output --year 2025
```

### Argumente

| Argument | Kurzform | Pflicht | Beschreibung |
|---|---|---|---|
| `--input` | `-i` | Ja | Pfad zum MT4-HTML-Statement |
| `--year` | `-y` | Ja | Steuerjahr, z. B. `2025` |
| `--output` | `-o` | Nein | Ausgabeverzeichnis für die CSV-Dateien (Standard: `.`) |

## Ausgabe

Das Skript erzeugt zwei CSV-Dateien im angegebenen Ausgabeverzeichnis:

### `summary_<jahr>.csv`

Eine einzeilige Zusammenfassung mit den vier zentralen Kennzahlen (Beträge gerundet auf 2 Nachkommastellen, mit Komma als Dezimaltrennzeichen):

| Spalte | Bedeutung |
|---|---|
| `netto_eur (Zeile 19)` | Nettoergebnis aller Trades und der Performance Fee |
| `verlust_eur (Zeile 22)` | Summe der Verlust-Trades **ohne** Performance Fee |
| `gewinn_eur` | Summe der Gewinn-Trades (Zwischenwert) |
| `performance_fee_eur` | Summe der abgebuchten Performance Fee (Zwischenwert) |

### `audit_<jahr>.csv`

Eine nachvollziehbare Einzelaufstellung pro Trade und Performance-Fee-Buchung (inkl. Handelsdaten, verwendetem EZB-Tageskurs sowie Zwischen- und Endergebnis der Umrechnung), gefolgt von den aufsummierten Kontrollzeilen (`summe_gewinn_trades_eur`, `summe_verlust_trades_eur`, `summe_performance_fee_eur`, `gesamtergebnis_netto_eur`). Diese Datei dient der Nachvollziehbarkeit im Fall einer Nachfrage durch das Finanzamt.

## Werte für die Steuererklärung (Anlage KAP)

Für die Anlage KAP (ausländische Kapitalerträge ohne inländischen Steuerabzug) werden aus der `summary_<jahr>.csv` genau **zwei Werte** benötigt:

| Wert aus der CSV | Zeile in der Anlage KAP | Bedeutung |
|---|---|---|
| `netto_eur (Zeile 19)` | **Zeile 19** | Ausländische Kapitalerträge – bereits um Verlust-Trades **und** die Performance Fee verrechnet |
| `verlust_eur (Zeile 22)` | **Zeile 22** | In Zeile 19 enthaltene Verluste ohne Verluste aus Aktien – **nur** die reinen Verlust-Trades, **ohne** die Performance Fee |

`gewinn_eur` und `performance_fee_eur` sind reine Zwischenwerte zur Nachvollziehbarkeit und werden **nicht** direkt in die Anlage KAP eingetragen.

> ℹ️ Die Performance Fee zählt bewusst **nicht** in `verlust_eur (Zeile 22)`, da sie keine Verlust-Transaktion im steuerlichen Sinne ist, sondern eine Kostenposition, die den Gewinn der profitablen Trades mindert.

## Funktionsweise (Kurzüberblick)

1. **Einlesen:** Das MT4-HTML-Statement wird geparst, echte Trades (`buy`/`sell`) und Performance-Fee-Abbuchungen (erkennbar am Buchungstext `ADJ - PF #<id>`) werden getrennt extrahiert.
2. **Wechselkurse:** Für das gewählte Steuerjahr werden die täglichen EZB-Referenzkurse USD→EUR über die öffentliche [Frankfurter-API](https://frankfurter.dev) abgerufen; fehlende Tage (Wochenenden/Feiertage) werden per Forward-Fill mit dem letzten bekannten Kurs aufgefüllt.
3. **Umrechnung:** Jeder Trade und jede Fee-Buchung wird anhand des Kurses am jeweiligen Schlusstag in EUR umgerechnet.
4. **Aggregation:** Gewinne, Verluste und die Performance Fee werden getrennt aufsummiert und als CSV exportiert.

## Lizenz

MIT
