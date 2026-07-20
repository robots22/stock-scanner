#!/usr/bin/env python3
"""
CLEANUP — zamyka Alpaca pozycje otwarte PRZED dzisiaj + porzadkuje DB.

Alpaca "position" endpoint nie ma pola open_date, ale mozemy sprawdzic
przez orders history z filtrem status=filled: kazda pozycja ma powiazany
BUY order z filled_at.

Wykonanie:
  python3 cleanup_pre_today.py           # dry-run: pokazuje co zamknie
  python3 cleanup_pre_today.py --execute # zamyka pozycje
"""

import sys
from datetime import datetime, timedelta
import pytz

CHICAGO_TZ = pytz.timezone('America/Chicago')


def analyze_and_cleanup(execute=False):
    from alpaca_trader import AlpacaPaperTrader

    trader = AlpacaPaperTrader()
    if not trader.enabled:
        print("Alpaca Paper trader wylaczony w .env — pomijam")
        return

    today = datetime.now(CHICAGO_TZ).date()
    print(f"Dzisiejsza data (Chicago): {today}")

    positions = trader.get_positions()
    print(f"Otwartych pozycji Alpaca: {len(positions)}\n")

    if not positions:
        return

    # Pobierz BUY orders z ostatnich 30 dni zeby zmatchowac z pozycjami
    since = (datetime.now(pytz.UTC) - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
    orders = trader._get(f'/v2/orders?status=filled&side=buy&limit=500&after={since}')
    if not orders:
        orders = []

    # {ticker: earliest_filled_at}
    ticker_open_date = {}
    for o in orders:
        sym  = (o.get('symbol') or '').upper()
        fill = o.get('filled_at')
        if not sym or not fill:
            continue
        try:
            fill_dt   = datetime.fromisoformat(fill.replace('Z', '+00:00'))
            fill_date = fill_dt.astimezone(CHICAGO_TZ).date()
        except Exception:
            continue

        # Wez NAJWCZESNIEJSZY BUY dla tickera (data otwarcia)
        if sym not in ticker_open_date or fill_date < ticker_open_date[sym]:
            ticker_open_date[sym] = fill_date

    to_close = []
    to_keep  = []

    for p in positions:
        sym     = (p.get('symbol') or '').upper()
        qty     = p.get('qty')
        pnl     = float(p.get('unrealized_pl') or 0)
        pnl_pct = float(p.get('unrealized_plpc') or 0) * 100

        open_date = ticker_open_date.get(sym)
        line = f"  {sym:6s} qty={qty:>6} | PnL: ${pnl:+7.2f} ({pnl_pct:+6.1f}%)"

        if open_date is None:
            # Brak w orders history = najprawdopodobniej stara pozycja
            to_close.append((p, 'brak w orders history'))
            print(f"{line} | opened: ??? -> ZAMKNAC")
        elif open_date < today:
            to_close.append((p, f'otwarta {open_date}'))
            print(f"{line} | opened: {open_date} -> ZAMKNAC")
        else:
            to_keep.append(p)
            print(f"{line} | opened: {open_date} -> zachowaj (dzis)")

    print(f"\nDo zamkniecia: {len(to_close)} | Do zachowania: {len(to_keep)}")

    if not execute:
        print("\nAby zamknac pozycje:  python3 cleanup_pre_today.py --execute")
        return

    if not to_close:
        return

    print("\n" + "!" * 60)
    confirm = input(f"Zamknac {len(to_close)} pozycji? Wpisz TAK: ")
    if confirm.strip() != 'TAK':
        print("Anulowano")
        return

    print("\nZamykanie...")
    closed = 0
    failed = 0
    for pos, reason in to_close:
        sym = pos.get('symbol')
        result = trader.sell(sym, reason=f'cleanup: {reason}')
        if result:
            closed += 1
            print(f"  [OK]   {sym}: PnL ${result['pnl']:+.2f} ({result['pnl_pct']:+.1f}%)")
        else:
            failed += 1
            print(f"  [FAIL] {sym}")

    print(f"\nGotowe: zamknieto {closed} | bledy {failed}")


def cleanup_db_monitoring():
    """Zamknij w DB stare aktywne monitorowania (monitoring_end < now)."""
    import sqlite3
    conn = sqlite3.connect('scanner.db')
    c = conn.cursor()

    # Wygasle monitoringi
    c.execute('''
        UPDATE signals
        SET monitoring = 0, closed = 1, close_reason = 'cleanup_pre_today'
        WHERE monitoring = 1 AND closed = 0
        AND (monitoring_end IS NULL OR datetime(monitoring_end) < datetime('now'))
    ''')
    n_expired = c.rowcount

    # Pozycje sprzed dzis w DB (BUY z DATE(timestamp) < today Chicago)
    today = datetime.now(CHICAGO_TZ).strftime('%Y-%m-%d')
    c.execute('''
        UPDATE signals
        SET monitoring = 0, closed = 1, close_reason = 'cleanup_pre_today'
        WHERE monitoring = 1 AND closed = 0
        AND DATE(timestamp) < ?
    ''', (today,))
    n_old = c.rowcount

    conn.commit()

    c.execute("SELECT COUNT(*) FROM signals WHERE monitoring=1 AND closed=0")
    active = c.fetchone()[0]

    conn.close()
    print(f"\nDB monitoring cleanup: wygasle={n_expired} | sprzed_dzis={n_old} | aktywnych zostalo={active}")


if __name__ == '__main__':
    execute = '--execute' in sys.argv

    print("=" * 60)
    print("CLEANUP: Alpaca pozycje sprzed dzis")
    print("=" * 60)
    analyze_and_cleanup(execute=execute)

    if execute:
        print("\n" + "=" * 60)
        print("CLEANUP: DB monitoring")
        print("=" * 60)
        cleanup_db_monitoring()
