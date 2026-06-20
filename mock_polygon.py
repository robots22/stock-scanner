#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 2: MOCK POLYGON
Zapisz jako mock_polygon.py w folderze stock-scanner

Symuluje dane rynkowe bez kluczy API.
Zastępowany przez polygon_api.py gdy masz prawdziwe klucze.
"""

import random
from config import logger, CONFIG

# ==================== SYMULOWANY RYNEK ====================
# Reprezentatywna lista small-cap tickerów $0.01-$15
# W prawdziwym systemie ta lista pochodzi z Polygon snapshot
MOCK_UNIVERSE = [
    # Tech / AI
    'SOUN', 'IONQ', 'RXRX', 'ASTS', 'RKLB',
    'ACHR', 'JOBY', 'BBAI', 'NLTX', 'GFAI',
    # Biotech
    'NVAX', 'OCGN', 'AGEN', 'MNKD', 'JAGX',
    'CLOV', 'MESO', 'ATNF', 'CYTO', 'IMVT',
    # EV / Energy
    'NKLA', 'GOEV', 'RIDE', 'WKHS', 'AYRO',
    'PLUG', 'FCEL', 'BLNK', 'CHPT', 'HYLN',
    # Crypto / Mining
    'MARA', 'RIOT', 'CIFR', 'BTBT', 'BITF',
    # Inne
    'AMC', 'CLOV', 'WISH', 'EXPR', 'BBBY',
    'SPCE', 'LCID', 'BLMN', 'SAVE', 'PTON',
]

class MockPolygon:
    """
    Symuluje Polygon.io API.
    Zwraca losowe ale realistyczne dane rynkowe.
    """

    def __init__(self):
        # Zapamiętaj "bazowe" ceny żeby dane były spójne w ramach sesji
        self._base_prices = {}
        self._init_prices()
        logger.info("MockPolygon zainicjowany — tryb DEMO")

    def _init_prices(self):
        """Ustaw bazowe ceny dla wszystkich tickerów"""
        for ticker in MOCK_UNIVERSE:
            self._base_prices[ticker] = round(random.uniform(0.50, 14.50), 2)

    def get_universe(self):
        """
        Zwraca listę wszystkich tickerów z podstawowymi danymi.
        W prawdziwym systemie: Polygon /v2/snapshot/locale/us/markets/stocks/tickers
        """
        universe = []

        for ticker in MOCK_UNIVERSE:
            base_price = self._base_prices[ticker]

            # Losowa zmiana ceny — większość tickerów nudna, kilka aktywnych
            change_pct = random.gauss(0, 4)  # średnia 0%, odchylenie 4%
            change_pct = max(-25, min(25, change_pct))  # ogranicz do -25%/+25%

            price = round(base_price * (1 + change_pct / 100), 2)
            price = max(0.01, min(15.00, price))

            # Wolumen — większość normalny, kilka z volume spike
            base_volume = random.randint(50_000, 2_000_000)
            volume_spike = random.random() < 0.15  # 15% tickerów ma spike
            if volume_spike:
                volume = base_volume * random.uniform(2.5, 8.0)
            else:
                volume = base_volume * random.uniform(0.5, 1.5)
            volume = int(volume)

            # Średnia 30-dniowa wolumenu (symulowana)
            avg_volume_30d = random.randint(100_000, 1_500_000)
            volume_ratio = round(volume / avg_volume_30d, 2)

            universe.append({
                'ticker':         ticker,
                'price':          price,
                'change_pct':     round(change_pct, 2),
                'change_dollar':  round(price - base_price, 2),
                'volume':         volume,
                'avg_volume_30d': avg_volume_30d,
                'volume_ratio':   volume_ratio,
                'high':           round(price * random.uniform(1.0, 1.08), 2),
                'low':            round(price * random.uniform(0.92, 1.0), 2),
                'open':           round(base_price * random.uniform(0.97, 1.03), 2),
                'vwap':           round(price * random.uniform(0.98, 1.02), 2),
            })

        logger.info(f"MockPolygon: zwrócono {len(universe)} tickerów")
        return universe

    def get_news(self, ticker, limit=3):
        """
        Zwraca symulowane newsy dla tickera.
        W prawdziwym systemie: Polygon /v2/reference/news
        """
        # Pula możliwych newsów — mix bullish / bearish / neutral
        news_pool = [
            # Bullish
            {'title': f'{ticker} announces major partnership with Fortune 500 company',
             'sentiment': 'bullish'},
            {'title': f'{ticker} reports revenue beat, raises guidance',
             'sentiment': 'bullish'},
            {'title': f'{ticker} receives FDA fast-track designation',
             'sentiment': 'bullish'},
            {'title': f'{ticker} secures $50M government contract',
             'sentiment': 'bullish'},
            {'title': f'Analyst upgrades {ticker} to Buy, raises price target',
             'sentiment': 'bullish'},
            # Bearish
            {'title': f'{ticker} misses earnings estimates, cuts guidance',
             'sentiment': 'bearish'},
            {'title': f'{ticker} announces secondary offering at discount',
             'sentiment': 'bearish'},
            {'title': f'Analyst downgrades {ticker} citing competitive pressure',
             'sentiment': 'bearish'},
            {'title': f'{ticker} CEO resigns amid accounting review',
             'sentiment': 'bearish'},
            # Neutral
            {'title': f'{ticker} schedules Q3 earnings call for next week',
             'sentiment': 'neutral'},
            {'title': f'{ticker} presents at industry conference',
             'sentiment': 'neutral'},
            {'title': f'{ticker} files quarterly report with SEC',
             'sentiment': 'neutral'},
        ]

        # Losowo wybierz kilka newsów
        count = random.randint(0, limit)
        if count == 0:
            return []

        selected = random.sample(news_pool, min(count, len(news_pool)))
        return selected[:limit]

    def get_ticker_details(self, ticker):
        """
        Zwraca szczegóły dla konkretnego tickera.
        W prawdziwym systemie: Polygon /v3/reference/tickers/{ticker}
        """
        base_price = self._base_prices.get(ticker, random.uniform(0.50, 14.50))
        change_pct = random.gauss(0, 4)
        price = round(base_price * (1 + change_pct / 100), 2)
        price = max(0.01, min(15.00, price))

        volume = random.randint(100_000, 5_000_000)
        avg_volume = random.randint(100_000, 2_000_000)

        return {
            'ticker':         ticker,
            'price':          price,
            'change_pct':     round(change_pct, 2),
            'change_dollar':  round(price - base_price, 2),
            'volume':         volume,
            'avg_volume_30d': avg_volume,
            'volume_ratio':   round(volume / avg_volume, 2),
            'high':           round(price * 1.05, 2),
            'low':            round(price * 0.95, 2),
            'vwap':           round(price * 1.01, 2),
        }


# ==================== MOCK UNUSUAL WHALES ====================

class MockUnusualWhales:
    """
    Symuluje Unusual Whales API.
    Zwraca dark pool i options flow.
    """

    def __init__(self):
        logger.info("MockUnusualWhales zainicjowany — tryb DEMO")

    def get_dark_pool_flow(self):
        """
        Zwraca symulowane transakcje dark pool.
        W prawdziwym systemie: UW /api/darkpool/recent
        """
        # Tylko część tickerów pojawia się w dark pool
        active_tickers = random.sample(MOCK_UNIVERSE, random.randint(3, 8))

        flow = []
        for ticker in active_tickers:
            size = random.randint(100_000, 5_000_000)
            flow.append({
                'ticker':    ticker,
                'size_usd':  size,
                'side':      random.choice(['BUY', 'SELL', 'NEUTRAL']),
                'price':     round(random.uniform(0.50, 14.50), 2),
                'timestamp': '10 min ago',
            })

        logger.info(f"MockUW dark pool: {len(flow)} transakcji")
        return flow

    def get_options_flow(self, ticker):
        """
        Zwraca symulowane options flow dla tickera.
        W prawdziwym systemie: UW /api/options/flow/{ticker}
        """
        call_volume = random.randint(500, 50_000)
        put_volume  = random.randint(500, 50_000)

        return {
            'ticker':        ticker,
            'call_volume':   call_volume,
            'put_volume':    put_volume,
            'call_put_ratio': round(call_volume / max(put_volume, 1), 2),
            'unusual':       random.random() < 0.2,  # 20% szansa na "unusual"
            'sentiment':     'bullish' if call_volume > put_volume * 1.5
                             else 'bearish' if put_volume > call_volume * 1.5
                             else 'neutral',
        }


# ==================== MOCK FINNHUB ====================

class MockFinnhub:
    """
    Symuluje Finnhub API.
    Zwraca earnings calendar i insider transactions.
    """

    def __init__(self):
        logger.info("MockFinnhub zainicjowany — tryb DEMO")

    def get_earnings_calendar(self, ticker):
        """
        Zwraca symulowane dane earnings.
        W prawdziwym systemie: Finnhub /calendar/earnings
        """
        has_upcoming = random.random() < 0.2  # 20% tickerów ma nadchodzące earnings

        if has_upcoming:
            days_until = random.randint(1, 14)
            return {
                'ticker':            ticker,
                'days_until':        days_until,
                'estimate_eps':      round(random.uniform(-0.50, 0.50), 2),
                'previous_surprise': random.choice(['beat', 'miss', 'inline']),
            }
        return None

    def get_insider_transactions(self, ticker):
        """
        Zwraca symulowane insider transactions.
        W prawdziwym systemie: Finnhub /stock/insider-transactions
        """
        has_insider = random.random() < 0.15  # 15% tickerów ma insider activity

        if has_insider:
            return {
                'ticker':       ticker,
                'type':         random.choice(['BUY', 'SELL']),
                'value_usd':    random.randint(10_000, 500_000),
                'person':       random.choice(['CEO', 'CFO', 'Director', 'COO']),
                'days_ago':     random.randint(1, 7),
            }
        return None


# ==================== TEST ====================

if __name__ == "__main__":
    print("\n" + "="*50)
    print("  TEST: MockPolygon, MockUW, MockFinnhub")
    print("="*50)

    # Test MockPolygon
    polygon = MockPolygon()
    universe = polygon.get_universe()
    print(f"\n✅ MockPolygon: {len(universe)} tickerów w universe")

    # Pokaż 5 przykładowych
    print("\nPrzykładowe tickery:")
    for t in universe[:5]:
        print(f"  {t['ticker']:6s} | ${t['price']:5.2f} | "
              f"{t['change_pct']:+.1f}% | "
              f"Vol: {t['volume']:>9,} | "
              f"Ratio: {t['volume_ratio']:.1f}x")

    # Test filtrowania
    filtered = [t for t in universe
                if t['volume'] >= CONFIG['min_volume']
                and t['price'] >= CONFIG['min_price']
                and t['price'] <= CONFIG['max_price']]
    print(f"\n✅ Po filtrze (cena + wolumen): {len(filtered)} tickerów")

    # Test MockUW
    uw = MockUnusualWhales()
    dark_pool = uw.get_dark_pool_flow()
    print(f"\n✅ MockUW dark pool: {len(dark_pool)} transakcji")
    for dp in dark_pool[:3]:
        print(f"  {dp['ticker']:6s} | {dp['side']:7s} | ${dp['size_usd']:>10,}")

    # Test MockFinnhub
    fh = MockFinnhub()
    print(f"\n✅ MockFinnhub test:")
    for ticker in ['SOUN', 'IONQ', 'RXRX']:
        earnings = fh.get_earnings_calendar(ticker)
        insider  = fh.get_insider_transactions(ticker)
        if earnings:
            print(f"  {ticker}: earnings za {earnings['days_until']} dni "
                  f"(poprzedni: {earnings['previous_surprise']})")
        if insider:
            print(f"  {ticker}: insider {insider['type']} "
                  f"${insider['value_usd']:,} przez {insider['person']}")

    # Test newsów
    print(f"\n✅ Newsy dla SOUN:")
    news = polygon.get_news('SOUN')
    for n in news:
        print(f"  [{n['sentiment']:7s}] {n['title']}")

    print("\n" + "="*50)
    print("  Plik 2 gotowy ✅")
    print("="*50)