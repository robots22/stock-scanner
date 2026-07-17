#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 9: UNUSUAL WHALES API WRAPPER
Zapisz jako uw_api.py w folderze stock-scanner

Endpointy:
    GET /api/darkpool/recent              → dark pool cały rynek
    GET /api/darkpool/{ticker}            → dark pool konkretny ticker
    GET /api/option-trades/flow-alerts    → unusual options activity
    GET /api/market/market-tide           → sentyment rynku
    GET /api/stock/{ticker}/options-volume → call/put ratio

Dokumentacja: https://api.unusualwhales.com/docs

Historia zmian:
    v1.0 — pierwsza wersja, dark pool i options flow
    v2.0 — fix: brak pola 'side' w dark pool (UW nie rozróżnia BUY/SELL)
           — dodano bid/ask porównanie do wnioskowania kierunku
           — dodano logowanie surowych danych (RAW_LOG=True w .env)
           — dodano options flow alerts (/api/option-trades/flow-alerts)
           — dodano market tide (/api/market/market-tide)
           — dodano UW-CLIENT-API-ID header (wymagany przez docs)
"""

import os
import requests
import time
from datetime import datetime, date
from config import logger, CONFIG, UNUSUAL_WHALES_KEY, now_chicago

# Włącz logowanie surowych danych UW — ustaw RAW_LOG=True w .env
RAW_LOG = os.getenv('UW_RAW_LOG', 'False').lower() == 'true'

# KILL SWITCH: UW_ENABLED=false w .env wylacza wszystkie API calls
# Zwraca puste dane bez uderzania w subskrypcje UW.
# Zmieniaj tylko przez .env, kod zostaje bez zmian dla latwego przywrocenia.
UW_ENABLED = os.getenv('UW_ENABLED', 'True').lower() != 'false'

UW_BASE_URL = "https://api.unusualwhales.com"
UW_TIMEOUT  = 15


class UnusualWhalesAPI:
    """
    Wrapper dla Unusual Whales API v2.0

    KILL SWITCH: gdy UW_ENABLED=false w .env, klasa dziala jako no-op —
    kazda metoda zwraca pusta liste/dict, zero HTTP requests.
    Kod uzywajacy tej klasy w main.py NIE wymaga zmian.
    """

    def __init__(self):
        self.enabled = UW_ENABLED

        if not self.enabled:
            self.session = None
            self._cache     = {}
            self._cache_ttl = {}
            self.total_calls = 0
            logger.info("UnusualWhalesAPI: KILL SWITCH aktywny (UW_ENABLED=false) — no-op mode")
            return

        if not UNUSUAL_WHALES_KEY:
            raise ValueError(
                "Brak UNUSUAL_WHALES_KEY w pliku .env"
            )

        self.session = requests.Session()
        self.session.headers.update({
            'Authorization':    f'Bearer {UNUSUAL_WHALES_KEY}',
            'Content-Type':     'application/json',
            'UW-CLIENT-API-ID': '100001',  # wymagany przez docs
        })

        self._cache     = {}
        self._cache_ttl = {}
        self.total_calls = 0

        logger.info("UnusualWhalesAPI v2.0 zainicjowany — tryb LIVE")

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
        # KILL SWITCH — zero HTTP calls
        if not self.enabled:
            return None

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
                logger.warning(f"UW API: 404: {endpoint}")
                return None

            if response.status_code != 200:
                logger.error(f"UW API błąd {response.status_code}: {endpoint}")
                return None

            data = response.json()

            # Logowanie surowych danych (debug)
            if RAW_LOG:
                results = data.get('data', [])
                if results:
                    logger.info(f"UW RAW [{endpoint}] pierwsze 2 rekordy:")
                    for item in results[:2]:
                        logger.info(f"  {item}")

            self._cache_set(cache_key, data, cache_ttl)
            return data

        except requests.exceptions.Timeout:
            logger.error(f"UW API timeout: {endpoint}")
            return None
        except Exception as e:
            logger.error(f"UW API wyjątek: {endpoint} — {e}")
            return None

    # ==================== DARK POOL ====================

    @staticmethod
    def _infer_side(price, ask, bid):
        """
        Wnioskuje kierunek transakcji z porównania ceny fill z bid/ask.
        UW nie dostarcza pola 'side' bezpośrednio.

        Logika:
        - fill >= ask → aggressive buyer → BUY
        - fill <= bid → aggressive seller → SELL
        - fill między bid/ask → NEUTRAL
        """
        if ask and bid and ask > bid:
            if price >= ask:
                return 'BUY'
            elif price <= bid:
                return 'SELL'
        return 'NEUTRAL'

    def get_dark_pool_flow(self, limit=50):
        """
        Zwraca najnowsze transakcje dark pool z całego rynku.

        UWAGA: UW nie dostarcza pola 'side' (BUY/SELL).
        Kierunek wnioskujemy z porównania ceny fill z bid/ask.

        Endpoint: GET /api/darkpool/recent
        """
        today = date.today().isoformat()
        data  = self._get(
            '/api/darkpool/recent',
            params={'date': today, 'limit': limit},
            cache_ttl=60,
        )

        if not data:
            return []

        results = data.get('data', [])
        flow    = []

        for item in results:
            try:
                ticker   = item.get('ticker', '')
                price    = float(item.get('price', 0) or 0)
                size     = int(item.get('size', 0) or 0)
                ask      = float(item.get('ask', 0) or 0)
                bid      = float(item.get('bid', 0) or 0)
                size_usd = price * size

                # Filtruj po minimalnej wartości
                if size_usd < 500_000:
                    continue

                # Filtruj po cenie small-cap
                if not (CONFIG['min_price'] <= price <= CONFIG['max_price']):
                    continue

                # Wnioskuj kierunek z bid/ask
                side = self._infer_side(price, ask, bid)

                flow.append({
                    'ticker':    ticker,
                    'size_usd':  size_usd,
                    'size':      size,
                    'price':     price,
                    'ask':       ask,
                    'bid':       bid,
                    'side':      side,
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
        """
        today = date.today().isoformat()
        data  = self._get(
            f'/api/darkpool/{ticker}',
            params={'date': today},
            cache_ttl=60,
        )

        if not data:
            return []

        results = data.get('data', [])
        trades  = []

        for item in results:
            try:
                price    = float(item.get('price', 0) or 0)
                size     = int(item.get('size', 0) or 0)
                ask      = float(item.get('ask', 0) or 0)
                bid      = float(item.get('bid', 0) or 0)
                size_usd = price * size
                side     = self._infer_side(price, ask, bid)

                trades.append({
                    'ticker':    ticker,
                    'size_usd':  size_usd,
                    'size':      size,
                    'price':     price,
                    'ask':       ask,
                    'bid':       bid,
                    'side':      side,
                    'timestamp': item.get('executed_at', ''),
                })
            except Exception as e:
                logger.warning(f"UW dark pool ticker parse error {ticker}: {e}")
                continue

        return trades

    def get_dominant_dark_pool_side(self, ticker):
        """
        Zwraca dominującą stronę dark pool dla tickera.
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

    # ==================== OPTIONS FLOW ALERTS ====================

    def get_flow_alerts(self, min_premium=50_000, limit=20):
        """
        Zwraca unusual options activity z całego rynku.
        To jest kluczowy sygnał smart money.

        Endpoint: GET /api/option-trades/flow-alerts
        Params:
            min_premium  — minimalna wartość premium ($)
            limit        — ile alertów zwrócić
        """
        data = self._get(
            '/api/option-trades/flow-alerts',
            params={
                'min_premium': min_premium,
                'limit':       limit,
            },
            cache_ttl=60,
        )

        if not data:
            return []

        results = data.get('data', [])
        alerts  = []

        for item in results:
            try:
                ticker    = item.get('ticker_symbol', '') or item.get('ticker', '')
                opt_type  = item.get('type', '').lower()  # 'call' or 'put'
                premium   = float(item.get('total_premium', 0) or 0)
                volume    = int(item.get('total_size', 0) or 0)
                oi        = int(item.get('open_interest', 0) or 0)
                price     = float(item.get('underlying_price', 0) or 0)

                # Filtruj po cenie small-cap
                if price > 0 and not (CONFIG['min_price'] <= price <= CONFIG['max_price']):
                    continue

                alerts.append({
                    'ticker':   ticker,
                    'type':     opt_type,  # 'call' lub 'put'
                    'premium':  premium,
                    'volume':   volume,
                    'oi':       oi,
                    'price':    price,
                    'bullish':  opt_type == 'call',
                })

            except Exception as e:
                logger.warning(f"UW flow alert parse error: {e}")
                continue

        logger.info(f"UW flow alerts: {len(alerts)} alertów >= ${min_premium:,}")
        return alerts

    def get_flow_alerts_for_ticker(self, ticker, min_premium=10_000):
        """
        Zwraca unusual options dla konkretnego tickera.
        Używane przy pre-filtrze i analizie Claude'a.
        """
        data = self._get(
            '/api/option-trades/flow-alerts',
            params={
                'ticker_symbol': ticker,
                'min_premium':   min_premium,
                'limit':         10,
            },
            cache_ttl=60,
        )

        if not data:
            return []

        return data.get('data', [])

    # ==================== OPTIONS FLOW (per ticker) ====================

    def get_options_flow(self, ticker):
        """
        Zwraca call/put ratio i sentyment opcyjny dla tickera.
        Używane przez monitoring aktywnych BUY.
        """
        data = self._get(
            f'/api/stock/{ticker}/options-volume',
            cache_ttl=60,
        )

        if not data:
            # Fallback na flow-alerts
            return self._options_flow_fallback(ticker)

        item = data.get('data', {})
        if isinstance(item, list) and item:
            item = item[0]

        try:
            call_volume     = int(item.get('call_volume', 0) or 0)
            put_volume      = int(item.get('put_volume', 0) or 0)

            # ask_side = aggressive buyers (bullish)
            # bid_side = aggressive sellers (bearish)
            call_ask_side   = int(item.get('call_volume_ask_side', 0) or 0)
            call_bid_side   = int(item.get('call_volume_bid_side', 0) or 0)
            put_ask_side    = int(item.get('put_volume_ask_side', 0) or 0)
            put_bid_side    = int(item.get('put_volume_bid_side', 0) or 0)

            # Bullish premium = calls bought (ask side) + puts sold (bid side)
            bullish_premium = float(item.get('bullish_premium', 0) or 0)
            bearish_premium = float(item.get('bearish_premium', 0) or 0)

            call_put = round(call_volume / max(put_volume, 1), 2)

            # Kierunek oparty na ask/bid side (bardziej wiarygodny)
            net_call_direction = call_ask_side - call_bid_side
            net_put_direction  = put_ask_side  - put_bid_side

            if bullish_premium > bearish_premium * 1.5:
                sentiment = 'bullish'
            elif bearish_premium > bullish_premium * 1.5:
                sentiment = 'bearish'
            elif net_call_direction > 0:
                sentiment = 'bullish'
            elif net_call_direction < 0:
                sentiment = 'bearish'
            else:
                sentiment = 'neutral'

            unusual = (
                call_put > 3.0 or call_put < 0.3 or
                abs(bullish_premium - bearish_premium) > 50_000
            )

            return {
                'ticker':           ticker,
                'call_volume':      call_volume,
                'put_volume':       put_volume,
                'call_put_ratio':   call_put,
                'call_ask_side':    call_ask_side,
                'call_bid_side':    call_bid_side,
                'bullish_premium':  bullish_premium,
                'bearish_premium':  bearish_premium,
                'unusual':          unusual,
                'sentiment':        sentiment,
            }
        except Exception:
            return self._options_flow_fallback(ticker)

    def _options_flow_fallback(self, ticker):
        """Fallback gdy /options-volume nie działa"""
        alerts = self.get_flow_alerts_for_ticker(ticker)

        call_volume = sum(
            a.get('volume', 0) for a in alerts
            if a.get('type') == 'call'
        )
        put_volume = sum(
            a.get('volume', 0) for a in alerts
            if a.get('type') == 'put'
        )
        call_put = round(call_volume / max(put_volume, 1), 2)

        sentiment = 'neutral'
        if call_put >= 1.5:
            sentiment = 'bullish'
        elif call_put <= 0.7:
            sentiment = 'bearish'

        return {
            'ticker':         ticker,
            'call_volume':    call_volume,
            'put_volume':     put_volume,
            'call_put_ratio': call_put,
            'unusual':        bool(alerts),
            'sentiment':      sentiment,
        }

    # ==================== MARKET TIDE ====================

    def get_market_tide(self):
        """
        Zwraca ogólny sentyment rynku (Market Tide).
        Pozytywny net premium call → bullish rynek
        Negatywny → bearish

        Endpoint: GET /api/market/market-tide
        """
        data = self._get(
            '/api/market/market-tide',
            params={'interval_5m': True},
            cache_ttl=300,  # cache 5 minut
        )

        if not data:
            return None

        results = data.get('data', [])
        if not results:
            return None

        # Weź najnowszy punkt
        latest = results[-1] if results else {}

        try:
            net_call = float(latest.get('net_call_premium', 0) or 0)
            net_put  = float(latest.get('net_put_premium', 0) or 0)
            net      = net_call - net_put

            return {
                'net_call_premium': net_call,
                'net_put_premium':  net_put,
                'net':              net,
                'sentiment':        'bullish' if net > 0 else 'bearish',
                'timestamp':        latest.get('timestamp', ''),
            }
        except Exception as e:
            logger.warning(f"UW market tide parse error: {e}")
            return None

    # ==================== TICKERS Z FLOW ALERTS ====================

    def get_tickers_with_unusual_flow(self, min_premium=100_000, limit=30):
        """
        Zwraca listę tickerów z unusual options activity.
        Używane przez pre-filter jako dodatkowy sygnał.

        Tickery z unusual flow dostają bonus w pre-filtrze.
        """
        alerts = self.get_flow_alerts(
            min_premium=min_premium,
            limit=limit
        )

        ticker_flow = {}
        for alert in alerts:
            ticker = alert.get('ticker', '')
            if not ticker:
                continue
            if ticker not in ticker_flow:
                ticker_flow[ticker] = {
                    'ticker':        ticker,
                    'call_premium':  0,
                    'put_premium':   0,
                    'total_premium': 0,
                    'bullish':       False,
                }
            premium = alert.get('premium', 0)
            ticker_flow[ticker]['total_premium'] += premium
            if alert.get('bullish'):
                ticker_flow[ticker]['call_premium'] += premium
            else:
                ticker_flow[ticker]['put_premium'] += premium

        # Oznacz jako bullish/bearish
        for t in ticker_flow.values():
            t['bullish'] = t['call_premium'] > t['put_premium']

        result = list(ticker_flow.values())
        logger.info(f"UW unusual flow: {len(result)} tickerów "
                    f"z premium >= ${min_premium:,}")
        return result

    # ==================== STATYSTYKI ====================

    def get_stats(self):
        return {
            'total_calls': self.total_calls,
            'cache_size':  len(self._cache),
        }


# ==================== TEST ====================

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  TEST: Unusual Whales API v2.0 (LIVE)")
    print("="*55)

    try:
        uw = UnusualWhalesAPI()
    except ValueError as e:
        print(f"\n❌ {e}")
        exit(1)

    # Test 1: Dark pool z bid/ask
    print("\n✅ Test 1: Dark pool flow (z bid/ask inference)")
    flow = uw.get_dark_pool_flow(limit=20)
    if flow:
        print(f"  Transakcji >= $500k: {len(flow)}")
        for dp in flow[:3]:
            print(f"  {dp['ticker']:6s} | {dp['side']:7s} | "
                  f"${dp['size_usd']:>12,.0f} | "
                  f"ask:{dp['ask']:.2f} bid:{dp['bid']:.2f} "
                  f"fill:{dp['price']:.2f}")
    else:
        print("  Brak danych (poza godzinami lub brak transakcji >= $500k)")

    # Test 2: Flow alerts
    print("\n✅ Test 2: Unusual options flow alerts")
    alerts = uw.get_flow_alerts(min_premium=50_000, limit=5)
    if alerts:
        for a in alerts[:3]:
            print(f"  {a['ticker']:6s} | {'CALL' if a['bullish'] else 'PUT ':4s} | "
                  f"${a['premium']:>10,.0f} | vol: {a['volume']:,}")
    else:
        print("  Brak alertów")

    # Test 3: Tickers z unusual flow
    print("\n✅ Test 3: Tickers z unusual flow (dla pre-filtra)")
    tickers = uw.get_tickers_with_unusual_flow(min_premium=100_000, limit=10)
    if tickers:
        for t in tickers[:5]:
            direction = "BULLISH" if t['bullish'] else "BEARISH"
            print(f"  {t['ticker']:6s} | {direction:7s} | "
                  f"${t['total_premium']:>10,.0f}")
    else:
        print("  Brak tickerów")

    # Test 4: Market tide
    print("\n✅ Test 4: Market Tide (sentyment rynku)")
    tide = uw.get_market_tide()
    if tide:
        print(f"  Sentyment: {tide['sentiment'].upper()}")
        print(f"  Net call premium: ${tide['net_call_premium']:,.0f}")
        print(f"  Net put premium:  ${tide['net_put_premium']:,.0f}")
    else:
        print("  Brak danych")

    # Test 5: Options flow GALT
    print("\n✅ Test 5: Options flow GALT")
    opts = uw.get_options_flow('GALT')
    print(f"  Call/Put ratio: {opts['call_put_ratio']}")
    print(f"  Sentiment:      {opts['sentiment']}")
    print(f"  Unusual:        {opts['unusual']}")

    stats = uw.get_stats()
    print(f"\n📊 API calls: {stats['total_calls']}")
    print("\n" + "="*55)
    print("  uw_api.py v2.0 gotowy ✅")
    print("="*55)
