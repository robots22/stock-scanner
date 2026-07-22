#!/usr/bin/env python3
"""
BACKTEST MULTI-STRATEGY na 6 miesiecy historii Polygon

Strategie testowane:

1. SHORT_GAP_FADE:  gap >= 50%, short at open
   SL -20% | TP +25%

2. LONG_GAP_SMALL:  gap 10-50%, vol >= 2M, long at open
   SL -5% | TP +8%

3. LONG_NANO_RUNNER: cena < $1, vol >= 5M, chg >= 15%, long at open
   SL -8% | TP +20%

4. LONG_MICRO_BREAK: cena $1-5, vol >= 5M, chg >= 20%, long at open
   SL -6% | TP +15%

5. LONG_LOWFLOAT:   float < 5M (estymowany z market cap), vol >= 2M, long
   SL -8% | TP +15%

6. LONG_VWAP_RECLAIM: gap 5-30%, open below VWAP, reclaim VWAP w pierwszych 30min
   SL -3% | TP +8%

Kazda strategia: WR, avg PnL, best/worst, symulacja SL/TP, per-day frequency.

Polygon Advanced: unlimited calls, adjusted=false.

Uruchomienie:
  python3 backtest.py

Czas: ~20-30 minut (2000-3000 API calls)
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

END_DATE   = date.today()
START_DATE = END_DATE - timedelta(days=180)

EXCLUDED_SUFFIXES = ('W', 'WS', 'WW', 'R', 'RT', 'U')
EXCLUDED_TICKERS  = {'SOXS', 'SOXL', 'TQQQ', 'SQQQ', 'UVXY', 'SPXU', 'SPXL',
                     'FNGU', 'FNGD', 'LABU', 'LABD', 'TZA', 'TNA'}

session = requests.Session()
session.headers.update({'User-Agent': 'StockScanner-Backtest/2.0'})
api_calls = 0


def api_get(endpoint, params=None):
    global api_calls
    params = params or {}
    params['apiKey'] = POLYGON_KEY
    try:
        r = session.get(f'{BASE_URL}{endpoint}', params=params, timeout=15)
        api_calls += 1
        if r.status_code == 429:
            time.sleep(5)
            return api_get(endpoint, params)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def is_excluded(ticker):
    t = ticker.upper()
    if t in EXCLUDED_TICKERS:
        return True
    for suffix in EXCLUDED_SUFFIXES:
        if t.endswith(suffix) and len(t) > len(suffix):
            return True
    if '.' in t or '-' in t:
        return True
    return False


def get_trading_days(start, end):
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def get_grouped_bars(day_str):
    data = api_get('/v2/aggs/grouped/locale/us/market/stocks/' + day_str,
                   params={'adjusted': 'false'})
    if not data:
        return []
    return data.get('results', [])


def get_minute_bars(ticker, day_str, end_str):
    data = api_get(
        f'/v2/aggs/ticker/{ticker}/range/5/minute/{day_str}/{end_str}',
        params={'adjusted': 'false', 'sort': 'asc', 'limit': 5000}
    )
    if not data:
        return []
    return data.get('results', [])


def price_at_offset(bars, open_ts_ms, hours):
    target_ms = open_ts_ms + int(hours * 3600 * 1000)
    for bar in bars:
        if bar['t'] >= target_ms:
            return bar['c']
    if bars:
        return bars[-1]['c']
    return None


def max_adverse_move(bars, open_ts_ms, entry_price, hours, direction='short'):
    """MAE: max niekorzystny ruch. direction='short' = max wzrost, 'long' = max spadek."""
    target_ms = open_ts_ms + int(hours * 3600 * 1000)
    worst = entry_price
    for bar in bars:
        if bar['t'] > target_ms:
            break
        if bar['t'] >= open_ts_ms:
            if direction == 'short' and bar.get('h', 0) > worst:
                worst = bar['h']
            elif direction == 'long' and bar.get('l', 0) < worst:
                worst = bar['l']
    if entry_price > 0:
        pct = (worst - entry_price) / entry_price * 100
        return round(abs(pct), 2)
    return 0


def vwap_from_bars(bars, open_ts_ms, minutes=30):
    """Oblicz VWAP z pierwszych N minut."""
    target_ms = open_ts_ms + minutes * 60 * 1000
    total_vp = 0
    total_v  = 0
    for bar in bars:
        if bar['t'] > target_ms:
            break
        if bar['t'] >= open_ts_ms:
            v  = bar.get('v', 0)
            vw = bar.get('vw', bar.get('c', 0))
            total_vp += vw * v
            total_v  += v
    if total_v > 0:
        return total_vp / total_v
    return None


def first_bar_above(bars, open_ts_ms, threshold, minutes=30):
    """Czy cena przebila threshold w pierwszych N minutach?"""
    target_ms = open_ts_ms + minutes * 60 * 1000
    for bar in bars:
        if bar['t'] > target_ms:
            break
        if bar['t'] >= open_ts_ms and bar.get('h', 0) >= threshold:
            return bar['c']  # cena zamkniecia bara gdzie przebito
    return None


# ==================== STRATEGIE ====================

def check_short_gap_fade(bar, prev_close):
    """S1: SHORT gap >= 50%, vol >= 500k."""
    gap = (bar['o'] - prev_close) / prev_close * 100 if prev_close else 0
    if gap >= 50 and bar['v'] >= 500_000 and 0.10 <= bar['o'] <= 20:
        return {'strategy': 'SHORT_GAP_FADE', 'gap': round(gap, 1),
                'entry': bar['o'], 'direction': 'short',
                'sl_pct': -20, 'tp_pct': 25}
    return None


def check_long_gap_small(bar, prev_close):
    """S2: LONG gap 10-50%, vol >= 2M."""
    gap = (bar['o'] - prev_close) / prev_close * 100 if prev_close else 0
    if 10 <= gap < 50 and bar['v'] >= 2_000_000 and 0.50 <= bar['o'] <= 15:
        return {'strategy': 'LONG_GAP_SMALL', 'gap': round(gap, 1),
                'entry': bar['o'], 'direction': 'long',
                'sl_pct': -5, 'tp_pct': 8}
    return None


def check_long_nano_runner(bar, prev_close):
    """S3: LONG nano-cap < $1, vol >= 5M, zmiana >= 15%."""
    chg = (bar['c'] - bar['o']) / bar['o'] * 100 if bar['o'] else 0
    if bar['o'] < 1.0 and bar['v'] >= 5_000_000 and chg >= 15 and bar['o'] >= 0.10:
        return {'strategy': 'LONG_NANO_RUNNER', 'gap': round(chg, 1),
                'entry': bar['o'], 'direction': 'long',
                'sl_pct': -8, 'tp_pct': 20}
    return None


def check_long_micro_break(bar, prev_close):
    """S4: LONG micro-cap $1-5, vol >= 5M, zmiana >= 20%."""
    chg = (bar['c'] - bar['o']) / bar['o'] * 100 if bar['o'] else 0
    if 1.0 <= bar['o'] <= 5.0 and bar['v'] >= 5_000_000 and chg >= 20:
        return {'strategy': 'LONG_MICRO_BREAK', 'gap': round(chg, 1),
                'entry': bar['o'], 'direction': 'long',
                'sl_pct': -6, 'tp_pct': 15}
    return None


def check_long_extreme_vol(bar, prev_close, avg_vol):
    """S5: LONG extreme volume (> 20x avg), vol >= 2M, chg > 0."""
    if avg_vol and avg_vol > 0:
        ratio = bar['v'] / avg_vol
    else:
        ratio = 0
    chg = (bar['c'] - bar['o']) / bar['o'] * 100 if bar['o'] else 0
    if ratio >= 20 and bar['v'] >= 2_000_000 and chg > 0 and 0.10 <= bar['o'] <= 15:
        return {'strategy': 'LONG_EXTREME_VOL', 'gap': round(chg, 1),
                'entry': bar['o'], 'direction': 'long', 'vol_ratio': round(ratio, 0),
                'sl_pct': -6, 'tp_pct': 10}
    return None


ALL_STRATEGIES = [
    check_short_gap_fade,
    check_long_gap_small,
    check_long_nano_runner,
    check_long_micro_break,
]


def main():
    global api_calls

    print("=" * 70)
    print(f"BACKTEST MULTI-STRATEGY | {START_DATE} -> {END_DATE} (6 miesiecy)")
    print("=" * 70)

    trading_days = get_trading_days(START_DATE, END_DATE)
    print(f"Dni handlowe: {len(trading_days)}")

    # Cache prev_close per ticker per dzien
    prev_closes = {}  # {day_str: {ticker: close}}
    all_signals = []

    for i, day in enumerate(trading_days):
        day_str  = day.strftime('%Y-%m-%d')
        next_str = (day + timedelta(days=2)).strftime('%Y-%m-%d')
        prev_str = (day - timedelta(days=1)).strftime('%Y-%m-%d')

        # Pobierz grouped daily bars na TEN dzien
        bars_today = get_grouped_bars(day_str)
        if not bars_today:
            continue

        # Pobierz grouped daily bars na WCZORAJ (prev close)
        if prev_str not in prev_closes:
            bars_prev = get_grouped_bars(prev_str)
            if bars_prev:
                prev_closes[prev_str] = {b['T']: b['c'] for b in bars_prev if b.get('T') and b.get('c')}
            else:
                prev_closes[prev_str] = {}

        prev_close_map = prev_closes.get(prev_str, {})

        # Sprawdz kazdy ticker w dniu
        candidates = []
        for bar in bars_today:
            ticker = bar.get('T', '')
            if is_excluded(ticker):
                continue

            prev_c = prev_close_map.get(ticker)
            if not prev_c or prev_c <= 0:
                continue

            # Testuj kazda strategie
            for strategy_fn in ALL_STRATEGIES:
                signal = strategy_fn(bar, prev_c)
                if signal:
                    signal['ticker'] = ticker
                    signal['day']    = day_str
                    signal['volume'] = bar['v']
                    signal['close']  = bar['c']
                    signal['high']   = bar.get('h', 0)
                    signal['low']    = bar.get('l', 0)
                    signal['prev_close'] = prev_c
                    candidates.append(signal)

        # Dla kazdego kandydata — pobierz minute bars i oblicz outcome
        # Ogranicz do 15 per dzien zeby nie przekroczyc API
        candidates.sort(key=lambda x: x.get('volume', 0), reverse=True)
        candidates = candidates[:15]

        seen_tickers_day = set()
        for sig in candidates:
            ticker = sig['ticker']
            if ticker in seen_tickers_day:
                continue
            seen_tickers_day.add(ticker)

            mbars = get_minute_bars(ticker, day_str, next_str)
            if not mbars:
                continue

            entry   = sig['entry']
            open_ms = mbars[0]['t'] if mbars else 0
            direction = sig['direction']

            # Outcome
            p1h  = price_at_offset(mbars, open_ms, 1)
            p4h  = price_at_offset(mbars, open_ms, 4)
            p24h = price_at_offset(mbars, open_ms, 24)

            if direction == 'short':
                sig['pnl_1h']  = round(-((p1h - entry) / entry * 100), 2) if p1h and entry else None
                sig['pnl_4h']  = round(-((p4h - entry) / entry * 100), 2) if p4h and entry else None
                sig['pnl_24h'] = round(-((p24h - entry) / entry * 100), 2) if p24h and entry else None
            else:
                sig['pnl_1h']  = round(((p1h - entry) / entry * 100), 2) if p1h and entry else None
                sig['pnl_4h']  = round(((p4h - entry) / entry * 100), 2) if p4h and entry else None
                sig['pnl_24h'] = round(((p24h - entry) / entry * 100), 2) if p24h and entry else None

            sig['mae_4h'] = max_adverse_move(mbars, open_ms, entry, 4, direction)

            # SL/TP simulation
            sl = sig['sl_pct']
            tp = sig['tp_pct']
            if direction == 'short':
                sig['stopped'] = sig['mae_4h'] >= abs(sl)
            else:
                sig['stopped'] = sig['mae_4h'] >= abs(sl)

            all_signals.append(sig)

        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(trading_days)}] {day_str} | "
                  f"sygnaly: {len(all_signals)} | API: {api_calls}")

    # Zapisz prev_closes na disk (zeby nie tracic danych)
    prev_closes.clear()

    # ==================== WYNIKI ====================
    print("\n" + "=" * 70)
    print(f"WYNIKI BACKESTU | {len(all_signals)} sygnalow | {api_calls} API calls")
    print("=" * 70)

    strategies = {}
    for s in all_signals:
        strat = s['strategy']
        strategies.setdefault(strat, []).append(s)

    for strat, sigs in sorted(strategies.items()):
        print(f"\n{'—' * 60}")
        print(f"  {strat} (n={len(sigs)})")
        print(f"{'—' * 60}")

        with_4h = [s for s in sigs if s.get('pnl_4h') is not None]
        if not with_4h:
            print("  Brak danych 4h")
            continue

        # Bez SL/TP
        vals_1h  = [s['pnl_1h'] for s in sigs if s.get('pnl_1h') is not None]
        vals_4h  = [s['pnl_4h'] for s in with_4h]
        vals_24h = [s['pnl_24h'] for s in sigs if s.get('pnl_24h') is not None]

        def p(vals, label):
            if not vals:
                return
            wins = sum(1 for v in vals if v > 0)
            avg  = sum(vals) / len(vals)
            best = max(vals)
            worst = min(vals)
            print(f"  {label}: n={len(vals):4d} WR {wins/len(vals)*100:5.1f}% "
                  f"avg {avg:+6.2f}% best {best:+.0f}% worst {worst:+.0f}%")

        p(vals_1h, '1h ')
        p(vals_4h, '4h ')
        p(vals_24h, '24h')

        # Z SL/TP
        sl = sigs[0]['sl_pct']
        tp = sigs[0]['tp_pct']
        total_pnl = 0
        wins_sim  = 0
        trades    = 0
        for s in with_4h:
            if s.get('stopped'):
                total_pnl += sl
            else:
                pnl = min(s['pnl_4h'], tp) if s['pnl_4h'] > 0 else s['pnl_4h']
                total_pnl += pnl
                if pnl > 0:
                    wins_sim += 1
            trades += 1

        stopped = sum(1 for s in with_4h if s.get('stopped'))
        if trades:
            print(f"\n  SL/TP sim ({sl}%/{tp}%):")
            print(f"    Trades: {trades} | Wins: {wins_sim} ({wins_sim/trades*100:.0f}%) | "
                  f"Stopped: {stopped} ({stopped/trades*100:.0f}%)")
            print(f"    Total PnL: {total_pnl:+.1f}% | Avg/trade: {total_pnl/trades:+.2f}%")

        # Frequency
        from collections import Counter
        daily = Counter(s['day'] for s in sigs)
        print(f"\n  Srednio dziennie: {len(sigs)/len(daily):.1f} sygnalow "
              f"(min {min(daily.values())} max {max(daily.values())})")

        # TOP 5
        top5 = sorted(with_4h, key=lambda x: x.get('pnl_4h', -999), reverse=True)[:5]
        print(f"\n  TOP 5:")
        for s in top5:
            print(f"    {s['ticker']:6s} {s['day']} gap{s.get('gap',0):+.0f}% "
                  f"${s['entry']:.2f} -> 4h: {s['pnl_4h']:+.1f}%")

        # WORST 5
        worst5 = sorted(with_4h, key=lambda x: x.get('pnl_4h', 999))[:5]
        print(f"  WORST 5:")
        for s in worst5:
            print(f"    {s['ticker']:6s} {s['day']} gap{s.get('gap',0):+.0f}% "
                  f"${s['entry']:.2f} -> 4h: {s['pnl_4h']:+.1f}%")

    # Zapisz wyniki
    fname = f"backtest_multi_{START_DATE}_{END_DATE}.json"
    with open(fname, 'w') as f:
        json.dump({
            'start': str(START_DATE), 'end': str(END_DATE),
            'strategies': {
                strat: {
                    'n': len(sigs),
                    'signals': sigs
                }
                for strat, sigs in strategies.items()
            }
        }, f, indent=2)
    print(f"\nZapisano: {fname}")
    print(f"API calls: {api_calls}")


if __name__ == '__main__':
    main()
