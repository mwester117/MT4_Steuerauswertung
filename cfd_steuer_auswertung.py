"""
CFD/Trading Steuer-Auswertung: MT4-HTML-Statement einlesen und in EUR umrechnen
=================================================================================

Struktur:
1. Einlesen der Trade-Daten UND der Performance-Fee-Abbuchungen aus dem
   MT4 "Save as Report" HTML-Statement
2. Abruf der EZB-Referenzkurse (USD/EUR) ueber die Frankfurter-API (v2, Provider: ECB)
3. Zusammenfuehren (Merge) + Berechnung der EUR-Werte
4. Export zweier CSV-Dateien in das Ausgabeverzeichnis:
   - summary_<jahr>.csv: aggregierte Steuerzahlen (Gewinn/Verlust/Fee/Netto)
   - audit_<jahr>.csv: nachvollziehbare Einzelaufstellung pro Trade/Gebuehr
     (fuer den Fall einer Nachfrage durch das Finanzamt)

Das MT4-HTML-Statement enthaelt vor der eigentlichen Trade-Tabelle
Meta-Zeilen (Account-Info, "Closed Transactions:"), die pandas beim
Einlesen als Spaltennamen 0,1,2... interpretiert. Die echte Kopfzeile
("Ticket", "Open Time", ...) wird daher separat gesucht und gesetzt.

Neben echten Trades (Type == buy/sell) enthaelt die Tabelle zwei Arten
von "balance"-Zeilen:
  - Ein-/Auszahlungen (z.B. "Deposit-Wire...") - werden ignoriert
  - Performance-Fee-Abbuchungen des Trading-Bots, erkennbar am Text
    "ADJ - PF #<id>" (die Bot-Gebuehr wird nur bei monatlichem Gewinn
    erhoben und ist daher KEINE dem Werbungskostenabzugsverbot unter-
    liegende Position, sondern mindert direkt den Kapitalertrag)
Da sich der Text "ADJ - PF #<id>" beim HTML-Export ueber mehrere
Spalten (Size bis Swap) zieht, steht der eigentliche Gebuehrenbetrag
weiterhin in der Spalte "Profit" (als negativer Wert). Die abschliessende
"Closed P/L:"-Summenzeile wird ebenfalls herausgefiltert.

Aufruf:
    python cfd_steuer_auswertung.py --input statement.html --year 2025
    python cfd_steuer_auswertung.py --input statement.html --output ./output --year 2025
"""

import argparse
import pandas as pd
import requests
from pathlib import Path


# ---------------------------------------------------------------------------
# TEIL 1: Einlesen der Trade- und Performance-Fee-Daten aus dem HTML-Statement
# ---------------------------------------------------------------------------

def _read_statement_table(html_path: str) -> pd.DataFrame:
    """
    Liest die rohe Tabelle aus dem MT4-Statement und setzt die echte
    Kopfzeile (die Zeile, in der Spalte 0 den Wert "Ticket" hat).
    """
    # pandas liest die HTML-Tabelle zunaechst ohne "Verstaendnis" fuer die
    # MT4-Struktur ein: die ersten Zeilen (Account-Info, "Closed
    # Transactions:") landen dabei als normale Datenzeilen unter den
    # generischen Spaltennamen 0, 1, 2, ... Erst die Zeile, in der Spalte 0
    # den Text "Ticket" enthaelt, ist die tatsaechliche Kopfzeile.
    raw = pd.read_html(html_path)[0]

    header_matches = raw[raw[0] == "Ticket"]
    if header_matches.empty:
        raise ValueError("Konnte die Kopfzeile ('Ticket') im HTML-Statement nicht finden.")
    header_row_idx = header_matches.index[0]

    # Alles ab der gefundenen Kopfzeile wird zur eigentlichen Tabelle;
    # die Kopfzeile selbst wird als neue Spaltenbenennung uebernommen und
    # aus den Daten entfernt.
    new_header = raw.iloc[header_row_idx].tolist()
    df = raw.iloc[header_row_idx + 1:].copy()
    df.columns = new_header
    df = df.reset_index(drop=True)
    return df


