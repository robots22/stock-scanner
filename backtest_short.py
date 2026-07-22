#!/usr/bin/env python3
"""
BACKTEST: SHORT GAP_FADE na 6 miesiecy historii Polygon

Strategia:
  Identyfikuj GAP_MONSTER: gap >= 50% od prev close, vol >= 500k
  Symuluj SHORT przy otwarciu
  Mierz outcome: 1h, 4h, 24h po otwarciu

  Stop loss: -20% (squeeze protection)
  Take profit: +25%

Polygon Advanced: unlimited calls, pelna historia.

Uruchomienie:
  python3 backtest_short.py

Wynik: backtest_short_results.json + statystyki w konsoli
"""

import json
import time
from datetime import datetime, timedelta, date
import pytz
import requests
import os
from dotenv import load_dotenv
import pathlib

_ENV_PATH = pathlib.Path(__file__).parent / '.env'
load_dotenv(dotenv_path=_ENV_PATH, override=True)

POLYGON_KEY = os.getenv('POLYGON_API_KEY', '')
BASE_URL    = 'https://api.polygon.io'
CHICAGO_TZ  = pytz.timezone('America/Chicago')

# Parametry backestu
GAP_MIN       = 50.0    # min gap %
MIN_VOL       = 500_000
MIN_PRICE     = 0.10
MAX_PRICE     = 20.0
STOP_LOSS     = -20.0   # max strata na short (squeeze)
TAKE_PROFIT   = 25.0    # cel zysku na short

# Zakres dat: 6 miesiecy
END_DATE   = date.today()
START_DATE = END_DATE - timedelta(days=180)

# Excluded suffixes
EXCLUDED = ('W', 'WS', 'WW', 'R', 'RT', 'U', 'WKHS', 'SOXS', 'SOXL',
            'TQQQ', 'SQQQ', 'UVXY', 'SPXU', 'SPXL')

session = requests.Session()
session.headers.update({'User-Agent': 'StockScanner-Backtest/1.0'})


