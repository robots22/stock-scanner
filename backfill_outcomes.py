#!/usr/bin/env python3
"""
STOCK SCANNER - BACKFILL OUTCOMES
Zapisz jako backfill_outcomes.py w folderze stock-scanner

Zadanie:
  Wypelnia outcome_1h/4h/24h WSTECZNIE dla wszystkich sygnalow
  uzywajac historycznych minute bars z Polygon.

  Grupuje sygnaly po (ticker, dzien) — jeden API call na grupe.
  Polygon Starter: unlimited calls, 5 lat historii.

Uruchomienie:
  python3 backfill_outcomes.py

Historia:
  v1.0 - backfill z Polygon 5-minute bars
"""

import sqlite3
import time
from datetime import datetime, timedelta
import pytz

from polygon_api import PolygonAPI
from config import logger

CHICAGO_TZ = pytz.timezone('America/Chicago')
DB_PATH    = 'scanner.db'


def parse_ts(ts_str):
    """Parsuj timestamp z bazy do datetime z tz."""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = CHICAGO_TZ.localize(dt)
    return dt


def get_minute_bars(polygon, ticker, day_str, next_day_str):
    """
    Pobierz 5-minute bars dla tickera na dzien + nastepny dzien.
    Zwraca liste (timestamp_ms, close_price).
    """
    try:
        data = polygon._get(
            f'/v2/aggs/ticker/{ticker}/range/5/minute/{day_str}/{next_day_str}',
            params={'adjusted': 'false', 'sort': 'asc', 'limit': 5000},
            cache_ttl=86400,
        )
        if not data:
            return []
        results = data.get('results', [])
        return [(bar['t'], bar['c']) for bar in results if bar.get('c')]
    except Exception as e:
        logger.debug(f"Bars error {ticker} {day_str}: {e}")
        return []


def price_at(bars, target_dt):
    """
    Znajdz cene najblizsza (>=) target_dt.
    bars: lista (timestamp_ms, close).
    Zwraca cene lub None.
    """
    target_ms = int(target_dt.timestamp() * 1000)
    # Pierwszy bar >= target
    for ts_ms, close in bars:
        if ts_ms >= target_ms:
            return close
    # Jesli target po ostatnim barze — uzyj ostatniego (max 26h roznic)
    if bars:
        last_ts, last_close = bars[-1]
        if target_ms - last_ts < 26 * 3600 * 1000:
            return last_close
    return None


def main():
    polygon = PolygonAPI()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # WSZYSTKIE sygnaly - przeliczamy od nowa (spojnosc: adjusted=false)
    c.execute('''
        SELECT id, ticker, timestamp, price,
               outcome_1h, outcome_4h, outcome_24h
        FROM signals
        WHERE price > 0
        ORDER BY ticker, timestamp
    ''')
    rows = c.fetchall()
    print(f"Sygnalow do backfill: {len(rows)}")

    # Grupuj po (ticker, dzien)
    groups = {}
    for row in rows:
        ticker = row['ticker'].upper()
        day    = row['timestamp'][:10]
        key    = (ticker, day)
        groups.setdefault(key, []).append(dict(row))

    print(f"Grup (ticker, dzien): {len(groups)}")

    now      = datetime.now(CHICAGO_TZ)
    updated  = 0
    skipped  = 0
    api_hits = 0

    for i, ((ticker, day), signals) in enumerate(sorted(groups.items()), 1):
        # Nastepny dzien dla 24h outcome
        day_dt   = datetime.strptime(day, '%Y-%m-%d')
        next_day = (day_dt + timedelta(days=2)).strftime('%Y-%m-%d')

        bars = get_minute_bars(polygon, ticker, day, next_day)
        api_hits += 1

        if not bars:
            skipped += len(signals)
            continue

        for sig in signals:
            entry = float(sig['price'] or 0)
            if entry <= 0:
                skipped += 1
                continue

            sig_dt  = parse_ts(sig['timestamp'])
            updates = {}

            for h, col in ((1, 'outcome_1h'), (4, 'outcome_4h'), (24, 'outcome_24h')):
                target = sig_dt + timedelta(hours=h)
                # Nie licz przyszlosci
                if target > now:
                    continue
                p = price_at(bars, target)
                if p and p > 0:
                    change = round((p - entry) / entry * 100, 2)
                    # Sanity: |zmiana| > 500% w 1h/4h = prawdopodobny split artifact
                    if h <= 4 and abs(change) > 500:
                        logger.warning(f"SANITY SKIP: {sig['ticker']} {col} = {change:+.0f}% (split?)")
                        updates[col] = None
                        continue
                    updates[col] = change
                    updates[col + '_at'] = now.isoformat()

            if updates:
                set_clause = ', '.join(f"{k}=?" for k in updates)
                c.execute(
                    f"UPDATE signals SET {set_clause} WHERE id=?",
                    list(updates.values()) + [sig['id']]
                )
                updated += 1

        if i % 25 == 0:
            conn.commit()
            print(f"  [{i}/{len(groups)}] {ticker} {day} | updated: {updated} | API: {api_hits}")

    conn.commit()
    conn.close()
    print(f"\nGotowe: {updated} sygnalow zaktualizowanych | {skipped} pominietych | {api_hits} API calls")


if __name__ == '__main__':
    main()