def _to_number(series: pd.Series) -> pd.Series:
    """Entfernt Tausender-Leerzeichen (z.B. '1 234.56') vor der Umwandlung."""
    return pd.to_numeric(
        series.astype(str).str.replace(" ", "", regex=False),
        errors="coerce",
    )


def load_trades(html_path: str) -> pd.DataFrame:
    """
    Extrahiert die echten Trade-Zeilen (Type == buy/sell) aus dem
    MT4-Statement, inkl. aller Angaben, die fuer eine spaetere
    Nachvollziehbarkeit der Berechnung (Audit-Trail) benoetigt werden:
    Open/Close-Zeit, Size, Open-/Close-Preis, Symbol, Profit, Commission, Swap.
    """
    df = _read_statement_table(html_path)

    # MT4 nutzt "Price" doppelt (Open-/Close-Preis). Da ein rename()-Dict
    # denselben Key nur einmal halten kann, werden die Spaltennamen hier
    # positionsbasiert als Liste erzeugt statt ueber ein Mapping-Dict.
    # "price_seen" zaehlt mit, ob die erste ("Price" = Open-Preis) oder
    # zweite ("Price" = Close-Preis) Preis-Spalte gerade verarbeitet wird.
    new_cols = []
    price_seen = 0
    for col in df.columns:
        col_clean = str(col).strip()
        if col_clean == "Price":
            new_cols.append("open_price" if price_seen == 0 else "close_price")
            price_seen += 1
        elif col_clean == "Open Time":
            new_cols.append("open_time")
        elif col_clean == "Close Time":
            new_cols.append("close_time")
        elif col_clean == "Ticket":
            new_cols.append("trade_id")
        elif col_clean == "Type":
            new_cols.append("type")
        elif col_clean == "Size":
            new_cols.append("size")
        elif col_clean == "Item":
            new_cols.append("symbol")
        elif col_clean == "Commission":
            new_cols.append("commission")
        elif col_clean == "Swap":
            new_cols.append("swap")
        elif col_clean == "Profit":
            new_cols.append("profit")
        else:
            new_cols.append(col_clean.lower().replace(" ", "_").replace("/", "_"))
    df.columns = new_cols

    required_cols = {
        "trade_id", "open_time", "type", "size", "symbol",
        "open_price", "close_time", "close_price",
        "commission", "swap", "profit",
    }
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Fehlende Spalten im HTML-Statement: {missing}")

    # Nur echte Trades behalten, "balance"-Zeilen (Ein-/Auszahlungen,
    # Performance-Fee-Buchungen) werden hier bewusst ausgeschlossen und
    # spaeter separat in load_performance_fees() ausgewertet.
    df = df[df["type"].isin(["buy", "sell"])].copy()

    df["trade_id"] = _to_number(df["trade_id"]).astype(int)
    df["open_time"] = pd.to_datetime(df["open_time"])
    df["close_time"] = pd.to_datetime(df["close_time"])
    # date_close (ohne Uhrzeit) wird als Merge-Schluessel fuer die
    # taegliche Wechselkurs-Zuordnung in _convert_to_eur() benoetigt.
    df["date_close"] = df["close_time"].dt.date

    for col in ["size", "open_price", "close_price", "commission", "swap", "profit"]:
        df[col] = _to_number(df[col])

    return df[[
        "trade_id", "type", "symbol", "size",
        "open_time", "open_price", "close_time", "close_price", "date_close",
        "profit", "commission", "swap",
    ]]


