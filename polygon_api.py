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

                prev_close   = round(float(prev.get('c', 0)), 2)
                day_open     = round(float(day.get('o', price)), 2)
                gap_pct      = round((day_open - prev_close) / prev_close * 100, 2) \
                               if prev_close > 0 else 0.0

                universe.append({
                    'ticker':         ticker,
                    'price':          round(float(price), 2),
                    'change_pct':     round(float(change_pct), 2),
                    'change_dollar':  round(float(item.get('todaysChange', 0)), 2),
                    'volume':         int(volume),
                    'avg_volume_30d': int(prev_volume),
                    'volume_ratio':   volume_ratio,
                    'high':           round(float(day.get('h', price)), 2),
                    'low':            round(float(day.get('l', price)), 2),
                    'open':           day_open,
                    'vwap':           round(float(day.get('vw', price)), 2),
                    'prev_close':     prev_close,
                    'gap_pct':        gap_pct,
                    'earnings':       None,
                    'insider':        None,
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

    def get_float_and_gap(self, ticker):
        """
        Pobiera float (shares outstanding) i gap od poprzedniego zamkniecia.
        
        Float: /v3/reference/tickers/{ticker}
        Gap:   /v2/aggs/ticker/{ticker}/prev
        
        Zwraca:
          float_shares  -- ilosc akcji w obiegu (shares outstanding)
          gap_pct       -- gap od poprzedniego zamkniecia (%)
          prev_close    -- poprzednie zamkniecie
        """
        result = {
            'float_shares': None,
            'gap_pct':      0.0,
            'prev_close':   0.0,
        }

        # Float z reference endpoint
        try:
            ref_data = self._get(
                f'/v3/reference/tickers/{ticker}',
                cache_ttl=3600,  # cache 1h - float sie nie zmienia czesto
            )
            if ref_data and ref_data.get('results'):
                r = ref_data['results']
                shares = r.get('weighted_shares_outstanding') or \
                         r.get('share_class_shares_outstanding')
                if shares:
                    result['float_shares'] = int(shares)
        except Exception as e:
            logger.debug(f"Float error {ticker}: {e}")

        # Gap z prev day endpoint
        try:
            prev_data = self._get(
                f'/v2/aggs/ticker/{ticker}/prev',
                cache_ttl=300,  # cache 5 min
            )
            if prev_data and prev_data.get('results'):
                prev = prev_data['results'][0]
                prev_close = float(prev.get('c', 0))
                result['prev_close'] = prev_close

                # Pobierz aktualna cene otwarcia
                snap = self._get(
                    f'/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}',
                    cache_ttl=30,
                )
                if snap and snap.get('ticker'):
                    day_open = snap['ticker'].get('day', {}).get('o', 0)
                    if day_open and prev_close:
                        result['gap_pct'] = round(
                            (day_open - prev_close) / prev_close * 100, 2
                        )
        except Exception as e:
            logger.debug(f"Gap error {ticker}: {e}")

        return result

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

    def get_rsi(self, ticker, window=14, timespan='minute', limit=1):
        """
        Pobiera RSI dla tickera z Polygon Technical Indicators API.
        Dostępne w Starter plan.
        
        Parametry:
          window   — okres RSI (domyślnie 14)
          timespan — 'minute', 'hour', 'day'
          limit    — ile wartości zwrócić (1 = tylko ostatnia)
        
        Zwraca:
          float — ostatnia wartość RSI (0-100) lub None
        """
        try:
            url = f"{POLYGON_BASE_URL}/v1/indicators/rsi/{ticker}"
            params = {
                'apiKey':      POLYGON_API_KEY,
                'timespan':    timespan,
                'window':      window,
                'series_type': 'close',
                'order':       'desc',
                'limit':       limit,
            }
            response = self.session.get(url, params=params, timeout=10)
            if response.status_code != 200:
                return None
            data = response.json()
            results = data.get('results', {}).get('values', [])
            if results:
                return round(float(results[0].get('value', 0)), 2)
            return None
        except Exception as e:
            logger.warning(f"RSI error {ticker}: {e}")
            return None

    def get_ema(self, ticker, window=9, timespan='minute', limit=2):
        """
        Pobiera EMA dla tickera.
        Zwraca (ema9, ema21) lub (None, None).
        """
        try:
            results = []
            for w in [window, 21]:
                url = f"{POLYGON_BASE_URL}/v1/indicators/ema/{ticker}"
                params = {
                    'apiKey':      POLYGON_API_KEY,
                    'timespan':    timespan,
                    'window':      w,
                    'series_type': 'close',
                    'order':       'desc',
                    'limit':       1,
                }
                response = self.session.get(url, params=params, timeout=10)
                if response.status_code == 200:
                    data  = response.json()
                    vals  = data.get('results', {}).get('values', [])
                    results.append(round(float(vals[0]['value']), 4) if vals else None)
                else:
                    results.append(None)
            return results[0], results[1]  # ema9, ema21
        except Exception as e:
            logger.warning(f"EMA error {ticker}: {e}")
            return None, None

    def get_ticker_type(self, ticker):
        """
        Pobiera typ tickera z Polygon Reference API.
        Zwraca: 'CS' (Common Stock), 'ETF', 'WARRANT', 'RIGHT', 'UNIT' itd.
        
        Używane do filtrowania warranty/prawa/unity przez oficjalny typ
        zamiast ręcznego sprawdzania nazwy tickera.
        """
        try:
            # Cache ticker types żeby nie odpytywać wielokrotnie
            cache_key = f"type_{ticker}"
            if cache_key in self._cache:
                return self._cache[cache_key]

            url = f"{POLYGON_BASE_URL}/v3/reference/tickers/{ticker}"
            params = {'apiKey': POLYGON_API_KEY}
            response = self.session.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data        = response.json()
                ticker_type = data.get('results', {}).get('type', 'CS')
                # Cache na 24 godziny — typ tickera się nie zmienia
                self._cache[cache_key] = ticker_type
                return ticker_type
            return 'CS'  # domyślnie traktuj jako common stock
        except Exception as e:
            logger.warning(f"Ticker type error {ticker}: {e}")
            return 'CS'

    def get_news_with_source(self, ticker, limit=3):
        """
        Pobiera newsy z informacją o źródle.
        Rozróżnia Reuters/Bloomberg (wiarygodne) od PRNewswire (PR).
        """
        try:
            url = f"{POLYGON_BASE_URL}/v2/reference/news"
            params = {
                'apiKey':      POLYGON_API_KEY,
                'ticker':      ticker,
                'limit':       limit,
                'sort':        'published_utc',
                'order':       'desc',
            }
            response = self.session.get(url, params=params, timeout=10)
            if response.status_code != 200:
                return []

            results = response.json().get('results', [])
            news    = []
            for item in results:
                publisher = item.get('publisher', {})
                source    = publisher.get('name', '').lower()

                # Ocena jakości źródła
                if any(s in source for s in ['reuters', 'bloomberg', 'ap ', 'associated press']):
                    quality = 'premium'
                    quality_score = 3
                elif any(s in source for s in ['benzinga', 'marketwatch', 'seeking alpha', 'the street']):
                    quality = 'standard'
                    quality_score = 2
                elif any(s in source for s in ['pr newswire', 'business wire', 'globe newswire', 'accesswire']):
                    quality = 'pr'
                    quality_score = 1
                else:
                    quality = 'standard'
                    quality_score = 2

                news.append({
                    'title':         item.get('title', ''),
                    'published_utc': item.get('published_utc', ''),
                    'source':        publisher.get('name', ''),
                    'quality':       quality,
                    'quality_score': quality_score,
                    'sentiment':     item.get('insights', [{}])[0].get('sentiment', '') 
                                     if item.get('insights') else '',
                    'url':           item.get('article_url', ''),
                })
            return news
        except Exception as e:
            logger.warning(f"News error {ticker}: {e}")
            return []


    def get_atr(self, ticker, window=14, timespan='minute', multiplier=5):
        """
        Oblicza ATR (Average True Range) z barów OHLC.
        Używane do dynamicznego stop-loss.

        ATR = średnia z (High - Low) za ostatnie N barów
        
        Parametry:
          window     — liczba barów do obliczenia ATR (domyślnie 14)
          timespan   — 'minute', 'hour', 'day'
          multiplier — rozmiar baru (domyślnie 5 = 5-minutowe bary)

        Zwraca:
          float — wartość ATR lub None
        """
        try:
            from datetime import date, timedelta
            today     = date.today().isoformat()
            yesterday = (date.today() - timedelta(days=1)).isoformat()

            url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{yesterday}/{today}"
            params = {
                'apiKey':   POLYGON_API_KEY,
                'adjusted': 'true',
                'sort':     'desc',
                'limit':    window + 1,
            }
            response = self.session.get(url, params=params, timeout=10)
            if response.status_code != 200:
                return None

            results = response.json().get('results', [])
            if len(results) < 2:
                return None

            # True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
            true_ranges = []
            for i in range(len(results) - 1):
                bar       = results[i]
                prev_bar  = results[i + 1]
                high      = bar.get('h', 0)
                low       = bar.get('l', 0)
                prev_close = prev_bar.get('c', 0)

                tr = max(
                    high - low,
                    abs(high - prev_close),
                    abs(low  - prev_close),
                )
                true_ranges.append(tr)

            if not true_ranges:
                return None

            atr = sum(true_ranges[:window]) / min(len(true_ranges), window)
            return round(atr, 4)

        except Exception as e:
            logger.warning(f"ATR error {ticker}: {e}")
            return None

    def calculate_stop_loss(self, ticker, entry_price, vwap=None, lod=None):
        """
        Oblicza dynamiczny stop-loss oparty na ATR i VWAP.

        Logika:
          stop_vwap = vwap - mały bufor (VWAP jako wsparcie)
          stop_atr  = entry - 1.5x ATR (zmienność rynku)
          stop_pct  = entry * 0.96 (max 4% strata)
          
          stop_loss = max(stop_vwap, stop_atr, stop_pct)
          → najciaśniejszy stop który ma sens

        Zwraca:
          dict z stop_loss, take_profit, rr_ratio, basis
        """
        atr = self.get_atr(ticker)

        # Oblicz kandydatów na stop-loss
        stop_candidates = {}

        if vwap and vwap > 0:
            stop_vwap = vwap - (vwap * 0.005)  # 0.5% poniżej VWAP
            stop_candidates['vwap'] = stop_vwap

        if atr and atr > 0:
            stop_atr = entry_price - (1.5 * atr)
            stop_candidates['atr'] = stop_atr

        # Stały % stop (backup)
        stop_pct = entry_price * 0.96  # max 4%
        stop_candidates['pct_4'] = stop_pct

        # Wybierz najwyższy (najciaśniejszy) stop który jest sensowny
        valid_stops = {k: v for k, v in stop_candidates.items()
                       if entry_price * 0.90 <= v < entry_price}

        if not valid_stops:
            stop_loss = stop_pct
            basis     = 'pct_4'
        else:
            basis     = max(valid_stops, key=valid_stops.get)
            stop_loss = valid_stops[basis]

        stop_loss = round(stop_loss, 2)
        risk      = entry_price - stop_loss

        # Take profit = 2:1 R/R (risk/reward)
        rr_ratio    = 2.0
        take_profit = round(entry_price + (risk * rr_ratio), 2)
        risk_pct    = round((risk / entry_price) * 100, 1)
        reward_pct  = round((take_profit - entry_price) / entry_price * 100, 1)

        return {
            'stop_loss':   stop_loss,
            'take_profit': take_profit,
            'rr_ratio':    rr_ratio,
            'risk_pct':    risk_pct,
            'reward_pct':  reward_pct,
            'basis':       basis,
            'atr':         atr,
        }


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
