#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 5: BAZA DANYCH (v2)
Zapisz jako database.py w folderze stock-scanner

Historia zmian:
    v2.0 — usunięto price_snapshots, monitoring przez UW
    v2.1 — fix update_outcomes: obsługa nieznanych tickerów
           - "No item with that key" → debug zamiast warning
           - ticker nieznany → oznaczany close_reason=ticker_not_found
"""

import sqlite3
import json
from datetime import datetime, timedelta
from config import logger, CONFIG, now_chicago


# ==================== INICJALIZACJA BAZY ====================

def get_connection():
    """Zwraca połączenie z bazą danych"""
    conn = sqlite3.connect(CONFIG['db_path'])
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """
    Tworzy tabele jeśli nie istnieją.
    Bezpieczne do wywołania wielokrotnie.
    """
    conn = get_connection()
    try:
        c = conn.cursor()

        # Tabela sygnałów
        c.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT NOT NULL,
                timestamp       TEXT NOT NULL,
                verdict         TEXT NOT NULL,
                confidence      TEXT,
                justification   TEXT,
                risk            TEXT,
                price           REAL,
                volume          INTEGER,
                volume_ratio    REAL,
                change_pct      REAL,
                score           INTEGER,
                reasons         TEXT,
                raw_response    TEXT,
                demo_mode       INTEGER DEFAULT 0,

                -- Automatyczne wyniki (wypełniane później)
                outcome_1h      REAL,
                outcome_4h      REAL,
                outcome_24h     REAL,
                outcome_1h_at   TEXT,
                outcome_4h_at   TEXT,
                outcome_24h_at  TEXT,

                -- Status monitorowania
                monitoring      INTEGER DEFAULT 0,
                monitoring_end  TEXT,
                closed          INTEGER DEFAULT 0,
                close_reason    TEXT
            )
        ''')

        # Tabela triggerów re-analizy
        c.execute('''
            CREATE TABLE IF NOT EXISTS retriggers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id   INTEGER NOT NULL,
                ticker      TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                trigger     TEXT NOT NULL,
                old_verdict TEXT,
                new_verdict TEXT,
                details     TEXT,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )
        ''')

        conn.commit()
        logger.info("Baza danych zainicjowana (v2 — bez price_snapshots)")

    finally:
        conn.close()


# ==================== ZAPIS SYGNAŁÓW ====================

def save_signal(result, ticker_data):
    """
    Zapisuje sygnał Claude'a do bazy.
    Aktywuje monitorowanie dla sygnałów BUY.
    """
    conn = get_connection()
    try:
        c = conn.cursor()

        verdict    = result.get('verdict', 'WATCH')
        monitoring = 1 if verdict == 'BUY' else 0

        monitoring_end = None
        if monitoring:
            end_time = now_chicago() + timedelta(hours=2)
            monitoring_end = end_time.isoformat()

        c.execute('''
            INSERT INTO signals (
                ticker, timestamp, verdict, confidence,
                justification, risk, price, volume,
                volume_ratio, change_pct, score, reasons,
                raw_response, demo_mode, monitoring, monitoring_end
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            result.get('ticker'),
            result.get('timestamp', now_chicago().isoformat()),
            verdict,
            result.get('confidence'),
            result.get('justification'),
            result.get('risk'),
            ticker_data.get('price', 0),
            ticker_data.get('volume', 0),
            ticker_data.get('volume_ratio', 1.0),
            ticker_data.get('change_pct', 0),
            ticker_data.get('score', 0),
            json.dumps(ticker_data.get('reasons', [])),
            result.get('raw_response', ''),
            1 if result.get('demo_mode') else 0,
            monitoring,
            monitoring_end,
        ))

        signal_id = c.lastrowid
        conn.commit()

        if monitoring:
            logger.info(f"DB: BUY {result.get('ticker')} (id={signal_id}) "
                        f"— monitoring do {monitoring_end[:16]}")
        else:
            logger.info(f"DB: {verdict} {result.get('ticker')} (id={signal_id})")

        return signal_id

    finally:
        conn.close()


# ==================== HISTORIA SYGNAŁÓW ====================

def get_signal_history(ticker, limit=5):
    """
    Zwraca ostatnie N sygnałów dla tickera.
    Używane jako kontekst dla Claude'a.
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute('''
            SELECT ticker, timestamp, verdict, confidence,
                   price, outcome_1h, outcome_4h, outcome_24h
            FROM signals
            WHERE ticker = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (ticker, limit))

        rows = c.fetchall()
        history = []

        for row in rows:
            try:
                dt = datetime.fromisoformat(row['timestamp'])
                date_str = dt.strftime('%Y-%m-%d %H:%M')
            except Exception:
                date_str = row['timestamp'][:16]

            history.append({
                'ticker':      row['ticker'],
                'date':        date_str,
                'verdict':     row['verdict'],
                'confidence':  row['confidence'],
                'price':       row['price'],
                'outcome_1h':  f"{row['outcome_1h']:+.1f}%" if row['outcome_1h'] else None,
                'outcome_4h':  f"{row['outcome_4h']:+.1f}%" if row['outcome_4h'] else None,
                'outcome_24h': f"{row['outcome_24h']:+.1f}%" if row['outcome_24h'] else None,
            })

        return history

    finally:
        conn.close()


# ==================== AUTOMATYCZNE WYNIKI ====================

def update_outcomes(polygon_api):
    """
    Sprawdza i zapisuje wyniki sygnałów po 1h, 4h, 24h.
    Wywołuj co cykl główny (5 min).
    """
    conn = get_connection()
    updated = 0

    try:
        c = conn.cursor()
        now = now_chicago()

        c.execute('''
            SELECT id, ticker, timestamp, price, verdict
            FROM signals
            WHERE (outcome_1h IS NULL OR outcome_4h IS NULL OR outcome_24h IS NULL)
            AND timestamp > datetime('now', '-2 days')
            ORDER BY timestamp DESC
        ''')

        rows = c.fetchall()

        for row in rows:
            try:
                signal_time = datetime.fromisoformat(row['timestamp'])
                elapsed_h   = (now - signal_time.replace(tzinfo=now.tzinfo)).total_seconds() / 3600
                entry_price = row['price']
                ticker      = row['ticker']

                if entry_price <= 0:
                    continue

                try:
                    current_data  = polygon_api.get_ticker_details(ticker)
                    current_price = float(current_data.get('price', 0) or 0)
                except Exception:
                    current_price = 0

                if current_price <= 0:
                    # Ticker nieznany w Polygon/Alpaca — oznacz jako nieaktywny
                    c.execute(
                        "UPDATE signals SET close_reason = ? WHERE id = ?",
                        ("ticker_not_found", row['id'])
                    )
                    continue

                change_pct = ((current_price - entry_price) / entry_price) * 100
                updates    = {}

                if elapsed_h >= 1 and row['outcome_1h'] is None:
                    updates['outcome_1h']    = round(change_pct, 2)
                    updates['outcome_1h_at'] = now.isoformat()

                if elapsed_h >= 4 and row['outcome_4h'] is None:
                    updates['outcome_4h']    = round(change_pct, 2)
                    updates['outcome_4h_at'] = now.isoformat()

                if elapsed_h >= 24 and row['outcome_24h'] is None:
                    updates['outcome_24h']    = round(change_pct, 2)
                    updates['outcome_24h_at'] = now.isoformat()

                if updates:
                    set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
                    c.execute(
                        f"UPDATE signals SET {set_clause} WHERE id = ?",
                        list(updates.values()) + [row['id']]
                    )
                    updated += 1

            except Exception as e:
                err_str = str(e).lower()
                if 'no item with that key' in err_str or 'not found' in err_str:
                    logger.debug(f"Ticker nieznany w API: {row['ticker']} — pomijam")
                else:
                    logger.warning(f"Błąd update outcome dla {row['ticker']}: {e}")

        conn.commit()

    finally:
        conn.close()

    if updated:
        logger.info(f"DB: zaktualizowano {updated} wyników")
    return updated


# ==================== MONITORING AKTYWNYCH BUY ====================

def get_active_buy_signals():
    """
    Zwraca aktywne sygnały BUY które są w trakcie monitorowania.
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        now = now_chicago().isoformat()

        c.execute('''
            SELECT id, ticker, timestamp, price, volume, volume_ratio
            FROM signals
            WHERE verdict = 'BUY'
            AND monitoring = 1
            AND closed = 0
            AND monitoring_end > ?
            ORDER BY timestamp DESC
        ''', (now,))

        rows = c.fetchall()
        return [dict(row) for row in rows]

    finally:
        conn.close()


def check_retrigger_conditions(signal, uw_data):
    """
    Sprawdza czy dane z UW wyzwalają re-analizę przez Claude'a.

    Triggery oparte na UW (nie na Polygon):
    - Dark pool side zmienił się na SELL
    - Options flow zmienił się na bearish (call/put ratio < 0.7)
    - Brak aktywności UW (unusual = False i mały wolumen opcji)
    - Cena cofnęła się o 3%+ (z UW lub Polygon)
    - Cena wzrosła o 10%+ (take profit)

    uw_data — dict z MockUnusualWhales.get_options_flow() +
              dodatkowe pole 'dark_pool' z get_dark_pool_flow()
    """
    entry_price  = signal.get('price', 0)
    current_price = uw_data.get('price', entry_price)

    if entry_price <= 0:
        return None, None

    price_change = ((current_price - entry_price) / entry_price) * 100

    # Trigger 1: Take profit +10%
    if price_change >= 10.0:
        return 'TAKE_PROFIT', (
            f"Cena wzrosła o {price_change:.1f}% "
            f"od sygnału (${entry_price:.2f} → ${current_price:.2f})"
        )

    # Trigger 2: Cena cofa się o 3%+
    if price_change <= -3.0:
        return 'PRICE_REVERSAL', (
            f"Cena cofnęła się o {price_change:.1f}% "
            f"od sygnału (${entry_price:.2f} → ${current_price:.2f})"
        )

    # Trigger 3: Dark pool zmienił się na SELL
    dark_pool_side = uw_data.get('dark_pool_side', '')
    if dark_pool_side == 'SELL':
        dark_pool_size = uw_data.get('dark_pool_size', 0)
        return 'DARKPOOL_SELL', (
            f"Dark pool zmienił się na SELL "
            f"(${dark_pool_size:,})"
        )

    # Trigger 4: Options flow stał się bearish
    call_put_ratio = uw_data.get('call_put_ratio', 1.0)
    if call_put_ratio < 0.7:
        return 'OPTIONS_BEARISH', (
            f"Options flow bearish — "
            f"call/put ratio: {call_put_ratio:.2f}"
        )

    # Trigger 5: UW aktywność zniknęła (unusual = False i mały wolumen)
    unusual      = uw_data.get('unusual', False)
    call_volume  = uw_data.get('call_volume', 0)
    put_volume   = uw_data.get('put_volume', 0)
    total_volume = call_volume + put_volume

    if not unusual and total_volume < 1000:
        return 'UW_ACTIVITY_GONE', (
            f"UW aktywność zniknęła — "
            f"unusual: {unusual}, opcje: {total_volume}"
        )

    return None, None


def save_retrigger(signal_id, ticker, trigger, details,
                   old_verdict, new_verdict=None):
    """Zapisuje zdarzenie re-analizy do bazy"""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute('''
            INSERT INTO retriggers
            (signal_id, ticker, timestamp, trigger, old_verdict, new_verdict, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            signal_id,
            ticker,
            now_chicago().isoformat(),
            trigger,
            old_verdict,
            new_verdict,
            details,
        ))
        conn.commit()
    finally:
        conn.close()


