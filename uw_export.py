#!/usr/bin/env python3
"""
UW EXPORT — backup wszystkich danych UW z bazy i live snapshot.

Eksportuje:
  1. Wszystkie wzmianki UW w justification/reasons z signals (n=455 BUY)
  2. Live snapshot: dark pool, market tide, flow alerts (co jest DOSTEPNE teraz)

Wynik: uw_backup_<YYYYMMDD>.json — pelny dump przed anulacja subskrypcji.

Uruchomienie:
  python3 uw_export.py
"""

import sqlite3
import json
from datetime import datetime
import pytz

CHICAGO_TZ = pytz.timezone('America/Chicago')


def export_signals_with_uw():
    """Wyciagnij wszystkie sygnaly gdzie UW/dark pool/flow byl wspomniany."""
    conn = sqlite3.connect('scanner.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('''
        SELECT id, ticker, timestamp, verdict, confidence, price,
               change_pct, volume_ratio, score, justification, reasons,
               outcome_1h, outcome_4h, outcome_24h
        FROM signals
        WHERE (justification LIKE '%UW%'      OR reasons LIKE '%UW%'
            OR justification LIKE '%dark pool%' OR reasons LIKE '%dark pool%'
            OR justification LIKE '%options flow%' OR reasons LIKE '%options flow%'
            OR justification LIKE '%unusual%' OR reasons LIKE '%unusual%'
            OR justification LIKE '%call/put%' OR reasons LIKE '%call/put%'
            OR justification LIKE '%CPR%'      OR reasons LIKE '%CPR%')
        ORDER BY timestamp
    ''')

    signals = [dict(r) for r in c.fetchall()]
    conn.close()
    return signals


def export_live_uw():
    """Wolaj UW API dla ostatniego snapshotu."""
    try:
        from uw_api import UnusualWhalesAPI
        uw = UnusualWhalesAPI()

        snapshot = {
            'exported_at': datetime.now(CHICAGO_TZ).isoformat(),
            'dark_pool_recent':  uw.get_dark_pool_flow(),
            'market_tide':       uw.get_market_tide(),
            'flow_alerts':       uw.get_flow_alerts(),
        }
        return snapshot
    except Exception as e:
        return {'error': str(e)}


def main():
    print("=" * 60)
    print("UW EXPORT — backup przed anulacja subskrypcji")
    print("=" * 60)

    print("\n[1/2] Eksport sygnalow z DB gdzie UW byl wspomniany...")
    signals = export_signals_with_uw()
    print(f"  Znaleziono: {len(signals)} sygnalow z UW/dark pool/flow")

    # Statystyki win rate na tych sygnalach
    with_outcome = [s for s in signals if s['outcome_1h'] is not None]
    if with_outcome:
        wins = sum(1 for s in with_outcome if s['outcome_1h'] > 0)
        avg  = sum(s['outcome_1h'] for s in with_outcome) / len(with_outcome)
        print(f"  Z outcome: {len(with_outcome)} | WR: {wins/len(with_outcome)*100:.1f}% | avg1h: {avg:+.2f}%")

    print("\n[2/2] Live snapshot dark pool + market tide + flow alerts...")
    live = export_live_uw()
    if 'error' in live:
        print(f"  UWAGA: {live['error']}")
    else:
        print(f"  dark_pool_recent: {len(live.get('dark_pool_recent', []))}")
        print(f"  flow_alerts:      {len(live.get('flow_alerts', []))}")
        print(f"  market_tide:      {'OK' if live.get('market_tide') else 'brak'}")

    payload = {
        'exported_at':    datetime.now(CHICAGO_TZ).isoformat(),
        'signals_count':  len(signals),
        'signals':        signals,
        'live_snapshot':  live,
    }

    fname = f"uw_backup_{datetime.now(CHICAGO_TZ).strftime('%Y%m%d_%H%M%S')}.json"
    with open(fname, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, default=str)

    print(f"\n[OK] Zapisano: {fname}")
    print(f"     Rozmiar: {sum(len(json.dumps(s)) for s in signals):,} bajtow surowo")


if __name__ == '__main__':
    main()
