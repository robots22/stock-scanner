#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 12: ALPACA API WRAPPER
Zapisz jako alpaca_api.py w folderze stock-scanner

Rola w systemie:
    Etap 2 (teraz): backup danych rynkowych gdy Polygon niedostępny
    Etap 3 (później): automatyczna egzekucja sygnałów (paper → live trading)

Endpointy (Market Data):
    GET /v2/stocks/snapshots          → snapshoty wielu tickerów
    GET /v2/stocks/{ticker}/snapshot  → snapshot jednego tickera
    GET /v2/stocks/{ticker}/bars      → historia (volume ratio 30d)

Dokumentacja: https://docs.alpaca.markets/us/docs/getting-started-with-alpaca-market-data

Historia zmian:
    v1.0 — pierwsza wersja, backup danych rynkowych (cena, wolumen, snapshot)
"""

import requests
import time
from datetime import date, timedelta, datetime, timezone
from config import logger, CONFIG, ALPACA_API_KEY, ALPACA_SECRET_KEY, now_chicago


# ==================== KONFIGURACJA ====================

ALPACA_DATA_URL = "https://data.alpaca.markets"
ALPACA_TRADING_URL = "https://paper-api.alpaca.markets"  # Paper trading
# ALPACA_TRADING_URL = "https://api.alpaca.markets"       # Live trading
ALPACA_TIMEOUT  = 15


# ==================== GŁÓWNA KLASA ====================

class AlpacaAPI:
    """
    Wrapper dla Alpaca Market Data API.
    Używany jako backup gdy Polygon/Massive niedostępny.

    W Etapie 3 zostanie rozszerzony o egzekucję zleceń.
    """

    def __init__(self):
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            raise ValueError(
                "Brak ALPACA_API_KEY lub ALPACA_SECRET_KEY w pliku .env"
            )

        self.session = requests.Session()
        self.session.headers.update({
            'APCA-API-KEY-ID':     ALPACA_API_KEY,
            'APCA-API-SECRET-KEY': ALPACA_SECRET_KEY,
            'Content-Type':        'application/json',
        })

        # Cache w pamięci
        self._cache     = {}
        self._cache_ttl = {}
        self.total_calls = 0

        logger.info("AlpacaAPI zainicjowany — backup danych rynkowych")

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
        Wykonuje GET request do Alpaca Market Data API.
        """
        cache_key = f"{endpoint}:{str(sorted((params or {}).items()))}"
        cached    = self._cache_get(cache_key)
        if cached is not None:
            return cached

        url = f"{ALPACA_DATA_URL}{endpoint}"

        try:
            response = self.session.get(
                url,
                params=params,
                timeout=ALPACA_TIMEOUT,
            )
            self.total_calls += 1

            if response.status_code == 401:
                logger.error("Alpaca API: nieprawidłowy klucz (401)")
                return None

            if response.status_code == 403:
                logger.error("Alpaca API: brak dostępu (403)")
                return None

            if response.status_code == 429:
                logger.warning("Alpaca API: rate limit (429) — czekam 5s")
                time.sleep(5)
                return self._get(endpoint, params, cache_ttl)

            if response.status_code != 200:
                logger.error(f"Alpaca API błąd {response.status_code}: "
                             f"{endpoint}")
                return None

            data = response.json()
            self._cache_set(cache_key, data, cache_ttl)
            return data

        except requests.exceptions.Timeout:
            logger.error(f"Alpaca API timeout: {endpoint}")
            return None
        except Exception as e:
            logger.error(f"Alpaca API wyjątek: {endpoint} — {e}")
            return None

    # ==================== SNAPSHOT TICKERA ====================

    def get_ticker_details(self, ticker):
        """
        Zwraca snapshot dla konkretnego tickera.
        Backup dla PolygonAPI.get_ticker_details().

        Endpoint: GET /v2/stocks/{ticker}/snapshot
        """
        data = self._get(
            f'/v2/stocks/{ticker}/snapshot',
            params={'feed': 'iex'},  # IEX dostępny na free tier
            cache_ttl=60,
        )

        if not data:
            return {}

        try:
            daily_bar = data.get('dailyBar', {})
            prev_bar  = data.get('prevDailyBar', {})
            latest    = data.get('latestTrade', {})

            price      = latest.get('p', 0) or daily_bar.get('c', 0)
            volume     = daily_bar.get('v', 0)
            prev_close = prev_bar.get('c', 0)
            prev_vol   = prev_bar.get('v', 0)

            change_pct = 0
            if prev_close > 0:
                change_pct = ((price - prev_close) / prev_close) * 100

            volume_ratio = round(volume / max(prev_vol, 1), 2) \
                           if prev_vol > 0 else 1.0

            return {
                'ticker':        ticker,
                'price':         round(float(price), 2),
                'change_pct':    round(float(change_pct), 2),
                'volume':        int(volume),
                'volume_ratio':  volume_ratio,
                'high':          round(float(daily_bar.get('h', price)), 2),
                'low':           round(float(daily_bar.get('l', price)), 2),
                'vwap':          round(float(daily_bar.get('vw', price)), 2),
                'source':        'alpaca',
            }

        except Exception as e:
            logger.warning(f"Alpaca snapshot parse error {ticker}: {e}")
            return {}

    def get_snapshots_batch(self, tickers):
        """
        Zwraca snapshoty dla listy tickerów naraz.
        Efektywniejsze niż wywołanie get_ticker_details() dla każdego z osobna.

        Endpoint: GET /v2/stocks/snapshots
        """
        if not tickers:
            return {}

        # Alpaca przyjmuje max 100 tickerów naraz
        results = {}

        for i in range(0, len(tickers), 100):
            batch = tickers[i:i+100]
            data  = self._get(
                '/v2/stocks/snapshots',
                params={
                    'symbols': ','.join(batch),
                    'feed':    'iex',
                },
                cache_ttl=60,
            )

            if not data:
                continue

            for ticker, snapshot in data.items():
                try:
                    daily_bar = snapshot.get('dailyBar', {})
                    prev_bar  = snapshot.get('prevDailyBar', {})
                    latest    = snapshot.get('latestTrade', {})

                    price      = latest.get('p', 0) or daily_bar.get('c', 0)
                    volume     = daily_bar.get('v', 0)
                    prev_close = prev_bar.get('c', 0)
                    prev_vol   = prev_bar.get('v', 0)

                    change_pct = 0
                    if prev_close > 0:
                        change_pct = ((price - prev_close) / prev_close) * 100

                    volume_ratio = round(volume / max(prev_vol, 1), 2) \
                                   if prev_vol > 0 else 1.0

                    # Filtr cenowy
                    if not (CONFIG['min_price'] <= price <= CONFIG['max_price']):
                        continue

                    results[ticker] = {
                        'ticker':        ticker,
                        'price':         round(float(price), 2),
                        'change_pct':    round(float(change_pct), 2),
                        'volume':        int(volume),
                        'volume_ratio':  volume_ratio,
                        'high':          round(float(daily_bar.get('h', price)), 2),
                        'low':           round(float(daily_bar.get('l', price)), 2),
                        'vwap':          round(float(daily_bar.get('vw', price)), 2),
                        'source':        'alpaca',
                    }

                except Exception as e:
                    logger.warning(f"Alpaca batch parse error {ticker}: {e}")
                    continue

        logger.info(f"Alpaca snapshots: {len(results)} tickerów $"
                    f"{CONFIG['min_price']}-${CONFIG['max_price']}")
        return results

    # ==================== HISTORIA WOLUMENU ====================

    def get_avg_volume_30d(self, ticker):
        """
        Pobiera średnią wolumenu z ostatnich 30 dni.
        Backup dla PolygonAPI.get_avg_volume_30d().

        Endpoint: GET /v2/stocks/{ticker}/bars
        """
        cache_key = f"alpaca_avg_vol_30d:{ticker}"
        cached    = self._cache_get(cache_key)
        if cached:
            return cached

        today     = date.today()
        from_date = (today - timedelta(days=35)).isoformat()
        to_date   = (today - timedelta(days=1)).isoformat()

        data = self._get(
            f'/v2/stocks/{ticker}/bars',
            params={
                'timeframe': '1Day',
                'start':     f'{from_date}T00:00:00Z',
                'end':       f'{to_date}T23:59:59Z',
                'limit':     30,
                'feed':      'iex',
            },
            cache_ttl=3600,
        )

        if not data:
            return 0

        bars    = data.get('bars', [])
        volumes = [b.get('v', 0) for b in bars if b.get('v', 0) > 0]
        avg_vol = int(sum(volumes) / len(volumes)) if volumes else 0

        self._cache_set(cache_key, avg_vol, 3600)
        return avg_vol

    # ==================== STATYSTYKI ====================

    def get_stats(self):
        return {
            'total_calls': self.total_calls,
            'cache_size':  len(self._cache),
        }


