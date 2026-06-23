#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 3: PRE-FILTER
Zapisz jako pre_filter.py w folderze stock-scanner

Zadanie: z całego universe tickerów wyłonić TOP 5
najbardziej interesujących do analizy przez Claude AI.

Logika: kod filtruje i rankuje — Claude analizuje.

Historia zmian:
    v1.0 — pierwsza wersja
    v1.1 — próg UW fast track $500k
    v2.0 — nowy scoring: options flow i news jako liderzy
           - UW unusual options flow → 30 pkt (nowy lider)
           - Polygon news → 25 pkt (nowy)
           - Volume ratio → 25 pkt (zmniejszony, wcześniejszy ruch preferowany)
           - Dark pool → 15 pkt (zmniejszony)
           - Finnhub events → 5 pkt (zmniejszony)
           - Dodano uw_flow_data i news_data do score_ticker()
"""

from config import logger, CONFIG


# ==================== SCORING ====================

def score_ticker(ticker_data, dark_pool_flow=None, finnhub_data=None,
                 uw_flow_data=None, news_data=None):
    """
    Oblicza wynik zainteresowania dla tickera (0-100).
    NIE jest to sygnał tradingowy — tylko priorytet do analizy przez Claude.

    ticker_data   — dane z Polygon (cena, wolumen, zmiana%)
    dark_pool_flow — lista transakcji dark pool dla tickera
    finnhub_data  — dict z earnings i insider data
    uw_flow_data  — dict z UW unusual options flow dla tickera
    news_data     — lista newsów z Polygon dla tickera
    """
    score   = 0
    reasons = []

    # ---- 1. UW OPTIONS FLOW (0-30 pkt) ---- NOWY LIDER
    # Unusual options activity = smart money sygnał PRZED ruchem ceny
    if uw_flow_data:
        call_put    = uw_flow_data.get('call_put_ratio', 1.0)
        unusual     = uw_flow_data.get('unusual', False)
        call_vol    = uw_flow_data.get('call_volume', 0)
        put_vol     = uw_flow_data.get('put_volume', 0)
        total_vol   = call_vol + put_vol
        sentiment   = uw_flow_data.get('sentiment', 'neutral')

        if unusual and sentiment == 'bullish' and call_put >= 5.0:
            score += 30
            reasons.append(f"UW unusual CALL flow — ratio {call_put:.1f}x (ekstremalny)")
        elif unusual and sentiment == 'bullish' and call_put >= 2.0:
            score += 22
            reasons.append(f"UW unusual options bullish — ratio {call_put:.1f}x")
        elif sentiment == 'bullish' and call_put >= 2.0 and total_vol > 1000:
            score += 15
            reasons.append(f"UW options bullish — ratio {call_put:.1f}x")
        elif unusual and sentiment == 'bearish' and call_put <= 0.3:
            score += 10  # bearish też wart uwagi (short okazja)
            reasons.append(f"UW unusual PUT flow — ratio {call_put:.1f}x (bearish)")

    # ---- 2. POLYGON NEWS (0-25 pkt) ---- NOWY
    # Fundamentalny katalizator PRZED ruchem ceny
    if news_data:
        bullish_count = sum(1 for n in news_data if n.get('sentiment') == 'bullish')
        bearish_count = sum(1 for n in news_data if n.get('sentiment') == 'bearish')
        total_news    = len(news_data)

        if bullish_count >= 2:
            score += 25
            reasons.append(f"Polygon news: {bullish_count} bullish z {total_news}")
        elif bullish_count == 1 and bearish_count == 0:
            score += 18
            reasons.append(f"Polygon news: bullish katalizator")
        elif bearish_count >= 2:
            score += 8  # bearish też wart uwagi
            reasons.append(f"Polygon news: {bearish_count} bearish")
        elif total_news > 0:
            score += 5
            reasons.append(f"Polygon news: {total_news} artykułów")

    # ---- 3. VOLUME RATIO (0-25 pkt) ---- ZMNIEJSZONY
    # Wcześniejszy ruch (1.5-3x) preferowany nad późnym (5x+)
    ratio = ticker_data.get('volume_ratio', 1.0)

    if 1.5 <= ratio < 3.0:
        score += 25
        reasons.append(f"Volume {ratio:.1f}x średniej (wczesny sygnał)")
    elif 3.0 <= ratio < 5.0:
        score += 15
        reasons.append(f"Volume {ratio:.1f}x średniej (wysoki)")
    elif ratio >= 5.0:
        score += 8
        reasons.append(f"Volume {ratio:.1f}x średniej (może za późno)")
    elif ratio >= 1.2:
        score += 5
        reasons.append(f"Volume {ratio:.1f}x średniej (podwyższony)")

    # ---- 4. ZMIANA CENY (0-15 pkt) ---- ZMNIEJSZONA
    # Wcześniejszy ruch (3-15%) preferowany nad dużym (15%+)
    change     = ticker_data.get('change_pct', 0)
    abs_change = abs(change)

    if 3.0 <= abs_change <= 15.0 and change > 0:
        score += 15
        reasons.append(f"Zmiana {change:+.1f}% (wczesny bullish ruch)")
    elif abs_change > 15.0 and change > 0:
        score += 5
        reasons.append(f"Zmiana {change:+.1f}% (duży ruch — może za późno)")
    elif 3.0 <= abs_change <= 15.0 and change < 0:
        score += 8
        reasons.append(f"Zmiana {change:+.1f}% (wczesny bearish ruch)")
    elif abs_change > 15.0 and change < 0:
        score += 3
        reasons.append(f"Zmiana {change:+.1f}% (duży spadek)")

    # ---- 5. DARK POOL (0-15 pkt) ---- ZMNIEJSZONY
    if dark_pool_flow:
        ticker_symbol   = ticker_data.get('ticker', '')
        dp_transactions = [
            dp for dp in dark_pool_flow
            if dp.get('ticker') == ticker_symbol
        ]

        if dp_transactions:
            total_size = sum(dp.get('size_usd', 0) for dp in dp_transactions)
            buy_count  = sum(1 for dp in dp_transactions if dp.get('side') == 'BUY')
            sell_count = sum(1 for dp in dp_transactions if dp.get('side') == 'SELL')

            if total_size >= 1_000_000 and buy_count > sell_count:
                score += 15
                reasons.append(f"Dark pool BUY ${total_size:,.0f}")
            elif total_size >= 1_000_000:
                score += 10
                reasons.append(f"Dark pool ${total_size:,.0f}")
            elif total_size >= 500_000:
                score += 8
                reasons.append(f"Dark pool ${total_size:,.0f}")

    # ---- 6. FINNHUB EVENTS (0-5 pkt) ---- ZMNIEJSZONY
    if finnhub_data:
        earnings = finnhub_data.get('earnings')
        insider  = finnhub_data.get('insider')

        if earnings:
            days = earnings.get('days_until', 99)
            prev = earnings.get('previous_surprise', '')
            if days <= 3:
                score += 5
                reasons.append(f"Earnings za {days} dni! (poprzedni: {prev})")
            elif days <= 7:
                score += 3
                reasons.append(f"Earnings za {days} dni")

        if insider and insider.get('type') == 'BUY':
            ival = insider.get('value_usd', 0)
            if ival >= 100_000:
                score += 5
                reasons.append(f"Insider BUY ${ival:,}")

    return score, reasons


# ==================== FILTROWANIE ====================

def apply_base_filters(universe):
    """
    Krok 1: Usuń tickery które nie spełniają podstawowych kryteriów.
    """
    passed         = []
    rejected_count = 0

    for ticker in universe:
        price  = ticker.get('price', 0)
        volume = ticker.get('volume', 0)

        if not (CONFIG['min_price'] <= price <= CONFIG['max_price']):
            rejected_count += 1
            continue

        if volume < CONFIG['min_volume']:
            rejected_count += 1
            continue

        passed.append(ticker)

    logger.info(f"Pre-filter: {len(universe)} \u2192 {len(passed)} tickerów "
                f"(odrzucono {rejected_count} po filtrach bazowych)")
    return passed


def rank_tickers(filtered_universe, dark_pool_flow=None, finnhub_cache=None,
                 uw_flow_cache=None, news_cache=None):
    """
    Krok 2: Oceń i posortuj tickery po wyniku zainteresowania.
    """
    scored = []

    for ticker_data in filtered_universe:
        ticker_symbol = ticker_data.get('ticker', '')

        finnhub_data = None
        if finnhub_cache and ticker_symbol in finnhub_cache:
            finnhub_data = finnhub_cache[ticker_symbol]

        uw_flow_data = None
        if uw_flow_cache and ticker_symbol in uw_flow_cache:
            uw_flow_data = uw_flow_cache[ticker_symbol]

        news_data = None
        if news_cache and ticker_symbol in news_cache:
            news_data = news_cache[ticker_symbol]

        score, reasons = score_ticker(
            ticker_data,
            dark_pool_flow=dark_pool_flow,
            finnhub_data=finnhub_data,
            uw_flow_data=uw_flow_data,
            news_data=news_data,
        )

        scored.append({
            'ticker':       ticker_symbol,
            'score':        score,
            'reasons':      reasons,
            'price':        ticker_data.get('price', 0),
            'change_pct':   ticker_data.get('change_pct', 0),
            'volume':       ticker_data.get('volume', 0),
            'volume_ratio': ticker_data.get('volume_ratio', 1.0),
            'raw_data':     ticker_data,
        })

    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored


def get_top_tickers(universe, dark_pool_flow=None, finnhub_cache=None,
                    uw_flow_cache=None, news_cache=None, top_n=None):
    """
    Główna funkcja pre-filtra.
    Przyjmuje cały universe, zwraca TOP N tickerów dla Claude'a.
    """
    if top_n is None:
        top_n = CONFIG['max_tickers_for_claude']

    filtered = apply_base_filters(universe)

    if not filtered:
        logger.warning("Pre-filter: brak tickerów po filtrach bazowych")
        return []

    ranked = rank_tickers(
        filtered,
        dark_pool_flow=dark_pool_flow,
        finnhub_cache=finnhub_cache,
        uw_flow_cache=uw_flow_cache,
        news_cache=news_cache,
    )

    top = [t for t in ranked[:top_n] if t['score'] > 0]

    if not top:
        logger.info("Pre-filter: brak tickerów z score > 0, ranking po wolumenie")
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
    Próg $500k — do weryfikacji i ewentualnej korekty po przejściu na LIVE.
    """
    if not dark_pool_flow:
        return []

    current_symbols = {t['ticker'] for t in current_top_tickers}
    fast_track      = []

    for dp in dark_pool_flow:
        ticker = dp.get('ticker', '')
        size   = dp.get('size_usd', 0)
        side   = dp.get('side', '')

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
    print("  TEST: Pre-filter v2.0")
    print("="*50)

    polygon = MockPolygon()
    uw      = MockUnusualWhales()
    fh      = MockFinnhub()

    universe       = polygon.get_universe()
    dark_pool_flow = uw.get_dark_pool_flow()

    finnhub_cache = {}
    news_cache    = {}
    uw_flow_cache = {}

    for t in universe:
        ticker = t['ticker']
        finnhub_cache[ticker] = {
            'earnings': fh.get_earnings_calendar(ticker),
            'insider':  fh.get_insider_transactions(ticker),
        }
        # Symuluj news cache
        news = polygon.get_news(ticker)
        if news:
            news_cache[ticker] = news
        # Symuluj UW flow cache
        uw_flow_cache[ticker] = uw.get_options_flow(ticker)

    print(f"\nUniverse: {len(universe)} tickerów")

    top5 = get_top_tickers(
        universe,
        dark_pool_flow=dark_pool_flow,
        finnhub_cache=finnhub_cache,
        uw_flow_cache=uw_flow_cache,
        news_cache=news_cache,
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
            print(f"   \u2192 {reason}")

    print("\n" + "="*50)
    print("  Plik 3 v2.0 gotowy \u2705")
    print("="*50)
