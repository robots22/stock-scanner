#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 5: BAZA DANYCH (v2)
Zapisz jako database.py w folderze stock-scanner

Historia zmian:
    v2.0 — usunięto price_snapshots, monitoring przez UW
    v2.1 — fix update_outcomes: obsługa nieznanych tickerów
    v2.2 — dodano STOP_LOSS trigger, dynamiczny TP
"""

import sqlite3
import json
from datetime import datetime, timedelta
from config import logger, CONFIG, now_chicago


def get_connection():
    conn = sqlite3.connect(CONFIG['db_path'])
    conn.row_factory = sqlite3.Row
    return conn


def _last_signal_today(conn, ticker, verdict_filter=None):
    """
    Zwraca ostatni sygnal dla tickera z DZISIAJ (Chicago).
    Jesli verdict_filter podany — tylko sygnaly tego verdyktu.
    Zwraca (verdict, timestamp) lub (None, None) gdy brak.
    """
    today = now_chicago().strftime('%Y-%m-%d')
    c = conn.cursor()
    if verdict_filter:
        c.execute('''
            SELECT verdict, timestamp FROM signals
            WHERE ticker=? AND DATE(timestamp)=? AND verdict=?
            ORDER BY timestamp DESC LIMIT 1
        ''', (ticker, today, verdict_filter))
    else:
        c.execute('''
            SELECT verdict, timestamp FROM signals
            WHERE ticker=? AND DATE(timestamp)=?
            ORDER BY timestamp DESC LIMIT 1
        ''', (ticker, today))
    row = c.fetchone()
    if row:
        return row['verdict'], row['timestamp']
    return None, None


def _should_skip_signal(conn, ticker, verdict):
    """
    Reguly dedup na zapis (spojne z dedup_history.py):
      MOMENTUM: pierwszy per ticker/dzien
      BUY:      pierwszy per ticker/dzien
      WATCH:    zapisz gdy pierwszy dzis LUB gdy poprzedni verdict != WATCH
      AVOID:    zapisz gdy pierwszy dzis LUB gdy poprzedni verdict != AVOID

    Zwraca True gdy zapis nalezy POMINAC.
    """
    if verdict == 'MOMENTUM':
        prev_v, _ = _last_signal_today(conn, ticker, verdict_filter='MOMENTUM')
        return prev_v is not None

    if verdict == 'BUY':
        prev_v, _ = _last_signal_today(conn, ticker, verdict_filter='BUY')
        return prev_v is not None

    if verdict in ('WATCH', 'AVOID'):
        # Bierzemy POPRZEDNI ostatni sygnal (dowolny verdict)
        prev_v, _ = _last_signal_today(conn, ticker)
        if prev_v is None:
            return False  # pierwszy dzis — zapisz
        # Pomijamy gdy poprzedni ma taki sam verdict
        return prev_v == verdict

    return False


def init_db():
    conn = get_connection()
    try:
        c = conn.cursor()
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
                outcome_1h      REAL,
                outcome_4h      REAL,
                outcome_24h     REAL,
                outcome_1h_at   TEXT,
                outcome_4h_at   TEXT,
                outcome_24h_at  TEXT,
                stop_loss       REAL,
                take_profit     REAL,
                rr_ratio        REAL,
                risk_pct        REAL,
                reward_pct      REAL,
                sl_basis        TEXT,
                atr             REAL,
                monitoring      INTEGER DEFAULT 0,
                monitoring_end  TEXT,
                closed          INTEGER DEFAULT 0,
                close_reason    TEXT
            )
        ''')

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


def save_signal(result, ticker_data, polygon_api=None):
    conn = get_connection()
    try:
        c = conn.cursor()

        verdict    = result.get('verdict', 'WATCH')
        ticker     = result.get('ticker', '')

        # DEDUP na zapis — nie zapisuj duplikatow zgodnie z reguly
        if _should_skip_signal(conn, ticker, verdict):
            logger.debug(f"DB dedup: pomijam {verdict} {ticker} (duplikat tego samego dnia)")
            return None

        monitoring = 1 if verdict == 'BUY' else 0

        monitoring_end = None
        if monitoring:
            from config import is_market_open, CHICAGO_TZ
            n = now_chicago()
            # Monitoring do konca sesji lub max 2h
            market_close = n.replace(hour=15, minute=0, second=0, microsecond=0)
            two_hours    = n + timedelta(hours=2)
            if is_market_open() and market_close > n:
                # Podczas sesji: min(2h, koniec sesji)
                end_time = min(two_hours, market_close)
            else:
                # Po godzinach: monitoring tylko 30 minut
                end_time = n + timedelta(minutes=30)
            monitoring_end = end_time.isoformat()

        stop_loss = take_profit = rr_ratio = None
        risk_pct = reward_pct = sl_basis = atr = None

        if verdict == 'BUY' and polygon_api:
            try:
                entry_price = ticker_data.get('price', 0)
                vwap        = ticker_data.get('vwap', 0)
                lod         = ticker_data.get('low', 0)
                ticker      = result.get('ticker', '')

                if entry_price > 0:
                    sl_data     = polygon_api.calculate_stop_loss(ticker, entry_price, vwap=vwap, lod=lod)
                    stop_loss   = sl_data['stop_loss']
                    take_profit = sl_data['take_profit']
                    rr_ratio    = sl_data['rr_ratio']
                    risk_pct    = sl_data['risk_pct']
                    reward_pct  = sl_data['reward_pct']
                    sl_basis    = sl_data['basis']
                    atr         = sl_data['atr']
            except Exception as e:
                logger.warning(f"Stop-loss calc error: {e}")

        c.execute('''
            INSERT INTO signals (
                ticker, timestamp, verdict, confidence,
                justification, risk, price, volume,
                volume_ratio, change_pct, score, reasons,
                raw_response, demo_mode, monitoring, monitoring_end,
                stop_loss, take_profit, rr_ratio, risk_pct,
                reward_pct, sl_basis, atr
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?)
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
            monitoring, monitoring_end,
            stop_loss, take_profit, rr_ratio, risk_pct,
            reward_pct, sl_basis, atr,
        ))

        signal_id = c.lastrowid
        conn.commit()

        if monitoring:
            sl_str = f" | SL: ${stop_loss:.2f} | TP: ${take_profit:.2f}" if stop_loss else ""
            logger.info(f"DB: BUY {result.get('ticker')} (id={signal_id}) — monitoring do {monitoring_end[:16]}{sl_str}")
        else:
            logger.info(f"DB: {verdict} {result.get('ticker')} (id={signal_id})")

        return signal_id

    finally:
        conn.close()


def save_momentum_signal(ticker_data, trigger_name, trigger_desc):
    """
    Zapisuje sygnal z Trybu 2 (MomentumScanner) do bazy.
    Uzywa tej samej tabeli signals z verdict='MOMENTUM'.
    Bez monitoringu, bez stop_loss/take_profit (informacyjny).
    Sluzy do przyszlej analizy self-learning.

    DEDUP: tylko pierwszy alert per ticker/dzien.
    """
    conn = get_connection()
    try:
        ticker = ticker_data.get('ticker', '')

        # DEDUP: pomijamy jesli juz jest MOMENTUM dla tego tickera dzisiaj
        if _should_skip_signal(conn, ticker, 'MOMENTUM'):
            return None

        c = conn.cursor()
        c.execute('''
            INSERT INTO signals (
                ticker, timestamp, verdict, confidence,
                justification, risk, price, volume,
                volume_ratio, change_pct, score, reasons,
                raw_response, demo_mode, monitoring, monitoring_end,
                stop_loss, take_profit, rr_ratio, risk_pct,
                reward_pct, sl_basis, atr
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?)
        ''', (
            ticker_data.get('ticker'),
            now_chicago().isoformat(),
            'MOMENTUM',
            trigger_name,        # uzywamy pola confidence na trigger_name
            trigger_desc,        # justification = opis triggera
            None,
            ticker_data.get('price', 0),
            ticker_data.get('volume', 0),
            ticker_data.get('volume_ratio', 1.0),
            ticker_data.get('change_pct', 0),
            0,
            json.dumps([trigger_desc]),
            '',
            0,
            0, None,  # monitoring=0, brak monitoring_end
            None, None, None, None,
            None, None, None,
        ))
        signal_id = c.lastrowid
        conn.commit()
        return signal_id
    finally:
        conn.close()


def get_signal_history(ticker, limit=5):
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


def update_outcomes(polygon_api):
    conn = get_connection()
    updated = 0
    try:
        c = conn.cursor()
        now = now_chicago()

        c.execute('''
            SELECT id, ticker, timestamp, price, verdict
            FROM signals
            WHERE (outcome_1h IS NULL OR outcome_4h IS NULL OR outcome_24h IS NULL)
            AND timestamp > datetime('now', '-7 days')
            ORDER BY timestamp DESC
        ''')

        rows = c.fetchall()

        for row in rows:
            try:
                signal_time = datetime.fromisoformat(row['timestamp'])
                # Upewnij sie ze oba maja timezone
                if signal_time.tzinfo is None:
                    signal_time = signal_time.replace(tzinfo=now.tzinfo)
                else:
                    signal_time = signal_time.astimezone(now.tzinfo)
                elapsed_h = (now - signal_time).total_seconds() / 3600
                entry_price = row['price']
                ticker      = row['ticker']

                if entry_price <= 0:
                    continue

                ticker_upper = ticker.upper()
                if (ticker_upper.endswith('W') or ticker_upper.endswith('WW') or
                        ticker_upper.endswith('R') or ticker_upper.endswith('U') or
                        '.WS' in ticker_upper or '.RT' in ticker_upper):
                    continue

                current_data  = polygon_api.get_ticker_details(ticker)
                current_price = float(current_data.get('price', 0) or 0)

                # Fallback na prev_close gdy rynek zamkniety
                if current_price <= 0:
                    current_price = float(current_data.get('prev_close', 0) or 0)

                if current_price <= 0:
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


def get_active_buy_signals():
    conn = get_connection()
    try:
        c = conn.cursor()
        now = now_chicago().isoformat()
        c.execute('''
            SELECT id, ticker, timestamp, price, volume, volume_ratio,
                   stop_loss, take_profit
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
    entry_price   = signal.get('price', 0)
    current_price = uw_data.get('price', entry_price)

    if entry_price <= 0:
        return None, None

    price_change = ((current_price - entry_price) / entry_price) * 100

    # Trigger 0: STOP LOSS
    stop_loss = signal.get('stop_loss')
    if stop_loss and current_price <= stop_loss:
        return 'STOP_LOSS', (
            f"Stop-loss osiagniety ${current_price:.2f} "
            f"(SL: ${stop_loss:.2f}, entry: ${entry_price:.2f}, "
            f"{price_change:+.1f}%)"
        )

    # Trigger 1: Take profit
    take_profit = signal.get('take_profit')
    tp_hit = (take_profit and current_price >= take_profit) or \
             (not take_profit and price_change >= 10.0)
    if tp_hit:
        return 'TAKE_PROFIT', (
            f"Take profit osiagniety ${current_price:.2f} "
            f"(+{price_change:.1f}% od sygnalu ${entry_price:.2f})"
        )

    # Trigger 2: Price reversal -3%
    if price_change <= -3.0:
        return 'PRICE_REVERSAL', (
            f"Cena cofnela sie o {price_change:.1f}% "
            f"od sygnalu (${entry_price:.2f} -> ${current_price:.2f})"
        )

    # Trigger 3: Dark pool SELL
    dark_pool_side = uw_data.get('dark_pool_side', '')
    if dark_pool_side == 'SELL':
        dark_pool_size = uw_data.get('dark_pool_size', 0)
        return 'DARKPOOL_SELL', f"Dark pool zmienil sie na SELL (${dark_pool_size:,})"

    # Trigger 4: Options bearish
    call_put_ratio = uw_data.get('call_put_ratio', 1.0)
    if call_put_ratio < 0.7:
        return 'OPTIONS_BEARISH', f"Options flow bearish — call/put ratio: {call_put_ratio:.2f}"

    # Trigger 5: UW activity gone
    unusual      = uw_data.get('unusual', False)
    call_volume  = uw_data.get('call_volume', 0)
    put_volume   = uw_data.get('put_volume', 0)
    total_volume = call_volume + put_volume

    if not unusual and total_volume < 1000:
        return 'UW_ACTIVITY_GONE', f"UW aktywnosc zniknela — unusual: {unusual}, opcje: {total_volume}"

    # Trigger 6: Time-based exit — midday slow zone
    from config import now_chicago
    n = now_chicago()
    signal_time = signal.get('timestamp', '')
    if signal_time:
        try:
            from datetime import datetime
            st = datetime.fromisoformat(signal_time)
            if st.tzinfo is None:
                st = st.replace(tzinfo=n.tzinfo)
            else:
                st = st.astimezone(n.tzinfo)
            elapsed_min = (n - st).total_seconds() / 60

            # Po 90 minutach w midday (10:00-14:00) bez TP — exit
            midday_start = n.replace(hour=10, minute=0, second=0, microsecond=0)
            midday_end   = n.replace(hour=14, minute=0, second=0, microsecond=0)
            if (midday_start <= n <= midday_end and
                    elapsed_min >= 90 and
                    price_change < 3.0):  # nie osiagnal TP
                return 'TIME_EXIT', (
                    f"90 min w midday bez TP ({price_change:+.1f}%) — exit"
                )
        except Exception:
            pass

    return None, None


def save_retrigger(signal_id, ticker, trigger, details, old_verdict, new_verdict=None):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute('''
            INSERT INTO retriggers
            (signal_id, ticker, timestamp, trigger, old_verdict, new_verdict, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (signal_id, ticker, now_chicago().isoformat(), trigger, old_verdict, new_verdict, details))
        conn.commit()
    finally:
        conn.close()


def close_signal(signal_id, reason):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute('''
            UPDATE signals
            SET monitoring = 0, closed = 1, close_reason = ?
            WHERE id = ?
        ''', (reason, signal_id))
        conn.commit()
        logger.info(f"DB: zamknieto sygnal id={signal_id} — {reason}")
    finally:
        conn.close()


def get_stats():
    """Statystyki TYLKO z dzisiejszej daty (dla dashboard/Telegram)."""
    conn = get_connection()
    try:
        c = conn.cursor()
        today = now_chicago().strftime('%Y-%m-%d')

        c.execute("SELECT COUNT(*) FROM signals WHERE DATE(timestamp) = ?", (today,))
        total = c.fetchone()[0]

        c.execute(
            "SELECT verdict, COUNT(*) FROM signals WHERE DATE(timestamp) = ? GROUP BY verdict",
            (today,)
        )
        by_verdict = dict(c.fetchall())

        c.execute('''
            SELECT AVG(outcome_1h), AVG(outcome_4h), AVG(outcome_24h)
            FROM signals
            WHERE verdict = 'BUY' AND outcome_1h IS NOT NULL
            AND DATE(timestamp) = ?
        ''', (today,))
        row = c.fetchone()
        avg_outcomes = {
            '1h':  round(row[0], 2) if row[0] else None,
            '4h':  round(row[1], 2) if row[1] else None,
            '24h': round(row[2], 2) if row[2] else None,
        }

        c.execute("SELECT COUNT(*) FROM signals WHERE monitoring = 1 AND closed = 0")
        active_monitoring = c.fetchone()[0]

        return {
            'total_signals':     total,
            'by_verdict':        by_verdict,
            'avg_outcomes_buy':  avg_outcomes,
            'active_monitoring': active_monitoring,
            'date':              today,
        }
    finally:
        conn.close()


def get_stats_all_time():
    """Statystyki z calej historii (np. dla /performance)."""
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
            WHERE verdict = 'BUY' AND outcome_1h IS NOT NULL
        ''')
        row = c.fetchone()
        avg_outcomes = {
            '1h':  round(row[0], 2) if row[0] else None,
            '4h':  round(row[1], 2) if row[1] else None,
            '24h': round(row[2], 2) if row[2] else None,
        }

        c.execute("SELECT COUNT(*) FROM signals WHERE monitoring = 1 AND closed = 0")
        active_monitoring = c.fetchone()[0]

        return {
            'total_signals':     total,
            'by_verdict':        by_verdict,
            'avg_outcomes_buy':  avg_outcomes,
            'active_monitoring': active_monitoring,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("Baza danych OK")