# ==================== FUNKCJA BACKUP ====================

def get_ticker_with_fallback(ticker, polygon_api, alpaca_api):
    """
    Próbuje pobrać dane z Polygon — jeśli fail, używa Alpaca jako backup.
    Używana w main.py przy monitorowaniu aktywnych BUY.
    """
    try:
        data = polygon_api.get_ticker_details(ticker)
        if data and data.get('price', 0) > 0:
            return data
    except Exception as e:
        logger.warning(f"Polygon fallback triggered dla {ticker}: {e}")

    # Fallback na Alpaca
    logger.info(f"Używam Alpaca jako backup dla {ticker}")
    return alpaca_api.get_ticker_details(ticker)


# ==================== TEST ====================

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  TEST: Alpaca Market Data API (LIVE)")
    print("="*55)

    try:
        alpaca = AlpacaAPI()
    except ValueError as e:
        print(f"\n❌ {e}")
        exit(1)

    # Test 1: Snapshot jednego tickera
    print("\n✅ Test 1: Snapshot AAPL")
    details = alpaca.get_ticker_details('AAPL')
    if details:
        print(f"  Cena:    ${details.get('price', 0):.2f}")
        print(f"  Zmiana:  {details.get('change_pct', 0):+.2f}%")
        print(f"  Wolumen: {details.get('volume', 0):,}")
        print(f"  VWAP:    ${details.get('vwap', 0):.2f}")
    else:
        print("  Brak danych")

    # Test 2: Batch snapshots
    print("\n✅ Test 2: Batch snapshots (5 tickerów)")
    test_tickers = ['SOUN', 'IONQ', 'RXRX', 'MARA', 'ASTS']
    snapshots    = alpaca.get_snapshots_batch(test_tickers)
    if snapshots:
        for ticker, data in snapshots.items():
            print(f"  {ticker:6s} | ${data['price']:6.2f} | "
                  f"{data['change_pct']:+.1f}% | "
                  f"Vol: {data['volume']:>10,}")
    else:
        print("  Brak danych (weekend lub poza godzinami)")

    # Test 3: Średnia wolumenu 30 dni
    print("\n✅ Test 3: Średnia wolumenu 30 dni SOUN")
    avg_vol = alpaca.get_avg_volume_30d('SOUN')
    print(f"  Średni wolumen 30d: {avg_vol:,}")

    stats = alpaca.get_stats()
    print(f"\n📊 API calls: {stats['total_calls']}")

    print("\n" + "="*55)
    print("  Plik 12 gotowy ✅")
    print("="*55)