def close_signal(signal_id, reason):
    """Zamyka monitorowanie sygnału"""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute('''
            UPDATE signals
            SET monitoring = 0, closed = 1, close_reason = ?
            WHERE id = ?
        ''', (reason, signal_id))
        conn.commit()
        logger.info(f"DB: zamknięto sygnał id={signal_id} — {reason}")
    finally:
        conn.close()


# ==================== STATYSTYKI ====================

def get_stats():
    """Zwraca statystyki systemu"""
    conn = get_connection()
    try:
        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM signals")
        total = c.fetchone()[0]

        c.execute("SELECT verdict, COUNT(*) FROM signals GROUP BY verdict")
        by_verdict = dict(c.fetchall())

        c.execute('''
            SELECT AVG(outcome_1h), AVG(outcome_4h), AVG(outcome_24h)
            FROM signals
            WHERE verdict = 'BUY'
            AND outcome_1h IS NOT NULL
        ''')
        row = c.fetchone()
        avg_outcomes = {
            '1h':  round(row[0], 2) if row[0] else None,
            '4h':  round(row[1], 2) if row[1] else None,
            '24h': round(row[2], 2) if row[2] else None,
        }

        c.execute('''
            SELECT COUNT(*) FROM signals
            WHERE monitoring = 1 AND closed = 0
        ''')
        active_monitoring = c.fetchone()[0]

        return {
            'total_signals':     total,
            'by_verdict':        by_verdict,
            'avg_outcomes_buy':  avg_outcomes,
            'active_monitoring': active_monitoring,
        }

    finally:
        conn.close()