def load_performance_fees(html_path: str) -> pd.DataFrame:
    """
    Extrahiert die monatlichen Performance-Fee-Abbuchungen des Trading-Bots
    aus dem MT4-Statement.

    Diese Zeilen haben Type == "balance" und der Text "ADJ - PF #<id>"
    steht (durch den HTML-Export bedingt) in mehreren Spalten zwischen
    Size und Swap. Der eigentliche Gebuehrenbetrag steht als negativer
    Wert in der Spalte "Profit". Da "Close Time" bei diesen Zeilen
    ebenfalls durch den Text ueberschrieben ist, wird stattdessen
    "Open Time" als Datum der Gebuehrenbuchung verwendet.
    """
    df = _read_statement_table(html_path)

    required_original_cols = {
        "Ticket": "trade_id",
        "Open Time": "booking_time",
        "Type": "type",
        "Item": "adj_text",
        "Profit": "fee_amount",
    }
    missing = set(required_original_cols) - set(df.columns)
    if missing:
        raise ValueError(f"Fehlende Spalten im HTML-Statement: {missing}")

    df = df[list(required_original_cols.keys())].rename(columns=required_original_cols)

    # Performance-Fee-Zeilen erkennen: Type muss "balance" sein UND die
    # Spalte "Item" (hier: "adj_text") muss den Text "ADJ - PF" enthalten.
    # Dieser Text stammt aus dem MT4-Export, bei dem die eigentliche
    # Buchungsbeschreibung "ADJ - PF #<id>" ueber mehrere Spalten (Size
    # bis Swap) verteilt abgelegt wird - hier reicht die "Item"-Spalte zur
    # eindeutigen Erkennung. Der Regex toleriert unterschiedliche
    # Leerzeichen um den Bindestrich (z.B. "ADJ-PF" oder "ADJ - PF").
    is_fee_row = (df["type"] == "balance") & (
        df["adj_text"].astype(str).str.contains(r"ADJ\s*-\s*PF", na=False)
    )
    fees = df[is_fee_row].copy()

    fees["trade_id"] = _to_number(fees["trade_id"]).astype(int)
    fees["booking_time"] = pd.to_datetime(fees["booking_time"])
    # "date_close" wird hier bewusst aus "Open Time" abgeleitet, NICHT aus
    # "Close Time": Bei Performance-Fee-Zeilen ist "Close Time" durch den
    # durchlaufenden Text "ADJ - PF #<id>" ueberschrieben und daher nicht
    # nutzbar. "Open Time" bleibt bei "balance"-Zeilen unveraendert und
    # entspricht dem tatsaechlichen Abbuchungszeitpunkt der Gebuehr.
    fees["date_close"] = fees["booking_time"].dt.date
    fees["fee_amount"] = _to_number(fees["fee_amount"])

    return fees[["trade_id", "booking_time", "date_close", "fee_amount"]]


