#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 8: BACKTESTING
Zapisz jako backtest.py w folderze stock-scanner

Zadanie:
- Symuluje działanie systemu na danych historycznych
- Sprawdza trafność sygnałów pre-filtra
- Mierzy jakość werdyktów Claude'a (w trybie DEMO: mock)
- Generuje raport wyników

Uruchomienie:
    python backtest.py
"""

import random
import json
from datetime import datetime, timedelta
from collections import defaultdict

from config import logger, CONFIG, now_chicago
from mock_polygon import MockPolygon, MockUnusualWhales, MockFinnhub
from pre_filter import get_top_tickers
from claude_analyst import ClaudeAnalyst
from database import init_db, save_signal, get_stats


# ==================== GENERATOR DANYCH HISTORYCZNYCH ====================

class HistoricalDataGenerator:
    """
    Generuje symulowane dane historyczne dla backtestingu.
    W trybie LIVE zastąpiony przez prawdziwe dane z Polygon
    (Polygon Starter ma 5 lat historii).
    """

    def __init__(self, days=30):
        self.days    = days
        self.polygon = MockPolygon()
        self.uw      = MockUnusualWhales()
        self.fh      = MockFinnhub()

    def generate_session(self, date):
        """
        Generuje dane dla jednej sesji tradingowej.
        Zwraca universe tickerów z danymi jak w prawdziwym skanie.
        """
        # Resetuj ceny dla każdej sesji (różne dni = różne ceny)
        random.seed(date.toordinal())  # reprodukowalne dane dla tej samej daty
        self.polygon._init_prices()

        universe = self.polygon.get_universe()

        # Dodaj dane Finnhub
        for t in universe:
            ticker       = t['ticker']
            t['earnings'] = self.fh.get_earnings_calendar(ticker)
            t['insider']  = self.fh.get_insider_transactions(ticker)

        dark_pool = self.uw.get_dark_pool_flow()

        finnhub_cache = {}
        for t in universe:
            ticker = t['ticker']
            if t.get('earnings') or t.get('insider'):
                finnhub_cache[ticker] = {
                    'earnings': t['earnings'],
                    'insider':  t['insider'],
                }

        return universe, dark_pool, finnhub_cache

    def simulate_price_outcome(self, entry_price, verdict):
        """
        Symuluje cenę po 1h, 4h, 24h od sygnału.
        W trybie DEMO używa losowych ale realistycznych ruchów.
        W trybie LIVE: prawdziwe ceny z Polygon.
        """
        # BUY sygnały mają lekką przewagę (założenie optymistyczne)
        # W prawdziwym backteście używamy rzeczywistych cen
        if verdict == 'BUY':
            bias = 0.5  # lekki bias w górę
        elif verdict == 'AVOID':
            bias = -0.5  # lekki bias w dół
        else:
            bias = 0.0

        outcome_1h  = random.gauss(bias, 2.5)   # średnia ±2.5%
        outcome_4h  = random.gauss(bias * 2, 4)  # większa zmienność
        outcome_24h = random.gauss(bias * 3, 7)  # jeszcze większa

        return {
            '1h':  round(outcome_1h, 2),
            '4h':  round(outcome_4h, 2),
            '24h': round(outcome_24h, 2),
        }


# ==================== METRYKI ====================

class BacktestMetrics:
    """Zbiera i liczy metryki backtestingu"""

    def __init__(self):
        self.signals    = []
        self.by_verdict = defaultdict(list)

    def add_signal(self, ticker, verdict, confidence, entry_price, outcomes):
        """Dodaje sygnał do metryk"""
        record = {
            'ticker':     ticker,
            'verdict':    verdict,
            'confidence': confidence,
            'price':      entry_price,
            'outcome_1h':  outcomes['1h'],
            'outcome_4h':  outcomes['4h'],
            'outcome_24h': outcomes['24h'],
            'win_1h':      outcomes['1h'] > 0,
            'win_4h':      outcomes['4h'] > 0,
            'win_24h':     outcomes['24h'] > 0,
        }
        self.signals.append(record)
        self.by_verdict[verdict].append(record)

    def compute(self):
        """Liczy wszystkie metryki"""
        if not self.signals:
            return {}

        results = {
            'total_signals': len(self.signals),
            'by_verdict':    {},
        }

        for verdict, signals in self.by_verdict.items():
            if not signals:
                continue

            outcomes_1h  = [s['outcome_1h']  for s in signals]
            outcomes_4h  = [s['outcome_4h']  for s in signals]
            outcomes_24h = [s['outcome_24h'] for s in signals]

            win_rate_1h  = sum(1 for s in signals if s['win_1h'])  / len(signals) * 100
            win_rate_4h  = sum(1 for s in signals if s['win_4h'])  / len(signals) * 100
            win_rate_24h = sum(1 for s in signals if s['win_24h']) / len(signals) * 100

            # Sygnały z wysoką pewnością
            high_conf = [s for s in signals if s['confidence'] == 'WYSOKA']
            high_conf_winrate = 0
            if high_conf:
                high_conf_winrate = (sum(1 for s in high_conf if s['win_1h'])
                                     / len(high_conf) * 100)

            results['by_verdict'][verdict] = {
                'count':             len(signals),
                'avg_outcome_1h':    round(sum(outcomes_1h)  / len(outcomes_1h),  2),
                'avg_outcome_4h':    round(sum(outcomes_4h)  / len(outcomes_4h),  2),
                'avg_outcome_24h':   round(sum(outcomes_24h) / len(outcomes_24h), 2),
                'win_rate_1h':       round(win_rate_1h,  1),
                'win_rate_4h':       round(win_rate_4h,  1),
                'win_rate_24h':      round(win_rate_24h, 1),
                'high_conf_count':   len(high_conf),
                'high_conf_winrate': round(high_conf_winrate, 1),
                'best_trade':        round(max(outcomes_1h),  2),
                'worst_trade':       round(min(outcomes_1h),  2),
            }

        # Metryki dla BUY z wysoką pewnością (najważniejsze)
        buy_high = [s for s in self.by_verdict.get('BUY', [])
                    if s['confidence'] == 'WYSOKA']
        if buy_high:
            results['buy_high_confidence'] = {
                'count':       len(buy_high),
                'win_rate_1h': round(
                    sum(1 for s in buy_high if s['win_1h'])
                    / len(buy_high) * 100, 1),
                'avg_1h': round(
                    sum(s['outcome_1h'] for s in buy_high)
                    / len(buy_high), 2),
            }

        return results


# ==================== GŁÓWNA KLASA BACKTESTINGU ====================

class Backtester:

    def __init__(self, days=10):
        self.days      = days
        self.generator = HistoricalDataGenerator(days)
        self.analyst   = ClaudeAnalyst()
        self.metrics   = BacktestMetrics()
        init_db()

    def run(self):
        """
        Uruchamia backtest na N dniach historycznych.
        """
        print(f"\n{'='*55}")
        print(f"  BACKTEST — {self.days} dni historycznych")
        print(f"  Tryb: {'DEMO (symulowane dane)' if True else 'LIVE'}")
        print(f"{'='*55}\n")

        start_date = now_chicago().date() - timedelta(days=self.days)

        total_scans   = 0
        total_signals = 0

        for day_offset in range(self.days):
            date = start_date + timedelta(days=day_offset)

            # Pomijaj weekendy
            if date.weekday() >= 5:
                continue

            print(f"📅 {date.strftime('%Y-%m-%d')} "
                  f"({date.strftime('%A')})")

            # Symuluj kilka skanów w ciągu dnia (co 5 min przez 6.5h = ~78)
            # W backteście robimy 3 skany na dzień dla szybkości
            scans_per_day = 3

            day_signals = 0

            for scan_num in range(scans_per_day):
                # Generuj dane dla tej sesji
                universe, dark_pool, finnhub_cache = \
                    self.generator.generate_session(date)

                # Pre-filter → TOP 5
                top5 = get_top_tickers(
                    universe,
                    dark_pool_flow=dark_pool,
                    finnhub_cache=finnhub_cache,
                    top_n=CONFIG['max_tickers_for_claude'],
                )

                if not top5:
                    continue

                # Analiza przez Claude (mock w DEMO)
                results = self.analyst.analyze_batch(
                    top5,
                    polygon_api=self.generator.polygon,
                    uw_api=self.generator.uw,
                )

                # Zbierz metryki
                for result, ticker_data in zip(results, top5):
                    verdict    = result.get('verdict', 'WATCH')
                    confidence = result.get('confidence', 'NISKA')
                    price      = ticker_data.get('price', 0)

                    # Symuluj wynik cenowy
                    outcomes = self.generator.simulate_price_outcome(
                        price, verdict
                    )

                    self.metrics.add_signal(
                        ticker=ticker_data.get('ticker'),
                        verdict=verdict,
                        confidence=confidence,
                        entry_price=price,
                        outcomes=outcomes,
                    )

                    day_signals  += 1
                    total_signals += 1

                total_scans += 1

            print(f"  Skanów: {scans_per_day} | "
                  f"Sygnałów: {day_signals}")

        print(f"\nŁącznie: {total_scans} skanów | "
              f"{total_signals} sygnałów\n")

        return self.metrics.compute()

    def print_report(self, results):
        """Drukuje raport backtestingu"""
        if not results:
            print("Brak wyników do raportu")
            return

        print(f"\n{'='*55}")
        print(f"  RAPORT BACKTESTINGU")
        print(f"{'='*55}")
        print(f"  Łącznie sygnałów: {results['total_signals']}")

        for verdict, stats in results.get('by_verdict', {}).items():
            icon = ('🟢' if verdict == 'BUY'
                    else '🟡' if verdict == 'WATCH'
                    else '🔴')

            print(f"\n{icon} {verdict} ({stats['count']} sygnałów):")
            print(f"  Win rate:    1h: {stats['win_rate_1h']}% | "
                  f"4h: {stats['win_rate_4h']}% | "
                  f"24h: {stats['win_rate_24h']}%")
            print(f"  Avg wynik:   1h: {stats['avg_outcome_1h']:+.2f}% | "
                  f"4h: {stats['avg_outcome_4h']:+.2f}% | "
                  f"24h: {stats['avg_outcome_24h']:+.2f}%")
            print(f"  Best/Worst:  {stats['best_trade']:+.2f}% / "
                  f"{stats['worst_trade']:+.2f}%")

            if stats['high_conf_count'] > 0:
                print(f"  Wysoka pewność: {stats['high_conf_count']} sygnałów | "
                      f"win rate 1h: {stats['high_conf_winrate']}%")

        # Najważniejsza metryka
        bhc = results.get('buy_high_confidence')
        if bhc:
            print(f"\n{'='*55}")
            print(f"  ⭐ BUY + WYSOKA PEWNOŚĆ (kluczowa metryka)")
            print(f"{'='*55}")
            print(f"  Sygnałów:  {bhc['count']}")
            print(f"  Win rate:  {bhc['win_rate_1h']}% (1h)")
            print(f"  Avg wynik: {bhc['avg_1h']:+.2f}% (1h)")

            # Ocena systemu
            print(f"\n  Ocena systemu:")
            wr = bhc['win_rate_1h']
            if wr >= 60:
                print(f"  ✅ DOBRY — win rate {wr}% > 60% (próg opłacalności)")
            elif wr >= 50:
                print(f"  ⚠️  PRZECIĘTNY — win rate {wr}% (potrzebna optymalizacja)")
            else:
                print(f"  ❌ SŁABY — win rate {wr}% < 50% (system wymaga pracy)")

            print(f"\n  UWAGA: To wyniki na danych DEMO (losowych).")
            print(f"  Prawdziwy backtest wymaga kluczy Polygon API.")

        print(f"\n{'='*55}")

    def save_report(self, results):
        """Zapisuje raport do pliku JSON"""
        filename = f"backtest_{now_chicago().strftime('%Y%m%d_%H%M')}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Raport zapisany: {filename}")
        return filename


# ==================== ANALIZA BAZY DANYCH ====================

def analyze_db_signals():
    """
    Analizuje sygnały zapisane w bazie danych.
    Pokazuje trafność na podstawie rzeczywistych wyników.
    """
    from database import get_connection

    conn = get_connection()
    try:
        c = conn.cursor()

        # Ogólne statystyki
        c.execute('''
            SELECT verdict, COUNT(*) as cnt,
                   AVG(outcome_1h) as avg_1h,
                   AVG(outcome_4h) as avg_4h,
                   AVG(outcome_24h) as avg_24h
            FROM signals
            WHERE outcome_1h IS NOT NULL
            GROUP BY verdict
        ''')
        rows = c.fetchall()

        if not rows:
            print("\nBrak sygnałów z wynikami w bazie.")
            print("Wyniki pojawią się po 1h, 4h, 24h od sygnałów.")
            return

        print(f"\n{'='*55}")
        print(f"  ANALIZA BAZY DANYCH")
        print(f"{'='*55}")

        for row in rows:
            verdict, cnt, avg_1h, avg_4h, avg_24h = row
            icon = ('🟢' if verdict == 'BUY'
                    else '🟡' if verdict == 'WATCH'
                    else '🔴')
            print(f"\n{icon} {verdict} ({cnt} sygnałów z wynikami):")
            if avg_1h:
                print(f"  Avg 1h:  {avg_1h:+.2f}%")
            if avg_4h:
                print(f"  Avg 4h:  {avg_4h:+.2f}%")
            if avg_24h:
                print(f"  Avg 24h: {avg_24h:+.2f}%")

        # Najlepsze i najgorsze sygnały
        c.execute('''
            SELECT ticker, verdict, price, outcome_1h, timestamp
            FROM signals
            WHERE verdict = 'BUY' AND outcome_1h IS NOT NULL
            ORDER BY outcome_1h DESC
            LIMIT 5
        ''')
        best = c.fetchall()

        if best:
            print(f"\n🏆 TOP 5 BUY sygnałów (wynik 1h):")
            for row in best:
                ticker, verdict, price, outcome, ts = row
                print(f"  {ticker:6s} @ ${price:.2f} → "
                      f"{outcome:+.2f}% ({ts[:10]})")

    finally:
        conn.close()


# ==================== URUCHOMIENIE ====================

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  STOCK SCANNER — BACKTEST")
    print("="*55)
    print("\nCo chcesz zrobić?")
    print("  1 — Backtest na symulowanych danych (10 dni)")
    print("  2 — Analiza sygnałów z bazy danych")
    print("  3 — Oba")

    choice = input("\nWybór (1/2/3): ").strip()

    if choice in ('1', '3'):
        backtester = Backtester(days=10)
        results    = backtester.run()
        backtester.print_report(results)
        backtester.save_report(results)

    if choice in ('2', '3'):
        analyze_db_signals()

    print("\n" + "="*55)
    print("  Plik 8 gotowy ✅")
    print("="*55)