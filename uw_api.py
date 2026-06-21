#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 9: UNUSUAL WHALES API WRAPPER
Zapisz jako uw_api.py w folderze stock-scanner

Zastępuje MockUnusualWhales prawdziwymi danymi z UW API.
Endpointy:
    GET /api/darkpool/recent         → najnowsze transakcje dark pool (cały rynek)
    GET /api/darkpool/{ticker}       → dark pool dla konkretnego tickera
    GET /api/option-trades/flow-alerts → options flow alerts

Dokumentacja: https://api.unusualwhales.com/docs

Historia zmian:
    v1.0 — pierwsza wersja, podstawowe endpointy dark pool i options flow
"""

import requests
import time
from datetime import datetime, date
from config import logger, CONFIG, UNUSUAL_WHALES_KEY, now_chicago


# ==================== KONFIGURACJA ====================

UW_BASE_URL = "https://api.unusualwhales.com"
UW_TIMEOUT  = 15  # sekundy


# ==================== GŁÓWNA KLASA ====================

class UnusualWhalesAPI:
    """
    Wrapper dla Unusual Whales API.
    Interfejs identyczny jak MockUnusualWhales —
    podmiana wymaga tylko zmiany jednej linii w main.py.
    """

    def __init__(self):
        if not UNUSUAL_WHALES_KEY:
            raise ValueError(
                "Brak UNUSUAL_WHALES_KEY w pliku .env\n"
                "Dodaj: UNUSUAL_WHALES_KEY=twój_klucz"
            )

        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {UNUSUAL_WHALES_KEY}',
            'Content-Type':  'application/json',
        })

        # Proste cache w pamięci — unika duplikatów wywołań w tym samym cyklu
        self._cache      = {}
        self._cache_ttl  = {}
        self.total_calls = 0

        logger.info("UnusualWhalesAPI zainicjowany — tryb LIVE")

    # ==================== CACHE ====================

    def _cache_get(self, key):
        """Zwraca dane z cache jeśli nie wygasły"""
        if key in self._cache:
            if time.time() < self._cache_ttl.get(key, 0):
                return self._cache[key]
        return None

    def _cache_set(self, key, value, ttl_seconds=60):
        """Zapisuje dane do cache"""
        self._cache[key]     = value
        self._cache_ttl[key] = time.time() + ttl_seconds

    # ==================== HTTP ====================

    def _get(self, endpoint, params=None, cache_ttl=60):
        """
        Wykonuje GET request do UW API z cache i obsługą błędów.
        """
        cache_key = f"{endpoint}:{str(params)}"
        cached    = self._cache_get(cache_key)
        if cached is not None:
            return cached

        url = f"{UW_BASE_URL}{endpoint}"

        try:
            response = self.session.get(
                url,
                params=params,
                timeout=UW_TIMEOUT,
            )
            self.total_calls += 1

            if response.status_code == 401:
                logger.error("UW API: nieprawidłowy klucz API (401)")
                return None

            if response.status_code == 429:
                logger.warning("UW API: rate limit (429) — czekam 5s")
                time.sleep(5)
                return self._get(endpoint, params, cache_ttl)

            if response.status_code == 404:
                logger.warning(f"UW API: endpoint nie znaleziony (404): {endpoint}")
                return None

            if response.status_code != 200:
                logger.error(f"UW API błąd {response.status_code}: {endpoint}")
                return None

            data = response.json()
            self._cache_set(cache_key, data, cache_ttl)
            return data

        except requests.exceptions.Timeout:
            logger.error(f"UW API timeout: {endpoint}")
            return None
        except Exception as e:
            logger.error(f"UW API wyjątek: {endpoint} — {e}")
            return None

    # ==================== DARK POOL ====================

    def get_dark_pool_flow(self, limit=50):
        """
        Zwraca najnowsze transakcje dark pool z całego rynku.
        Używane w cyklu UW co 1 minutę.

        Endpoint: GET /api/darkpool/recent
        Zwraca listę transakcji z polami:
            ticker, size, price, executed_at, side (B/S/N)
        """
        today  = date.today().isoformat()
        data   = self._get(
            '/api/darkpool/recent',
            params={'date': today, 'limit': limit},
            cache_ttl=60,  # cache 60 sekund
        )

        if not data:
            return []

        results = data.get('data', [])
        flow    = []

        for item in results:
            try:
                ticker  = item.get('ticker', '')
                price   = float(item.get('price', 0))
                size    = int(item.get('size', 0))
                side_raw = item.get('side', 'N')

                # UW używa B/S/N zamiast BUY/SELL/NEUTRAL
                side_map = {'B': 'BUY', 'S': 'SELL', 'N': 'NEUTRAL'}
                side     = side_map.get(side_raw, 'NEUTRAL')

                size_usd = price * size

                # Filtruj po minimalnej wartości transakcji
                if size_usd < 500_000:
                    continue

                # Filtruj po cenie — tylko small-cap $0.01-$15
                if not (CONFIG['min_price'] <= price <= CONFIG['max_price']):
                    continue

                flow.append({
                    'ticker':   ticker,
                    'size_usd': size_usd,
                    'size':     size,
                    'price':    price,
                    'side':     side,
                    'timestamp': item.get('executed_at', ''),
                })

            except Exception as e:
                logger.warning(f"UW dark pool parse error: {e}")
                continue

        logger.info(f"UW dark pool: {len(flow)} transakcji >= $500k "
                    f"(z {len(results)} łącznie)")
        return flow

    def get_dark_pool_ticker(self, ticker):
        """
        Zwraca transakcje dark pool dla konkretnego tickera.
        Używane przy monitorowaniu aktywnych BUY sygnałów.

        Endpoint: GET /api/darkpool/{ticker}
        """
        today = date.today().isoformat()
        data  = self._get(
            f'/api/darkpool/{ticker}',
            params={'date': today},
            cache_ttl=60,
        )

        if not data:
            return []

        results  = data.get('data', [])
        trades   = []

        for item in results:
            try:
                price    = float(item.get('price', 0))
                size     = int(item.get('size', 0))
                side_raw = item.get('side', 'N')
                side_map = {'B': 'BUY', 'S': 'SELL', 'N': 'NEUTRAL'}
                side     = side_map.get(side_raw, 'NEUTRAL')
                size_usd = price * size

                trades.append({
                    'ticker':    ticker,
                    'size_usd':  size_usd,
                    'size':      size,
                    'price':     price,
                    'side':      side,
                    'timestamp': item.get('executed_at', ''),
                })
            except Exception as e:
                logger.warning(f"UW dark pool ticker parse error {ticker}: {e}")
                continue

        return trades

    def get_dominant_dark_pool_side(self, ticker):
        """
        Zwraca dominującą stronę dark pool dla tickera (BUY/SELL/NEUTRAL).
        Używane przez check_retrigger_conditions() w database.py.
        """
        trades = self.get_dark_pool_ticker(ticker)
        if not trades:
            return 'NEUTRAL', 0

        buy_volume  = sum(t['size_usd'] for t in trades if t['side'] == 'BUY')
        sell_volume = sum(t['size_usd'] for t in trades if t['side'] == 'SELL')
        total       = buy_volume + sell_volume

        if total == 0:
            return 'NEUTRAL', 0

        if buy_volume > sell_volume * 1.5:
            return 'BUY', buy_volume
        elif sell_volume > buy_volume * 1.5:
            return 'SELL', sell_volume
        else:
            return 'NEUTRAL', total

    # ==================== OPTIONS FLOW ====================

    def get_options_flow(self, ticker):
        """
        Zwraca options flow dla tickera.
        Interfejs identyczny jak MockUnusualWhales.get_options_flow().

        Endpoint: GET /api/option-trades/flow-alerts
        """
        data = self._get(
            '/api/option-trades/flow-alerts',
            params={'ticker': ticker, 'limit': 20},
            cache_ttl=60,
        )

        if not data:
            return {
                'ticker':        ticker,
                'call_volume':   0,
                'put_volume':    0,
                'call_put_ratio': 1.0,
                'unusual':       False,
                'sentiment':     'neutral',
            }

        results = data.get('data', [])

        call_volume = 0
        put_volume  = 0
        unusual     = False

        for item in results:
            try:
                contract_type = item.get('type', '').lower()
                volume        = int(item.get('volume', 0))
                is_unusual    = item.get('unusual', False)

                if contract_type == 'call':
                    call_volume += volume
                elif contract_type == 'put':
                    put_volume  += volume

                if is_unusual:
                    unusual = True

            except Exception as e:
                logger.warning(f"UW options flow parse error {ticker}: {e}")
                continue

        call_put_ratio = round(call_volume / max(put_volume, 1), 2)

        sentiment = 'neutral'
        if call_put_ratio >= 1.5:
            sentiment = 'bullish'
        elif call_put_ratio <= 0.7:
            sentiment = 'bearish'

        return {
            'ticker':         ticker,
            'call_volume':    call_volume,
            'put_volume':     put_volume,
            'call_put_ratio': call_put_ratio,
            'unusual':        unusual,
            'sentiment':      sentiment,
        }

    # ==================== STATYSTYKI ====================

    def get_stats(self):
        """Zwraca statystyki użycia API"""
        return {
            'total_calls': self.total_calls,
            'cache_size':  len(self._cache),
        }


# ==================== TEST ====================

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  TEST: Unusual Whales API (LIVE)")
    print("="*55)

    try:
        uw = UnusualWhalesAPI()
    except ValueError as e:
        print(f"\n❌ {e}")
        exit(1)

    # Test 1: Dark pool flow (cały rynek)
    print("\n✅ Test 1: Dark pool flow (cały rynek)")
    flow = uw.get_dark_pool_flow(limit=20)
    if flow:
        print(f"  Transakcji >= $500k: {len(flow)}")
        for dp in flow[:5]:
            print(f"  {dp['ticker']:6s} | {dp['side']:7s} | "
                  f"${dp['size_usd']:>12,.0f} | "
                  f"${dp['price']:.2f}")
    else:
        print("  Brak danych (poza godzinami rynkowymi lub błąd API)")

    # Test 2: Dark pool dla konkretnego tickera
    print("\n✅ Test 2: Dark pool dla AAPL")
    ticker_flow = uw.get_dark_pool_ticker('AAPL')
    print(f"  Transakcji: {len(ticker_flow)}")

    side, size = uw.get_dominant_dark_pool_side('AAPL')
    print(f"  Dominująca strona: {side} (${size:,.0f})")

    # Test 3: Options flow
    print("\n✅ Test 3: Options flow dla SOUN")
    options = uw.get_options_flow('SOUN')
    print(f"  Call volume:   {options['call_volume']:,}")
    print(f"  Put volume:    {options['put_volume']:,}")
    print(f"  Call/Put ratio: {options['call_put_ratio']}")
    print(f"  Sentiment:     {options['sentiment']}")
    print(f"  Unusual:       {options['unusual']}")

    # Statystyki
    stats = uw.get_stats()
    print(f"\n📊 API calls: {stats['total_calls']}")

    print("\n" + "="*55)
    print("  Plik 9 gotowy ✅")
    print("="*55)
