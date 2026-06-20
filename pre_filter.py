#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 3: PRE-FILTER
Zapisz jako pre_filter.py w folderze stock-scanner

Zadanie: z całego universe tickerów wyłonić TOP 5
najbardziej interesujących do analizy przez Claude AI.

Logika: kod filtruje i rankuje — Claude analizuje.
"""

from config import logger, CONFIG


# ==================== SCORING ====================

def score_ticker(ticker_data, dark_pool_flow=None, finnhub_data=None):
    """
    Oblicza wynik zainteresowania dla tickera (0-100).
    NIE jest to sygnał tradingowy — tylko priorytet do analizy przez Claude.

    ticker_data   — dane z Polygon (cena, wolumen, zmiana%)
    dark_pool_flow — lista transakcji UW dla tego tickera (opcjonalne)
    finnhub_data  — dict z earnings i insider data (opcjonalne)
    """
    score = 0
    reasons = []

    # ---- 1. VOLUME RATIO (0-40 pkt) ----
    # Najsilniejszy sygnał że coś się dzieje
    ratio = ticker_data.get('volume_ratio', 1.0)

    if ratio >= 5.0:
        score += 40
        reasons.append(f"Volume {ratio:.1f}x średniej (ekstremalny)")
    elif ratio >= 3.0:
        score += 30
        reasons.append(f"Volume {ratio:.1f}x średniej (bardzo wysoki)")
    elif ratio >= 2.0:
        score += 20
        reasons.append(f"Volume {ratio:.1f}x średniej (wysoki)")
    elif ratio >= 1.5:
        score += 10
        reasons.append(f"Volume {ratio:.1f}x średniej (podwyższony)")

    # ---- 2. ZMIANA CENY (0-25 pkt) ----
    change = abs(ticker_data.get('change_pct', 0))

    if change >= 15:
        score += 25
        reasons.append(f"Zmiana {ticker_data['change_pct']:+.1f}% (ekstremalny ruch)")
    elif change >= 10:
        score += 20
        reasons.append(f"Zmiana {ticker_data['change_pct']:+.1f}% (duży ruch)")
    elif change >= 5:
        score += 15
        reasons.append(f"Zmiana {ticker_data['change_pct']:+.1f}% (znaczący ruch)")
    elif change >= 3:
        score += 8
        reasons.append(f"Zmiana {ticker_data['change_pct']:+.1f}% (umiarkowany ruch)")

    # ---- 3. DARK POOL (0-20 pkt) ----
    if dark_pool_flow:
        # Znajdź transakcje dla tego tickera
        ticker_symbol = ticker_data.get('ticker', '')
        dp_transactions = [
            dp for dp in dark_pool_flow
            if dp.get('ticker') == ticker_symbol
        ]

        if dp_transactions:
            total_size = sum(dp.get('size_usd', 0) for dp in dp_transactions)
            buy_count  = sum(1 for dp in dp_transactions if dp.get('side') == 'BUY')

            if total_size >= 1_000_000:
                score += 20
                reasons.append(f"Dark pool ${total_size:,.0f} "
                                f"({'głównie kupno' if buy_count > 0 else 'sprzedaż'})")
            elif total_size >= 500_000:
                score += 12
                reasons.append(f"Dark pool ${total_size:,.0f}")
            elif total_size >= 100_000:
                score += 6
                reasons.append(f"Dark pool ${total_size:,.0f}")

    # ---- 4. FINNHUB EVENTS (0-15 pkt) ----
    if finnhub_data:
        earnings = finnhub_data.get('earnings')
        insider  = finnhub_data.get('insider')

        if earnings:
            days = earnings.get('days_until', 99)
            prev = earnings.get('previous_surprise', '')

            if days <= 3:
                score += 15
                reasons.append(f"Earnings za {days} dni! (poprzedni: {prev})")
            elif days <= 7:
                score += 10
                reasons.append(f"Earnings za {days} dni (poprzedni: {prev})")
            elif days <= 14:
                score += 5
                reasons.append(f"Earnings za {days} dni")

        if insider:
            itype = insider.get('type', '')
            ival  = insider.get('value_usd', 0)
            iperson = insider.get('person', '')

            if itype == 'BUY' and ival >= 100_000:
                score += 12
                reasons.append(f"Insider BUY ${ival:,} przez {iperson}")
            elif itype == 'BUY':
                score += 6
                reasons.append(f"Insider BUY ${ival:,} przez {iperson}")
            elif itype == 'SELL' and ival >= 200_000:
                score += 4  # Insider sell też wart uwagi (bearish)
                reasons.append(f"Insider SELL ${ival:,} przez {iperson}")

    return score, reasons


# ==================== FILTROWANIE ====================

def apply_base_filters(universe):
    """
    Krok 1: Usuń tickery które nie spełniają podstawowych kryteriów.
    Zwraca tylko tickery kwalifikujące się do dalszej analizy.
    """
    passed = []
    rejected_count = 0

    for ticker in universe:
        price  = ticker.get('price', 0)
        volume = ticker.get('volume', 0)

        # Filtr cenowy
        if not (CONFIG['min_price'] <= price <= CONFIG['max_price']):
            rejected_count += 1
            continue

        # Filtr wolumenu
        if volume < CONFIG['min_volume']:
            rejected_count += 1
            continue

        passed.append(ticker)

    logger.info(f"Pre-filter: {len(universe)} → {len(passed)} tickerów "
                f"(odrzucono {rejected_count} po filtrach bazowych)")
    return passed


def rank_tickers(filtered_universe, dark_pool_flow=None, finnhub_cache=None):
    """
    Krok 2: Oceń i posortuj tickery po wyniku zainteresowania.
    Zwraca posortowaną listę z wynikami i powodami.
    """
    scored = []

    for ticker_data in filtered_universe:
        ticker_symbol = ticker_data.get('ticker', '')

        # Pobierz dane Finnhub dla tego tickera (jeśli dostępne)
        finnhub_data = None
        if finnhub_cache and ticker_symbol in finnhub_cache:
            finnhub_data = finnhub_cache[ticker_symbol]

        score, reasons = score_ticker(
            ticker_data,
            dark_pool_flow=dark_pool_flow,
            finnhub_data=finnhub_data
        )

        scored.append({
            'ticker':      ticker_symbol,
            'score':       score,
            'reasons':     reasons,
            'price':       ticker_data.get('price', 0),
            'change_pct':  ticker_data.get('change_pct', 0),
            'volume':      ticker_data.get('volume', 0),
            'volume_ratio': ticker_data.get('volume_ratio', 1.0),
            'raw_data':    ticker_data,  # pełne dane do Claude'a
        })

    # Sortuj malejąco po score
    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored


def get_top_tickers(universe, dark_pool_flow=None, finnhub_cache=None,
                    top_n=None):
    """
    Główna funkcja pre-filtra.
    Przyjmuje cały universe, zwraca TOP N tickerów dla Claude'a.

    universe       — lista tickerów z Polygon
    dark_pool_flow — lista transakcji z Unusual Whales
    finnhub_cache  — dict {ticker: {earnings: ..., insider: ...}}
    top_n          — ile tickerów zwrócić (domyślnie z CONFIG)
    """
    if top_n is None:
        top_n = CONFIG['max_tickers_for_claude']

    # Krok 1: filtry bazowe
    filtered = apply_base_filters(universe)

    if not filtered:
        logger.warning("Pre-filter: brak tickerów po filtrach bazowych")
        return []

    # Krok 2: scoring i ranking
    ranked = rank_tickers(filtered, dark_pool_flow, finnhub_cache)

    # Krok 3: weź TOP N z minimalnym score > 0
    top = [t for t in ranked[:top_n] if t['score'] > 0]

    if not top:
        # Jeśli żaden nie ma score > 0, weź TOP N po samym wolumenie
        logger.info("Pre-filter: brak tickerów z score > 0, "
                    "ranking po wolumenie")
        top = ranked[:top_n]

    logger.info(f"Pre-filter: wyłoniono TOP {len(top)} tickerów dla Claude'a")
    for t in top:
        logger.info(f"  {t['ticker']:6s} | score: {t['score']:3d} | "
                    f"${t['price']:.2f} | {t['change_pct']:+.1f}% | "
                    f"vol {t['volume_ratio']:.1f}x")

    return top


# ==================== UW FAST TRACK ====================

def uw_fast_track(dark_pool_flow, current_top_tickers):
    """
    Sprawdza czy UW flow zawiera tickery spoza aktualnego TOP 5.
    Jeśli tak — dodaje je jako priorytetowe do natychmiastowej analizy.

    Wywoływane co 1 minutę (cykl UW).
    """
    if not dark_pool_flow:
        return []

    current_symbols = {t['ticker'] for t in current_top_tickers}
    fast_track = []

    for dp in dark_pool_flow:
        ticker = dp.get('ticker', '')
        size   = dp.get('size_usd', 0)
        side   = dp.get('side', '')

        # Tylko duże transakcje BUY spoza obecnego TOP 5
        if (ticker not in current_symbols
                and size >= 500_000
                and side == 'BUY'):
            fast_track.append({
                'ticker':   ticker,
                'reason':   f"UW dark pool BUY ${size:,} — fast track",
                'size_usd': size,
            })
            logger.info(f"UW Fast Track: {ticker} | BUY ${size:,}")

    return fast_track


# ==================== TEST ====================

if __name__ == "__main__":
    from mock_polygon import MockPolygon, MockUnusualWhales, MockFinnhub

    print("\n" + "="*50)
    print("  TEST: Pre-filter")
    print("="*50)

    # Pobierz dane z mocków
    polygon = MockPolygon()
    uw      = MockUnusualWhales()
    fh      = MockFinnhub()

    universe       = polygon.get_universe()
    dark_pool_flow = uw.get_dark_pool_flow()

    # Zbierz dane Finnhub dla wszystkich tickerów
    finnhub_cache = {}
    for t in universe:
        ticker = t['ticker']
        finnhub_cache[ticker] = {
            'earnings': fh.get_earnings_calendar(ticker),
            'insider':  fh.get_insider_transactions(ticker),
        }

    print(f"\nUniverse: {len(universe)} tickerów")
    print(f"Dark pool: {len(dark_pool_flow)} transakcji")

    # Uruchom pre-filter
    top5 = get_top_tickers(
        universe,
        dark_pool_flow=dark_pool_flow,
        finnhub_cache=finnhub_cache,
        top_n=5
    )

    print(f"\n{'='*50}")
    print(f"  TOP {len(top5)} TICKERÓW DLA CLAUDE AI")
    print(f"{'='*50}")

    for i, t in enumerate(top5, 1):
        print(f"\n#{i} {t['ticker']} | Score: {t['score']} | "
              f"${t['price']:.2f} | {t['change_pct']:+.1f}% | "
              f"Vol: {t['volume_ratio']:.1f}x")
        for reason in t['reasons']:
            print(f"   → {reason}")

    # Test UW fast track
    print(f"\n{'='*50}")
    print("  UW FAST TRACK TEST")
    print(f"{'='*50}")

    fast = uw_fast_track(dark_pool_flow, top5)
    if fast:
        for f in fast:
            print(f"  ⚡ {f['ticker']}: {f['reason']}")
    else:
        print("  Brak fast track tickerów w tej chwili")

    print("\n" + "="*50)
    print("  Plik 3 gotowy ✅")
    print("="*50)