def api_get(endpoint, params=None):
    params = params or {}
    params['apiKey'] = POLYGON_KEY
    try:
        r = session.get(f'{BASE_URL}{endpoint}', params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(5)
            return api_get(endpoint, params)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        print(f"  API error: {e}")
        return None


def get_trading_days(start, end):
    """Zwraca liste dat handlowych (pon-pt, bez swiat)."""
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # pon-pt
            days.append(d)
        d += timedelta(days=1)
    return days


def get_daily_bars(day_str):
    """Pobierz grouped daily bars dla WSZYSTKICH tickerow na dany dzien."""
    data = api_get('/v2/aggs/grouped/locale/us/market/stocks/' + day_str,
                   params={'adjusted': 'false'})
    if not data:
        return []
    return data.get('results', [])


def get_prev_close(ticker, day_str):
    """Pobierz prev close z dnia przed day_str."""
    data = api_get(f'/v2/aggs/ticker/{ticker}/prev',
                   params={'adjusted': 'false'})
    if not data:
        return None
    results = data.get('results', [])
    if results:
        return results[0].get('c')
    return None


def get_minute_bars(ticker, day_str, next_day_str):
    """Pobierz 5-min bars na dzien + nastepny (do outcome 24h)."""
    data = api_get(
        f'/v2/aggs/ticker/{ticker}/range/5/minute/{day_str}/{next_day_str}',
        params={'adjusted': 'false', 'sort': 'asc', 'limit': 5000}
    )
    if not data:
        return []
    return [(bar['t'], bar['o'], bar['c'], bar['h'], bar['l'])
            for bar in data.get('results', [])
            if bar.get('c')]


def price_at_offset(bars, open_ts_ms, hours):
    """Znajdz cene po 'hours' godzinach od otwarcia."""
    target_ms = open_ts_ms + int(hours * 3600 * 1000)
    for ts, o, c, h, l in bars:
        if ts >= target_ms:
            return c
    # Jesli target po ostatnim barze — uzyj ostatniego
    if bars:
        return bars[-1][2]  # last close
    return None


def max_adverse_move(bars, open_ts_ms, entry_price, hours=4):
    """Znajdz max niekorzystny ruch (dla shorta = max wzrost) w oknie."""
    target_ms = open_ts_ms + int(hours * 3600 * 1000)
    max_high = entry_price
    for ts, o, c, h, l in bars:
        if ts > target_ms:
            break
        if ts >= open_ts_ms and h > max_high:
            max_high = h
    if entry_price > 0:
        return round((max_high - entry_price) / entry_price * 100, 2)
    return 0


def is_excluded(ticker):
    t = ticker.upper()
    for suffix in EXCLUDED:
        if t == suffix or (t.endswith(suffix) and len(t) > len(suffix)):
            return True
    if '.' in t or '-' in t:
        return True
    return False


def main():
    print("=" * 65)
    print(f"BACKTEST: SHORT GAP_FADE  |  {START_DATE} -> {END_DATE}")
    print(f"Kryteria: gap >= {GAP_MIN}% | vol >= {MIN_VOL:,} | cena ${MIN_PRICE}-${MAX_PRICE}")
    print(f"SL: {STOP_LOSS}% | TP: {TAKE_PROFIT}%")
    print("=" * 65)

    trading_days = get_trading_days(START_DATE, END_DATE)
    print(f"\nDni handlowe: {len(trading_days)}")

    all_signals = []
    api_calls   = 0

    for i, day in enumerate(trading_days):
        day_str  = day.strftime('%Y-%m-%d')
        next_str = (day + timedelta(days=2)).strftime('%Y-%m-%d')

        # Pobierz grouped daily bars
        bars = get_daily_bars(day_str)
        api_calls += 1
        if not bars:
            continue

        # Filtruj GAP_MONSTER kandydatow
        candidates = []
        for bar in bars:
            ticker = bar.get('T', '')
            if is_excluded(ticker):
                continue

            open_p  = bar.get('o', 0)
            close_p = bar.get('c', 0)
            volume  = bar.get('v', 0)
            high    = bar.get('h', 0)

            if not (MIN_PRICE <= open_p <= MAX_PRICE):
                continue
            if volume < MIN_VOL:
                continue

            # Potrzebujemy prev close do obliczenia gap
            # Uzyj prev close z grouped bars dnia wczesniejszego
            # OPTYMALIZACJA: prev close jest w prev_day grouped bars
            # ale to za duzo API calls - zamiast tego uzyj vwap z dnia
            # i oblicz gap z otwarcia vs prev close
            # Dla uproszczenia: gap = (open - prev_close) / prev_close
            # prev_close ~= low bound of range
            # Lepiej: pobierz /v2/aggs/ticker/{T}/prev dla kazdego kandydata
            candidates.append({
                'ticker': ticker,
                'open':   open_p,
                'close':  close_p,
                'high':   high,
                'volume': volume,
                'day':    day_str,
            })

        # Sortuj po volume — top 20 kandydatow po prev_close check
        candidates.sort(key=lambda x: x['volume'], reverse=True)
        candidates = candidates[:50]  # ogranicz API calls

        gap_monsters = []
        for cand in candidates:
            ticker = cand['ticker']
            # Pobierz prev close
            prev_data = api_get(f'/v2/aggs/ticker/{ticker}/prev',
                                params={'adjusted': 'false'})
            api_calls += 1
            if not prev_data or not prev_data.get('results'):
                continue

            prev_results = prev_data['results']
            # /prev zwraca DZISIEJSZE dane jesli rynek otwarty
            # Ale w backtest to nie problem — dane historyczne
            prev_close = None
            for pr in prev_results:
                if pr.get('T') == ticker:
                    prev_close = pr.get('c', 0)
                    break
            if not prev_close or prev_close <= 0:
                prev_close = prev_results[0].get('c', 0) if prev_results else 0
            if not prev_close or prev_close <= 0:
                continue

            gap_pct = (cand['open'] - prev_close) / prev_close * 100

            if gap_pct >= GAP_MIN:
                cand['prev_close'] = prev_close
                cand['gap_pct']    = round(gap_pct, 1)
                gap_monsters.append(cand)

        # Dla GAP_MONSTER — pobierz minute bars i oblicz outcome
        for gm in gap_monsters:
            ticker = gm['ticker']
            mbars  = get_minute_bars(ticker, day_str, next_str)
            api_calls += 1

            if not mbars:
                continue

            entry   = gm['open']  # SHORT at open
            open_ms = mbars[0][0] if mbars else 0

            # Outcome: SHORT = zysk gdy cena SPADA
            p1h  = price_at_offset(mbars, open_ms, 1)
            p4h  = price_at_offset(mbars, open_ms, 4)
            p24h = price_at_offset(mbars, open_ms, 24)

            # Short PnL = -(price_change%)
            o1h  = round(-((p1h  - entry) / entry * 100), 2) if p1h  and entry else None
            o4h  = round(-((p4h  - entry) / entry * 100), 2) if p4h  and entry else None
            o24h = round(-((p24h - entry) / entry * 100), 2) if p24h and entry else None

            # Max adverse (max wzrost = max strata shorta)
            mae = max_adverse_move(mbars, open_ms, entry, hours=4)

            # Stop loss simulation
            stopped_out = mae >= abs(STOP_LOSS) if mae else False

            signal = {
                'ticker':      ticker,
                'day':         day_str,
                'prev_close':  gm['prev_close'],
                'open':        entry,
                'gap_pct':     gm['gap_pct'],
                'volume':      gm['volume'],
                'high':        gm['high'],
                'short_1h':    o1h,
                'short_4h':    o4h,
                'short_24h':   o24h,
                'mae_4h':      mae,      # max adverse excursion (% wzrost)
                'stopped_out': stopped_out,
            }
            all_signals.append(signal)

        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(trading_days)}] {day_str} | "
                  f"sygnaly: {len(all_signals)} | API: {api_calls}")
            time.sleep(0.1)

    # ==================== WYNIKI ====================
    print("\n" + "=" * 65)
    print(f"WYNIKI BACKESTU: {len(all_signals)} GAP_MONSTER sygnałów")
    print("=" * 65)

    if not all_signals:
        print("Brak sygnałów!")
        return

    # Ogolne statystyki
    with_1h  = [s for s in all_signals if s['short_1h'] is not None]
    with_4h  = [s for s in all_signals if s['short_4h'] is not None]
    with_24h = [s for s in all_signals if s['short_24h'] is not None]

    def stats(data, key):
        if not data:
            return 'n/a'
        vals  = [s[key] for s in data if s[key] is not None]
        wins  = sum(1 for v in vals if v > 0)
        avg   = sum(vals) / len(vals) if vals else 0
        best  = max(vals) if vals else 0
        worst = min(vals) if vals else 0
        return f"n={len(vals):4d} | WR: {wins/len(vals)*100:5.1f}% | avg: {avg:+6.2f}% | best: {best:+.0f}% | worst: {worst:+.0f}%"

    print(f"\nBEZ stop loss:")
    print(f"  SHORT 1h:  {stats(with_1h, 'short_1h')}")
    print(f"  SHORT 4h:  {stats(with_4h, 'short_4h')}")
    print(f"  SHORT 24h: {stats(with_24h, 'short_24h')}")

    # Z stop loss
    not_stopped = [s for s in all_signals if not s.get('stopped_out')]
    stopped     = [s for s in all_signals if s.get('stopped_out')]
    print(f"\nZ STOP LOSS {STOP_LOSS}%:")
    print(f"  Stopped out:  {len(stopped)} ({len(stopped)/len(all_signals)*100:.0f}%)")
    print(f"  Survived:     {len(not_stopped)}")
    print(f"  SHORT 4h (survived): {stats(not_stopped, 'short_4h')}")

    # Symulacja P&L z SL/TP
    total_pnl = 0
    trades    = 0
    wins_sim  = 0
    for s in all_signals:
        if s.get('stopped_out'):
            total_pnl += STOP_LOSS  # strata = SL
            trades += 1
        elif s['short_4h'] is not None:
            pnl = min(s['short_4h'], TAKE_PROFIT)  # cap at TP
            total_pnl += pnl
            trades += 1
            if pnl > 0:
                wins_sim += 1

    if trades:
        print(f"\n  Symulacja SL/TP ({STOP_LOSS}%/{TAKE_PROFIT}%):")
        print(f"  Trades: {trades} | Wins: {wins_sim} ({wins_sim/trades*100:.0f}%) | "
              f"Total PnL: {total_pnl:+.1f}% | Avg: {total_pnl/trades:+.2f}%/trade")

    # Per gap bucket
    print(f"\nPo wielkosci gap:")
    for lo, hi, label in [(50,100,'50-100%'), (100,200,'100-200%'), (200,9999,'200%+')]:
        subset = [s for s in with_4h if lo <= s['gap_pct'] < hi]
        if subset:
            print(f"  Gap {label:8s}: {stats(subset, 'short_4h')}")

    # Po cenie
    print(f"\nPo cenie entry:")
    for lo, hi, label in [(0.1,1,'<$1'), (1,5,'$1-5'), (5,20,'$5-20')]:
        subset = [s for s in with_4h if lo <= s['open'] < hi]
        if subset:
            print(f"  Cena {label:6s}: {stats(subset, 'short_4h')}")

    # Dziennie
    from collections import Counter
    daily = Counter(s['day'] for s in all_signals)
    avg_daily = sum(daily.values()) / len(daily) if daily else 0
    print(f"\nSrednio dziennie: {avg_daily:.1f} sygnałów")

    # TOP 10 najlepszych shortow
    print(f"\nTOP 10 shortow (4h):")
    for s in sorted(with_4h, key=lambda x: x['short_4h'] or -999, reverse=True)[:10]:
        print(f"  {s['ticker']:6s} {s['day']} gap{s['gap_pct']:+.0f}% "
              f"${s['open']:.2f} → 4h: {s['short_4h']:+.1f}%")

    # TOP 10 najgorszych (squeeze)
    print(f"\nTOP 10 squeeze (najgorsze shorty 4h):")
    for s in sorted(with_4h, key=lambda x: x['short_4h'] or 999)[:10]:
        print(f"  {s['ticker']:6s} {s['day']} gap{s['gap_pct']:+.0f}% "
              f"${s['open']:.2f} → 4h: {s['short_4h']:+.1f}% MAE: {s['mae_4h']:+.1f}%")

    # Zapisz wyniki
    fname = f"backtest_short_results_{START_DATE}_{END_DATE}.json"
    with open(fname, 'w') as f:
        json.dump({
            'params': {
                'gap_min': GAP_MIN, 'min_vol': MIN_VOL,
                'stop_loss': STOP_LOSS, 'take_profit': TAKE_PROFIT,
                'start': str(START_DATE), 'end': str(END_DATE),
            },
            'n_signals': len(all_signals),
            'signals':   all_signals,
        }, f, indent=2)
    print(f"\nZapisano: {fname}")
    print(f"API calls: {api_calls}")


if __name__ == '__main__':
    main()
