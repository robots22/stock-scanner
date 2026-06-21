#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 11: FINNHUB API WRAPPER
Zapisz jako finnhub_api.py w folderze stock-scanner

Zastępuje MockFinnhub prawdziwymi danymi z Finnhub.io (free tier).
Free tier: 60 API calls/minutę — wystarczający dla naszego systemu.

Endpointy:
    GET /calendar/earnings      → earnings calendar
    GET /stock/insider-transactions → insider transactions
    GET /stock/earnings         → historia earnings (surprise)

Dokumentacja: https://finnhub.io/docs/api

Historia zmian:
    v1.0 — pierwsza wersja, earnings calendar i insider transactions
"""

import requests
import time
from datetime import date, timedelta
from config import logger, CONFIG, FINNHUB_API_KEY, now_chicago


# ==================== KONFIGURACJA ====================

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
FINNHUB_TIMEOUT  = 10


# ==================== GŁÓWNA KLASA ====================

class FinnhubAPI:
    """
    Wrapper dla Finnhub API (free tier).
    Interfejs identyczny jak MockFinnhub —
    podmiana wymaga tylko zmiany jednej linii w main.py.
    """

    def __init__(self):
        if not FINNHUB_API_KEY:
            raise ValueError(
                "Brak FINNHUB_API_KEY w pliku .env\n"
                "Zarejestruj się na finnhub.io (darmowe) i dodaj klucz."
            )

        self.session = requests.Session()
        self.session.headers.update({
            'X-Finnhub-Token': FINNHUB_API_KEY,
        })

        # Cache w pamięci
        self._cache     = {}
        self._cache_ttl = {}
        self.total_calls = 0

        # Rate limit — 60 calls/minutę na free tier
        self._last_call_time = 0
        self._min_interval   = 1.0  # 1 sekunda między wywołaniami

        logger.info("FinnhubAPI zainicjowany — tryb LIVE (free tier)")

    # ==================== CACHE ====================

    def _cache_get(self, key):
        if key in self._cache:
            if time.time() < self._cache_ttl.get(key, 0):
                return self._cache[key]
        return None

    def _cache_set(self, key, value, ttl_seconds=3600):
        self._cache[key]     = value
        self._cache_ttl[key] = time.time() + ttl_seconds

    # ==================== HTTP ====================

    def _get(self, endpoint, params=None, cache_ttl=3600):
        """
        Wykonuje GET request do Finnhub API z cache i rate limiting.
        """
        params = params or {}
        params['token'] = FINNHUB_API_KEY

        cache_key = f"{endpoint}:{str(sorted(params.items()))}"
        cached    = self._cache_get(cache_key)
        if cached is not None:
            return cached

        # Rate limit — nie przekraczaj 60 calls/minutę
        elapsed = time.time() - self._last_call_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        url = f"{FINNHUB_BASE_URL}{endpoint}"

        try:
            response = self.session.get(
                url,
                params=params,
                timeout=FINNHUB_TIMEOUT,
            )
            self.total_calls      += 1
            self._last_call_time   = time.time()

            if response.status_code == 401:
                logger.error("Finnhub API: nieprawidłowy klucz (401)")
                return None

            if response.status_code == 403:
                logger.warning(f"Finnhub API: brak dostępu (403) — "
                               f"endpoint może wymagać płatnego planu: "
                               f"{endpoint}")
                return None

            if response.status_code == 429:
                logger.warning("Finnhub API: rate limit (429) — czekam 60s")
                time.sleep(60)
                return self._get(endpoint, params, cache_ttl)

            if response.status_code != 200:
                logger.error(f"Finnhub API błąd {response.status_code}: "
                             f"{endpoint}")
                return None

            data = response.json()
            self._cache_set(cache_key, data, cache_ttl)
            return data

        except requests.exceptions.Timeout:
            logger.error(f"Finnhub API timeout: {endpoint}")
            return None
        except Exception as e:
            logger.error(f"Finnhub API wyjątek: {endpoint} — {e}")
            return None

    # ==================== EARNINGS CALENDAR ====================

    def get_earnings_calendar(self, ticker):
        """
        Zwraca nadchodzące earnings dla tickera.
        Interfejs identyczny jak MockFinnhub.get_earnings_calendar().

        Endpoint: GET /calendar/earnings
        """
        today    = date.today()
        from_date = today.isoformat()
        to_date   = (today + timedelta(days=14)).isoformat()

        data = self._get(
            '/calendar/earnings',
            params={
                'from':   from_date,
                'to':     to_date,
                'symbol': ticker,
            },
            cache_ttl=3600,  # cache 1 godzina
        )

        if not data:
            return None

        earnings_list = data.get('earningsCalendar', [])

        if not earnings_list:
            return None

        # Weź najbliższy earnings
        earnings = earnings_list[0]

        earnings_date = earnings.get('date', '')
        try:
            earnings_dt  = date.fromisoformat(earnings_date)
            days_until   = (earnings_dt - today).days
        except Exception:
            days_until = 99

        if days_until < 0 or days_until > 14:
            return None

        # Pobierz historię earnings (surprise)
        previous_surprise = self._get_earnings_surprise(ticker)

        return {
            'ticker':            ticker,
            'date':              earnings_date,
            'days_until':        days_until,
            'estimate_eps':      earnings.get('epsEstimate', 0) or 0,
            'previous_surprise': previous_surprise,
        }

    def _get_earnings_surprise(self, ticker):
        """
        Pobiera wynik ostatniego raportu earnings (beat/miss/inline).
        """
        data = self._get(
            '/stock/earnings',
            params={'symbol': ticker, 'limit': 1},
            cache_ttl=86400,  # cache 24 godziny
        )

        if not data or not isinstance(data, list) or len(data) == 0:
            return 'unknown'

        last = data[0]
        actual   = last.get('actual', None)
        estimate = last.get('estimate', None)

        if actual is None or estimate is None:
            return 'unknown'

        diff = actual - estimate

        if diff > 0.01:
            return 'beat'
        elif diff < -0.01:
            return 'miss'
        else:
            return 'inline'

    # ==================== INSIDER TRANSACTIONS ====================

    def get_insider_transactions(self, ticker):
        """
        Zwraca ostatnie insider transactions dla tickera.
        Interfejs identyczny jak MockFinnhub.get_insider_transactions().

        Endpoint: GET /stock/insider-transactions
        """
        data = self._get(
            '/stock/insider-transactions',
            params={'symbol': ticker},
            cache_ttl=3600,
        )

        if not data:
            return None

        transactions = data.get('data', [])
        if not transactions:
            return None

        # Filtruj po ostatnich 7 dniach
        today = date.today()
        recent = []

        for t in transactions:
            try:
                tx_date  = date.fromisoformat(t.get('transactionDate', ''))
                days_ago = (today - tx_date).days

                if days_ago > 7:
                    continue

                tx_type  = t.get('transactionCode', '')
                shares   = abs(int(t.get('share', 0)))
                price    = float(t.get('transactionPrice', 0) or 0)
                value    = shares * price

                # Tylko znaczące transakcje
                if value < 10_000:
                    continue

                # P = Purchase (BUY), S = Sale (SELL)
                if tx_type == 'P':
                    side = 'BUY'
                elif tx_type == 'S':
                    side = 'SELL'
                else:
                    continue

                recent.append({
                    'ticker':    ticker,
                    'type':      side,
                    'value_usd': int(value),
                    'shares':    shares,
                    'price':     price,
                    'person':    t.get('name', 'Insider'),
                    'days_ago':  days_ago,
                })

            except Exception as e:
                logger.warning(f"Finnhub insider parse error {ticker}: {e}")
                continue

        if not recent:
            return None

        # Zwróć największą transakcję
        recent.sort(key=lambda x: x['value_usd'], reverse=True)
        return recent[0]

    # ==================== STATYSTYKI ====================

    def get_stats(self):
        return {
            'total_calls': self.total_calls,
            'cache_size':  len(self._cache),
        }


# ==================== TEST ====================

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  TEST: Finnhub API (LIVE)")
    print("="*55)

    try:
        fh = FinnhubAPI()
    except ValueError as e:
        print(f"\n❌ {e}")
        exit(1)

    # Test 1: Earnings calendar
    print("\n✅ Test 1: Earnings calendar")
    test_tickers = ['AAPL', 'NVDA', 'SOUN', 'IONQ', 'RXRX']
    for ticker in test_tickers:
        earnings = fh.get_earnings_calendar(ticker)
        if earnings:
            print(f"  {ticker}: earnings za {earnings['days_until']} dni "
                  f"| EPS est: ${earnings['estimate_eps']:.2f} "
                  f"| poprzedni: {earnings['previous_surprise']}")
        else:
            print(f"  {ticker}: brak earnings w ciągu 14 dni")

    # Test 2: Insider transactions
    print("\n✅ Test 2: Insider transactions (ostatnie 7 dni)")
    for ticker in test_tickers[:3]:
        insider = fh.get_insider_transactions(ticker)
        if insider:
            print(f"  {ticker}: {insider['type']} "
                  f"${insider['value_usd']:,} "
                  f"przez {insider['person']} "
                  f"({insider['days_ago']} dni temu)")
        else:
            print(f"  {ticker}: brak insider activity")

    stats = fh.get_stats()
    print(f"\n📊 API calls: {stats['total_calls']}")

    print("\n" + "="*55)
    print("  Plik 11 gotowy ✅")
    print("="*55)