def filter_by_year(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Filtert Zeilen mit einer 'date_close'-Spalte auf das angegebene Steuerjahr."""
    filtered = df[pd.to_datetime(df["date_close"]).dt.year == year].copy()
    return filtered


# ---------------------------------------------------------------------------
# TEIL 2: Abruf der EZB-Referenzkurse (USD/EUR) ueber die Frankfurter-API v2
# ---------------------------------------------------------------------------

def fetch_ecb_rates(year: int) -> pd.DataFrame:
    """
    Holt taegliche EZB-Referenzkurse USD->EUR fuer das gesamte Steuerjahr
    ueber die Frankfurter-API v2 (oeffentlicher Dienst, kein API-Key noetig).

    Mit 'base=USD, quotes=EUR' liefert die API den usd_eur-Kurs direkt im
    'rate'-Feld. Der Parameter 'providers=ECB' stellt sicher, dass explizit
    die offiziellen Referenzkurse der Europaeischen Zentralbank verwendet
    werden.
    """
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"

    url = "https://api.frankfurter.dev/v2/rates"
    params = {
        "base": "USD",
        "quotes": "EUR",
        "from": start_date,
        "to": end_date,
        "providers": "ECB",
    }

    # timeout=30: verhindert, dass das Skript bei einer haengenden
    # Netzwerkverbindung unbegrenzt lange wartet.
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    if not data:
        raise ValueError("Keine Wechselkursdaten von der API erhalten.")

    records = [
        {"date": pd.to_datetime(entry["date"]).date(), "usd_eur": entry["rate"]}
        for entry in data
    ]

    return pd.DataFrame(records).sort_values("date").reset_index(drop=True)


def fill_missing_rate_days(fx_df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Fuellt Wochenenden/Feiertage per Forward-Fill mit dem letzten bekannten Kurs."""
    # Die EZB veroeffentlicht nur an Bankarbeitstagen Referenzkurse.
    # Trades koennen aber an jedem Kalendertag (auch Wochenende, bei
    # manchen Instrumenten) geschlossen werden. Deshalb wird hier ein
    # vollstaendiger Tagesbereich erzeugt und fehlende Tage per
    # Forward-Fill (ffill) mit dem letzten bekannten Kurs aufgefuellt -
    # der uebliche Ansatz, da an Wochenenden/Feiertagen kein neuer
    # Referenzkurs entsteht.
    full_range = pd.date_range(f"{year}-01-01", f"{year}-12-31")
    fx_full = (
        fx_df.set_index(pd.to_datetime(fx_df["date"]))["usd_eur"]
        .reindex(full_range)
        .ffill()
        .reset_index()
        .rename(columns={"index": "date"})
    )
    fx_full["date"] = fx_full["date"].dt.date
    return fx_full


# ---------------------------------------------------------------------------
# TEIL 3: Zusammenfuehren, Berechnung, Audit-Trail und Export als CSV
# ---------------------------------------------------------------------------

def _convert_to_eur(df: pd.DataFrame, amount_col: str, fx_full: pd.DataFrame) -> pd.DataFrame:
    """Fuehrt einen DataFrame (mit 'date_close') mit den Wechselkursen zusammen
    und rechnet die angegebene USD-Spalte in EUR um (Ergebnis in 'gain_eur')."""
    # left-Join: jede Trade-/Fee-Zeile bekommt den Wechselkurs ihres
    # eigenen Schlusstages zugeordnet. Da fx_full (siehe
    # fill_missing_rate_days) bereits fuer JEDEN Kalendertag des Jahres
    # einen Kurs enthaelt, sollte usd_eur nach dem Merge nie leer sein -
    # der anschliessende Check faengt trotzdem ab, falls z.B. ein Trade
    # ausserhalb des abgefragten Jahres liegt.
    merged = df.merge(fx_full, left_on="date_close", right_on="date", how="left")
    if merged["usd_eur"].isna().any():
        missing_dates = merged.loc[merged["usd_eur"].isna(), "date_close"].unique()
        raise ValueError(f"Kein Wechselkurs gefunden fuer Daten: {missing_dates}")
    merged["gain_eur"] = merged[amount_col] * merged["usd_eur"]
    return merged


def build_audit_trail(
    trades_df: pd.DataFrame, fees_df: pd.DataFrame, fx_full: pd.DataFrame
) -> pd.DataFrame:
    """
    Erstellt eine vollstaendig nachvollziehbare Einzelaufstellung pro Trade
    und Performance-Fee-Buchung, die das Gesamtergebnis der Steuerauswertung
    Schritt fuer Schritt belegt (z.B. fuer eine Nachfrage des Finanzamts):
    Handelsdaten, verwendeter EZB-Tageskurs sowie die Zwischen- und
    Endergebnisse der Umrechnung.
    """
    trades_df = trades_df.copy()
    # "gross_usd" ist das tatsaechliche USD-Ergebnis eines Trades inkl.
    # aller vom Broker abgezogenen/gutgeschriebenen Nebenkosten
    # (Commission, Swap) - nicht nur der reine Kurs-Gewinn/Verlust.
    trades_df["gross_usd"] = trades_df["profit"] + trades_df["commission"] + trades_df["swap"]
    trades_merged = _convert_to_eur(trades_df, "gross_usd", fx_full)
    trades_merged["entry_type"] = "trade"
    # fee_amount existiert bei Trade-Zeilen nicht und wird nur als
    # Platzhalter-Spalte angelegt, damit trades_audit und fees_audit
    # (siehe unten) exakt dieselben Spalten haben und sich per concat()
    # sauber untereinander haengen lassen.
    trades_merged["fee_amount"] = pd.NA

    trades_audit_cols = [
        "entry_type", "trade_id", "type", "symbol", "size",
        "open_time", "open_price", "close_time", "close_price", "date_close",
        "profit", "commission", "swap", "fee_amount",
        "gross_usd", "usd_eur", "gain_eur",
    ]
    trades_audit = trades_merged.reindex(columns=trades_audit_cols)

    audit_parts = [trades_audit]
    if not fees_df.empty:
        fees_merged = _convert_to_eur(fees_df, "fee_amount", fx_full)
        fees_merged["entry_type"] = "performance_fee"
        # Bei Fee-Zeilen entspricht "gross_usd" (der Betrag vor der
        # EUR-Umrechnung) einfach der Fee selbst, da es hier keine
        # zusaetzlichen Nebenkosten wie Commission/Swap gibt.
        fees_merged["gross_usd"] = fees_merged["fee_amount"]
        # "booking_time" (aus load_performance_fees) wird in die
        # gemeinsame Spalte "open_time" umbenannt, damit fees_audit in
        # dasselbe Spaltenschema wie trades_audit passt.
        fees_merged = fees_merged.rename(columns={"booking_time": "open_time"})
        fees_audit = fees_merged.reindex(columns=trades_audit_cols)
        audit_parts.append(fees_audit)

    # sort=False verhindert, dass pandas beim concat() die Spalten
    # alphabetisch neu ordnet - die oben festgelegte Reihenfolge
    # (trades_audit_cols) bleibt dadurch erhalten.
    audit = pd.concat(audit_parts, ignore_index=True, sort=False)
    audit = audit.sort_values(["date_close", "trade_id"]).reset_index(drop=True)
    return audit


def build_summary(audit: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregiert den Audit-Trail zu den finalen Steuerzahlen.

    Spaltenreihenfolge und -benennung orientieren sich an der Relevanz
    fuer die Anlage KAP (auslaendische Kapitalertraege ohne inlaendischen
    Steuerabzug):
      - "netto_eur (Zeile 19)"  -> Auslaendische Kapitalertraege; bereits
                                    um Verlust-Trades UND Performance Fee
                                    verrechnet
      - "verlust_eur (Zeile 22)" -> In Zeile 19 enthaltene Verluste ohne
                                    Verluste aus Aktien; NUR Verlust-
                                    Trades, OHNE Performance Fee
      - "gewinn_eur" und "performance_fee_eur" sind reine Zwischenwerte
        zur Nachvollziehbarkeit und werden selbst NICHT in die Anlage
        KAP eingetragen.

    "verlust_eur" wird bewusst NUR aus den Trade-Zeilen (entry_type ==
    "trade") berechnet und nicht aus allen negativen "gain_eur"-Werten
    im Audit-Trail. Andernfalls wuerde die Performance Fee (die immer
    negativ ist, aber keine Verlust-Transaktion im Sinne der Anlage KAP
    Zeile 22 darstellt) faelschlich in den Verlustbetrag eingerechnet.
    """
    trade_rows = audit[audit["entry_type"] == "trade"]
    fee_rows = audit[audit["entry_type"] == "performance_fee"]

    return pd.DataFrame({
        "netto_eur (Zeile 19)": [audit["gain_eur"].sum()],
        "verlust_eur (Zeile 22)": [-trade_rows.loc[trade_rows["gain_eur"] < 0, "gain_eur"].sum()],
        "gewinn_eur": [trade_rows.loc[trade_rows["gain_eur"] > 0, "gain_eur"].sum()],
        "performance_fee_eur": [-fee_rows["gain_eur"].sum() if not fee_rows.empty else 0.0],
    })


def append_summary_rows(audit: pd.DataFrame) -> pd.DataFrame:
    """
    Haengt unterhalb der Einzelaufstellung zusaetzliche Zeilen mit den
    Zwischenergebnissen und dem Gesamtergebnis an, damit die Ableitung des
    Endergebnisses direkt in derselben Tabelle nachvollziehbar ist.

    Die Zwischensummen werden bewusst getrennt nach "trade" und
    "performance_fee" direkt aus dem Audit-Trail berechnet (statt aus
    build_summary() uebernommen), da Gewinn- und Verlust-Trades sowie die
    Performance Fee sonst ueber das gemeinsame Kriterium "gain_eur < 0"
    vermischt wuerden (die Fee ist immer negativ und wuerde faelschlich
    als Trade-Verlust erscheinen).

    Es wird eine Leerzeile als optische Trennung eingefuegt, gefolgt von
    je einer Zeile pro Zwischenschritt. Die Werte stehen dabei in der
    bereits vorhandenen Spalte "gain_eur", die Bezeichnung der jeweiligen
    Zeile in "entry_type".
    """
    trade_rows = audit[audit["entry_type"] == "trade"]
    fee_rows = audit[audit["entry_type"] == "performance_fee"]

    # Gewinn/Verlust ausschliesslich aus den Trade-Zeilen berechnen (NICHT
    # aus allen Zeilen mit gain_eur < 0), da sonst die Performance Fee
    # (die immer negativ ist) faelschlich als Trade-Verlust mitgezaehlt
    # wuerde.
    sum_gewinn_trades = trade_rows.loc[trade_rows["gain_eur"] > 0, "gain_eur"].sum()
    sum_verlust_trades = trade_rows.loc[trade_rows["gain_eur"] < 0, "gain_eur"].sum()
    sum_performance_fee = fee_rows["gain_eur"].sum()
    gesamtergebnis_netto = audit["gain_eur"].sum()

    row_labels = [
        ("summe_gewinn_trades_eur", sum_gewinn_trades),
        ("summe_verlust_trades_eur", sum_verlust_trades),
        ("summe_performance_fee_eur", sum_performance_fee),
        ("gesamtergebnis_netto_eur", gesamtergebnis_netto),
    ]

    # Leerzeile als optische Trennung zwischen den Einzelbuchungen und den
    # abschliessenden Summenzeilen. Alle Spalten werden dafuer explizit
    # auf NA gesetzt (nicht nur ausgelassen), damit die Zeile in jeder
    # Spalte des Audit-Trails leer erscheint statt fehlende Werte zu
    # erzeugen, die beim Einlesen zu Verschiebungen fuehren koennten.
    blank_row = pd.DataFrame([{col: pd.NA for col in audit.columns}])

    # Fuer jede Zwischensumme wird eine eigene Zeile erzeugt, die in allen
    # Spalten NA ist ausser "entry_type" (Bezeichnung der Kennzahl) und
    # "gain_eur" (der berechnete Betrag) - dadurch fuegt sich jede
    # Summenzeile nahtlos in die bestehende Tabellenstruktur ein.
    summary_rows = pd.DataFrame(
        [{**{col: pd.NA for col in audit.columns}, "entry_type": label, "gain_eur": value}
         for label, value in row_labels]
    )

    # sort=False haelt die Spaltenreihenfolge von "audit" bei; ignore_index
    # sorgt fuer einen fortlaufenden, neuen Zeilenindex ueber alle drei
    # Tabellenteile hinweg.
    return pd.concat([audit, blank_row, summary_rows], ignore_index=True, sort=False)


# ---------------------------------------------------------------------------
# Argument Parser & Hauptprogramm
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="CFD/Trading Steuer-Auswertung: MT4-HTML-Statement in EUR umrechnen (EZB-Kurse), inkl. Performance Fee."
    )
    parser.add_argument("--input", "-i", required=True, help="Pfad zum MT4-HTML-Statement (z.B. statement.html)")
    parser.add_argument(
        "--output", "-o", required=False, default=".",
        help="Ausgabeverzeichnis fuer summary_<jahr>.csv und audit_<jahr>.csv (Standard: aktuelles Verzeichnis)",
    )
    parser.add_argument("--year", "-y", required=True, type=int, help="Steuerjahr (z.B. 2025)")
    return parser.parse_args()


def main():
    args = parse_args()

    trades = filter_by_year(load_trades(args.input), args.year)
    fees = filter_by_year(load_performance_fees(args.input), args.year)

    if trades.empty and fees.empty:
        raise ValueError(f"Keine Trades oder Gebuehren im Steuerjahr {args.year} gefunden.")

    fx_rates = fetch_ecb_rates(args.year)
    fx_full = fill_missing_rate_days(fx_rates, args.year)

    # audit: Einzelaufstellung ohne Summenzeilen (wird intern u.a. fuer
    # build_summary() benoetigt). audit_with_totals: dieselbe Tabelle,
    # aber mit angehaengten Zwischen-/Gesamtergebnis-Zeilen - das ist die
    # Version, die tatsaechlich als audit_<jahr>.csv exportiert wird.
    audit = build_audit_trail(trades, fees, fx_full)
    summary = build_summary(audit)
    audit_with_totals = append_summary_rows(audit)

    # parents=True legt bei Bedarf auch Zwischenverzeichnisse an;
    # exist_ok=True verhindert einen Fehler, falls der Ordner schon existiert.
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_path = out_dir / f"summary_{args.year}.csv"
    audit_path = out_dir / f"audit_{args.year}.csv"

    def format_money(value):
        """Rundet einen Geldwert auf 2 Nachkommastellen und ersetzt den
        Punkt durch ein Komma. Leere/NaN-Werte bleiben leer."""
        if pd.isna(value):
            return ""
        return f"{value:.2f}".replace(".", ",")

    def format_money_unrounded(value):
        """Gibt einen Geldwert OHNE Rundung aus (nur der Punkt wird durch
        ein Komma ersetzt). Wird im Audit-Trail verwendet, damit die
        Zwischenschritte der Umrechnung (insbesondere usd_eur mit vielen
        Nachkommastellen) exakt nachvollziehbar bleiben und sich nicht
        durch Rundung minimal von der tatsaechlichen Rechnung unterscheiden."""
        if pd.isna(value):
            return ""
        return format(float(value), "f").replace(".", ",")

    # Fuer die Summary werden ALLE Spalten formatiert, da dort ausschliesslich
    # Geldwerte stehen. col.map() wendet format_money() auf jeden einzelnen
    # Wert der Spalte an (nicht auf die Spalte als Ganzes).
    summary_formatted = summary.apply(lambda col: col.map(format_money))
    summary_formatted.to_csv(summary_path, index=False)

    # Im Audit-Trail dagegen gibt es auch Nicht-Geld-Spalten (z.B. trade_id,
    # symbol, date_close) - deshalb wird die Formatierung hier gezielt nur
    # auf die in money_cols gelisteten Spalten angewendet, alle anderen
    # Spalten bleiben unveraendert. Im Unterschied zur Summary wird hier
    # bewusst NICHT gerundet (format_money_unrounded statt format_money),
    # damit die Berechnung der Endwerte (z.B. gain_eur = gross_usd * usd_eur)
    # jederzeit exakt nachgerechnet werden kann.
    money_cols = [
        "open_price", "close_price", "profit", "commission", "swap",
        "fee_amount", "gross_usd", "usd_eur", "gain_eur",
    ]
    audit_formatted = audit_with_totals.copy()
    for col in money_cols:
        audit_formatted[col] = audit_formatted[col].map(format_money_unrounded)
    audit_formatted.to_csv(audit_path, index=False)

    print(f"Steuerjahr {args.year}: Auswertung abgeschlossen.")
    print(summary.to_string(index=False))
    print(f"\nZusammenfassung: {summary_path}")
    print(f"Nachvollziehbare Einzelaufstellung (Audit-Trail): {audit_path}")


if __name__ == "__main__":
    main()
