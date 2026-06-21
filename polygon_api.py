#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 10: MASSIVE/POLYGON API WRAPPER
Zapisz jako polygon_api.py w folderze stock-scanner

Zastępuje MockPolygon prawdziwymi danymi z Massive.com (dawny Polygon.io).
Endpointy api.polygon.io nadal działają bez zmian po rebrandzie.

Endpointy:
    GET /v2/snapshot/locale/us/markets/stocks/tickers  → cały rynek
    GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker} → jeden ticker
    GET /v2/reference/news  → newsy
    GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to} → historia (volume ratio)

Dokumentacja: https://massive.com/docs/rest/stocks/overview

Historia zmian:
    v1.0 — pierwsza wersja, podstawowe endpointy snapshot, newsy, historia
"""

import requests
import time
from datetime import date, timedelta
from config import logger, CONFIG, POLYGON_API_KEY, now_chicago


# ==================== KONFIGURACJA ====================

POLYGON_BASE_URL = "https://api.polygon.io"  # działa równolegle z api.massive.com
POLYGON_TIMEOUT  = 15


# ==================== GŁÓWNA KLASA ====================

class PolygonAPI:
    """
    Wrapper dla Massive/Polygon API.
    Interfejs identyczny jak MockPolygon —
    podmiana wymaga tylko zmiany jednej linii w main.py.
    """

    def __init__(self):
        if not POLYGON_API_KEY:
            raise ValueError(
                "Brak POLYGON_API_KEY w pliku .env\n"
                "Dodaj: POLYGON_API_KEY=twój_klucz"
            )

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'StockScanner/1.0',
        })

        # Cache w pamięci
        self._cache     = {}
        self._cache_ttl = {}
        self.total_calls = 0

        logger.info("PolygonAPI zainicjowany — tryb LIVE")

    # ==================== CACHE ====================

    def _cache_get(self, key):
        if key in self._cache:
            if time.time() < self._cache_ttl.get(key, 0):
                return self._cache[key]
        return None

    def _cache_set(self, key, value, ttl_seconds=60):
        self._cache[key]     = value
        self._cache_ttl[key] = time.time() + ttl_seconds

    # ==================== HTTP ====================

    def _get(self, endpoint, params=None, cache_ttl=60):
        """
        Wykonuje GET request do Polygon API z cache i obsługą błędów.
        """
        params = params or {}
        params['apiKey'] = POLYGON_API_KEY

        cache_key = f"{endpoint}:{str(sorted(params.items()))}"
        cached    = self._cache_get(cache_key)
        if cached is not None:
            return cached

        url = f"{POLYGON_BASE_URL}{endpoint}"

        try:
            response = self.session.get(
                url,
                params=params,
                timeout=POLYGON_TIMEOUT,
            )
            self.total_calls += 1

            if response.status_code == 401:
                logger.error("Polygon API: nieprawidłowy klucz API (401)")
                return None

            if response.status_code == 403:
                logger.error("Polygon API: brak dostępu do endpointu (403) — "
                             "sprawdź plan")
                return None

            if response.status_code == 429:
                logger.warning("Polygon API: rate limit (429) — czekam 10s")
                time.sleep(10)
                return self._get(endpoint, params, cache_ttl)

            if response.status_code == 404:
                return None

            if response.status_code != 200:
                logger.error(f"Polygon API błąd {response.status_code}: "
                             f"{endpoint}")
                return None

            data = response.json()

            if data.get('status') == 'ERROR':
                logger.error(f"Polygon API error: {data.get('error', '')}")
                return None

            self._cache_set(cache_key, data, cache_ttl)
            return data

        except requests.exceptions.Timeout:
            logger.error(f"Polygon API timeout: {endpoint}")
            return None
        except Exception as e:
            logger.error(f"Polygon API wyjątek: {endpoint} — {e}")
            return None

    # ==================== UNIVERSE TICKERÓW ====================

    def get_universe(self):
        """
        Zwraca listę wszystkich aktywnych tickerów z danymi rynkowymi.
        Interfejs identyczny jak MockPolygon.get_universe().

        Endpoint: GET /v2/snapshot/locale/us/markets/stocks/tickers
        """
        data = self._get(
            '/v2/snapshot/locale/us/markets/stocks/tickers',
            cache_ttl=300,  # cache 5 minut (cykl główny)
        )

        if not data:
            return []

        tickers  = data.get('tickers', [])
        universe = []

        for item in tickers:
            try:
                ticker = item.get('ticker', '')
                day    = item.get('day', {})
                prev   = item.get('prevDay', {})

                price  = day.get('c', 0) or prev.get('c', 0)
                volume = day.get('v', 0)

                # Filtruj już na tym etapie
                if not (CONFIG['min_price'] <= price <= CONFIG['max_price']):
                    continue
                if volume < CONFIG['min_volume']:
                    continue

                change_pct = item.get('todaysChangePerc', 0) or 0

                # Volume ratio — potrzebuje historii 30 dni
                # Na razie używamy prostego ratio vs poprzedni dzień
                prev_volume = prev.get('v', 0)
                volume_ratio = round(volume / max(prev_volume, 1), 2) \
                               if prev_volume > 0 else 1.0

                universe.append({
                    'ticker':         ticker,
                    'price':          round(float(price), 2),
                    'change_pct':     round(float(change_pct), 2),
                    'change_dollar':  round(float(item.get('todaysChange', 0)), 2),
                    'volume':         int(volume),
                    'avg_volume_30d': int(prev_volume),  # placeholder
                    'volume_ratio':   volume_ratio,
                    'high':           round(float(day.get('h', price)), 2),
                    'low':            round(float(day.get('l', price)), 2),
                    'open':           round(float(day.get('o', price)), 2),
                    'vwap':           round(float(day.get('vw', price)), 2),
                    'earnings':       None,  # wypełnia Finnhub
                    'insider':        None,  # wypełnia Finnhub
                })

            except Exception as e:
                logger.warning(f"Polygon snapshot parse error: {e}")
                continue

        logger.info(f"Polygon: {len(universe)} tickerów $"
                    f"{CONFIG['min_price']}-${CONFIG['max_price']} "
                    f"z vol > {CONFIG['min_volume']:,}")
        return universe

    def get_ticker_details(self, ticker):
        """
        Zwraca szczegóły dla konkretnego tickera.
        Używane przez monitoring aktywnych BUY.

        Endpoint: GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
        """
        data = self._get(
            f'/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}',
            cache_ttl=60,
        )

        if not data:
            return {}

        item = data.get('ticker', {})
        day  = item.get('day', {})
        prev = item.get('prevDay', {})

        price  = day.get('c', 0) or prev.get('c', 0)
        volume = day.get('v', 0)

        prev_volume  = prev.get('v', 0)
        volume_ratio = round(volume / max(prev_volume, 1), 2) \
                       if prev_volume > 0 else 1.0

        return {
            'ticker':        ticker,
            'price':         round(float(price), 2),
            'change_pct':    round(float(item.get('todaysChangePerc', 0)), 2),
            'volume':        int(volume),
            'volume_ratio':  volume_ratio,
            'high':          round(float(day.get('h', price)), 2),
            'low':           round(float(day.get('l', price)), 2),
            'vwap':          round(float(day.get('vw', price)), 2),
        }

    # ==================== VOLUME RATIO 30 DNI ====================

    def get_avg_volume_30d(self, ticker):
        """
        Pobiera prawdziwą średnią wolumenu z ostatnich 30 dni.
        Używane do dokładnego volume ratio.

        Endpoint: GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}
        """
        cache_key = f"avg_vol_30d:{ticker}"
        cached    = self._cache_get(cache_key)
        if cached:
            return cached

        today     = date.today()
        from_date = (today - timedelta(days=35)).isoformat()
        to_date   = (today - timedelta(days=1)).isoformat()

        data = self._get(
            f'/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}',
            params={'adjusted': 'true', 'sort': 'asc', 'limit': 30},
            cache_ttl=3600,  # cache 1 godzina
        )

        if not data:
            return 0

        results = data.get('results', [])
        if not results:
            return 0

        volumes  = [r.get('v', 0) for r in results if r.get('v', 0) > 0]
        avg_vol  = int(sum(volumes) / len(volumes)) if volumes else 0

        self._cache_set(cache_key, avg_vol, 3600)
        return avg_vol

    # ==================== NEWSY ====================

    def get_news(self, ticker, limit=3):
        """
        Zwraca newsy dla tickera z ostatnich 24h.
        Interfejs identyczny jak MockPolygon.get_news().

        Endpoint: GET /v2/reference/news
        """
        data = self._get(
            '/v2/reference/news',
            params={
                'ticker': ticker,
                'limit':  limit,
                'sort':   'published_utc',
                'order':  'desc',
            },
            cache_ttl=900,  # cache 15 minut
        )

        if not data:
            return []

        results = data.get('results', [])
        news    = []

        for item in results:
            # Prosta analiza sentymentu po słowach kluczowych
            title = item.get('title', '').lower()
            desc  = item.get('description', '').lower()
            text  = f"{title} {desc}"

            bullish_words = [
                'beat', 'beats', 'upgrade', 'upgraded', 'approve', 'approved',
                'breakthrough', 'partnership', 'contract', 'launch', 'wins',
                'record', 'raises guidance', 'buyback', 'dividend'
            ]
            bearish_words = [
                'miss', 'misses', 'downgrade', 'downgraded', 'delay', 'delayed',
                'lawsuit', 'investigation', 'recall', 'cuts guidance', 'layoffs',
                'bankruptcy', 'dilution', 'offering'
            ]

            bull_score = sum(1 for w in bullish_words if w in text)
            bear_score = sum(1 for w in bearish_words if w in text)

            if bull_score > bear_score:
                sentiment = 'bullish'
            elif bear_score > bull_score:
                sentiment = 'bearish'
            else:
                sentiment = 'neutral'

            news.append({
                'title':       item.get('title', ''),
                'description': item.get('description', ''),
                'published':   item.get('published_utc', ''),
                'sentiment':   sentiment,
                'url':         item.get('article_url', ''),
            })

        return news

    # ==================== STATYSTYKI ====================

    def get_stats(self):
        return {
            'total_calls': self.total_calls,
            'cache_size':  len(self._cache),
        }


# ==================== TEST ====================

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  TEST: Massive/Polygon API (LIVE)")
    print("="*55)

    try:
        polygon = PolygonAPI()
    except ValueError as e:
        print(f"\n❌ {e}")
        exit(1)

    # Test 1: Universe tickerów
    print("\n✅ Test 1: Universe tickerów $0.01-$15")
    universe = polygon.get_universe()
    print(f"  Tickerów w universe: {len(universe)}")

    if universe:
        print("\n  Przykładowe tickery:")
        for t in universe[:5]:
            print(f"  {t['ticker']:6s} | ${t['price']:6.2f} | "
                  f"{t['change_pct']:+.1f}% | "
                  f"Vol: {t['volume']:>10,} | "
                  f"Ratio: {t['volume_ratio']:.1f}x")
    else:
        print("  Brak danych (weekend lub poza godzinami rynkowymi)")

    # Test 2: Szczegóły tickera
    print("\n✅ Test 2: Szczegóły AAPL")
    details = polygon.get_ticker_details('AAPL')
    if details:
        print(f"  Cena: ${details.get('price', 0):.2f}")
        print(f"  Zmiana: {details.get('change_pct', 0):+.2f}%")
        print(f"  Wolumen: {details.get('volume', 0):,}")
    else:
        print("  Brak danych")

    # Test 3: Średnia wolumenu 30 dni
    print("\n✅ Test 3: Średnia wolumenu 30 dni dla SOUN")
    avg_vol = polygon.get_avg_volume_30d('SOUN')
    print(f"  Średni wolumen 30d: {avg_vol:,}")

    # Test 4: Newsy
    print("\n✅ Test 4: Newsy dla SOUN")
    news = polygon.get_news('SOUN', limit=3)
    if news:
        for n in news:
            print(f"  [{n['sentiment']:7s}] {n['title'][:60]}")
    else:
        print("  Brak newsów")

    stats = polygon.get_stats()
    print(f"\n📊 API calls: {stats['total_calls']}")

    print("\n" + "="*55)
    print("  Plik 10 gotowy ✅")
    print("="*55)