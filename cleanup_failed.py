#!/usr/bin/env python3
"""
CLEANUP FAILED — dokoncza to co cleanup_pre_today.py nie moglo zamknac.

Problem: DELETE /v2/positions blokowane przez otwarte sell orders
(trailing_stop, take_profit) na tej pozycji.

Rozwiazanie: dla kazdej pozostalej pozycji:
  1. Znajdz i anuluj wszystkie otwarte sell orders na tym tickerze
  2. Poczekaj 2s (Alpaca musi zwolnic qty)
  3. DELETE pozycje
  4. Loguj kazdy krok

Uruchomienie:
  python3 cleanup_failed.py             # dry-run
  python3 cleanup_failed.py --execute
"""

import sys
import time
from datetime import datetime
import pytz

CHICAGO_TZ = pytz.timezone('America/Chicago')


def main(execute=False):
    from alpaca_trader import AlpacaPaperTrader

    trader = AlpacaPaperTrader()
    if not trader.enabled:
        print("Alpaca wylaczony")
        return

    today = datetime.now(CHICAGO_TZ).date()
    print(f"Dzis: {today}\n")

    # Pozostale pozycje
    positions = trader.get_positions()
    print(f"Pozostalych pozycji: {len(positions)}")

    if not positions:
        print("Nic do zamkniecia")
        return

    # Otwarte orders (sell strony)
    open_orders = trader._get('/v2/orders?status=open&limit=500')
    if not open_orders:
        open_orders = []

    # Group orders by ticker
    orders_by_ticker = {}
    for o in open_orders:
        sym  = (o.get('symbol') or '').upper()
        side = o.get('side')
        if side == 'sell':
            orders_by_ticker.setdefault(sym, []).append(o)

    # Match: ktore pozycje maja blokujace orders
    positions_by_ticker = {}
    for p in positions:
        sym = (p.get('symbol') or '').upper()
        positions_by_ticker[sym] = p

    # Filtruj: tylko pozycje otwarte przed dzis (identycznie jak wczesniej)
    # Pobierz BUY history
    from datetime import timedelta
    since = (datetime.now(pytz.UTC) - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
    buy_orders = trader._get(f'/v2/orders?status=filled&side=buy&limit=500&after={since}') or []

    ticker_open_date = {}
    for o in buy_orders:
        sym  = (o.get('symbol') or '').upper()
        fill = o.get('filled_at')
        if not sym or not fill:
            continue
        try:
            fill_dt   = datetime.fromisoformat(fill.replace('Z', '+00:00'))
            fill_date = fill_dt.astimezone(CHICAGO_TZ).date()
            if sym not in ticker_open_date or fill_date < ticker_open_date[sym]:
                ticker_open_date[sym] = fill_date
        except Exception:
            continue

    to_process = []
    for sym, p in positions_by_ticker.items():
        open_date = ticker_open_date.get(sym)
        pnl_pct   = float(p.get('unrealized_plpc') or 0) * 100
        n_orders  = len(orders_by_ticker.get(sym, []))

        if open_date is None:
            reason = 'brak w historii'
            action = 'ZAMKNAC'
        elif open_date < today:
            reason = f'otwarta {open_date}'
            action = 'ZAMKNAC'
        else:
            reason = f'dzis ({open_date})'
            action = 'zachowaj'

        print(f"  {sym:6s} PnL {pnl_pct:+6.1f}% | sell orders: {n_orders} | {reason} -> {action}")

        if action == 'ZAMKNAC':
            to_process.append((sym, p, orders_by_ticker.get(sym, [])))

    print(f"\nDo zamkniecia: {len(to_process)}")

    if not execute:
        print("\nAby wykonac: python3 cleanup_failed.py --execute")
        return

    confirm = input(f"Zamknac {len(to_process)} pozycji (najpierw anulowac ich sell orders)? Wpisz TAK: ")
    if confirm.strip() != 'TAK':
        print("Anulowano")
        return

    print("\nProcesing...\n")
    closed  = 0
    failed  = 0
    for sym, pos, sell_orders in to_process:
        # 1. Anuluj wszystkie sell orders dla tickera
        for o in sell_orders:
            oid = o.get('id')
            if not oid:
                continue
            ok = trader._delete(f'/v2/orders/{oid}')
            print(f"  [{sym}] cancel order {oid[:8]}... -> {'OK' if ok else 'FAIL'}")

        # 2. Czekaj 2s zeby Alpaca zwolnil qty
        if sell_orders:
            time.sleep(2)

        # 3. Zamknij pozycje
        result = trader.sell(sym, reason='cleanup_failed')
        if result:
            closed += 1
            print(f"  [{sym}] CLOSED  PnL: ${result['pnl']:+.2f} ({result['pnl_pct']:+.1f}%)\n")
        else:
            failed += 1
            # Sprobuj alternatywnie: market sell dostepnej ilosci
            trader._get(f'/v2/positions/{sym}')  # refresh
            pos_fresh = trader.get_position(sym)
            if pos_fresh:
                avail = int(float(pos_fresh.get('qty_available', 0) or 0))
                if avail > 0:
                    order = {
                        'symbol':        sym,
                        'qty':           str(avail),
                        'side':          'sell',
                        'type':          'market',
                        'time_in_force': 'day',
                    }
                    res = trader._post('/v2/orders', order)
                    if res:
                        closed += 1
                        failed -= 1
                        print(f"  [{sym}] FALLBACK market sell x{avail} zlozono\n")
                        continue
            print(f"  [{sym}] FAILED (sprawdz Alpaca manualnie)\n")

    print(f"\nGotowe: zamknieto {closed} | bledy {failed}")


if __name__ == '__main__':
    main(execute='--execute' in sys.argv)