# ==================== TEST ====================

if __name__ == "__main__":
    from mock_polygon import MockPolygon, MockUnusualWhales, MockFinnhub
    from pre_filter import get_top_tickers
    from claude_analyst import ClaudeAnalyst

    print("\n" + "="*50)
    print("  TEST: Baza danych SQLite v2 (UW monitoring)")
    print("="*50)

    init_db()
    print("✅ Baza danych zainicjowana")

    polygon = MockPolygon()
    uw      = MockUnusualWhales()
    fh      = MockFinnhub()

    universe       = polygon.get_universe()
    dark_pool_flow = uw.get_dark_pool_flow()

    finnhub_cache = {}
    for t in universe:
        ticker        = t['ticker']
        t['earnings'] = fh.get_earnings_calendar(ticker)
        t['insider']  = fh.get_insider_transactions(ticker)
        finnhub_cache[ticker] = {
            'earnings': t['earnings'],
            'insider':  t['insider'],
        }

    top5 = get_top_tickers(
        universe,
        dark_pool_flow=dark_pool_flow,
        finnhub_cache=finnhub_cache,
        top_n=5
    )

    analyst = ClaudeAnalyst()
    results = analyst.analyze_batch(top5, polygon_api=polygon, uw_api=uw)

    print("\n✅ Zapisywanie sygnałów:")
    for result, ticker_data in zip(results, top5):
        signal_id = save_signal(result, ticker_data)
        icon = ('🟢' if result['verdict'] == 'BUY'
                else '🟡' if result['verdict'] == 'WATCH'
                else '🔴')
        print(f"  {icon} {result['ticker']} — {result['verdict']} "
              f"(id={signal_id})")

    print("\n✅ Aktywne BUY do monitorowania:")
    active = get_active_buy_signals()
    if active:
        for sig in active:
            print(f"  {sig['ticker']} — BUY @ ${sig['price']:.2f}")

        print("\n✅ Test triggerów UW:")
        for sig in active[:2]:
            # Symuluj bearish UW data
            fake_uw = {
                'price':          sig['price'] * 0.96,  # cena -4%
                'call_volume':    200,
                'put_volume':     800,
                'call_put_ratio': 0.25,   # bardzo bearish
                'unusual':        False,
                'dark_pool_side': 'SELL',
                'dark_pool_size': 750_000,
            }
            trigger, details = check_retrigger_conditions(sig, fake_uw)
            if trigger:
                print(f"  ⚡ {sig['ticker']}: [{trigger}] — {details}")
                save_retrigger(sig['id'], sig['ticker'], trigger,
                               details, 'BUY')
            else:
                print(f"  ✓ {sig['ticker']}: brak triggera")
    else:
        print("  Brak aktywnych BUY (losowe dane)")

    print("\n✅ Statystyki:")
    stats = get_stats()
    print(f"  Sygnałów: {stats['total_signals']}")
    print(f"  Werdykty: {stats['by_verdict']}")
    print(f"  Monitoring: {stats['active_monitoring']}")

    print("\n" + "="*50)
    print("  Plik 5 v2 gotowy ✅")
    print("="*50)
