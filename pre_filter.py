#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 3: PRE-FILTER v3.0
Zapisz jako pre_filter.py w folderze stock-scanner

Zadanie: z całego universe tickerów wyłonić TOP 5
najbardziej interesujących do analizy przez Claude AI.

Hierarchia scoring v3.0 (oparty na logice rynkowej):
─────────────────────────────────────────────────────
TIER 0 — Dyskwalifikatory (skip bez scoringu):
  • Cena < $1.00
  • Typ: WARRANT, RIGHT, UNIT (z Polygon API)
  • Volume < 50,000

TIER 1 — Smart Money (0-40 pkt) — LIDER:
  • UW unusual options flow (kierunek niepewny → traktuj jako sygnał uwagi)
  • Dark pool duża transakcja (bid/ask inference)

TIER 2 — Fundamentalny Katalizator (0-30 pkt):
  • Polygon news (źródło ma znaczenie)
  • Finnhub earnings w ciągu 3 dni
  • Insider BUY

TIER 3 — Techniczne (0-20 pkt):
  • RSI 40-65 (sweet spot — nie overbought, nie dead)
  • VWAP reclaim (cena właśnie przekroczyła VWAP)
  • EMA9 > EMA21 (momentum bullish)
  • HOD breakout (cena atakuje dzienny szczyt)
  • Range position (cena w górnej części zakresu)

TIER 4 — Volume (0-10 pkt) — ZMNIEJSZONY:
  • 1.5-3x → wczesny ruch (najlepszy)
  • 3-5x  → może być jeszcze OK
  • >5x   → prawdopodobnie za późno

Historia zmian:
    v1.0 — pierwsza wersja
    v2.0 — nowy scoring: options flow i news jako liderzy
    v3.0 — pełna hierarchia smart money → katalizator → tech → volume
           - Ticker Types API (oficjalny filtr warrant/right/unit)
           - RSI z Polygon (sweet spot 40-65)
           - EMA9 vs EMA21 (momentum)
           - VWAP reclaim i HOD breakout
           - News source quality (Reuters > PRNewswire)
           - Cena minimalna $1.00
           - Volume zmniejszony do TIER 4
"""

from config import logger, CONFIG


# ==================== DYSKWALIFIKATORY ====================

# Typy tickerów które wykluczamy (z Polygon Ticker Types API)
EXCLUDED_TYPES = {'WARRANT', 'RIGHT', 'UNIT', 'FUND', 'SP'}

# Sufiksy wykluczające (backup gdy brak Polygon type)
EXCLUDED_SUFFIXES = ('W', 'WS', 'R', 'RT', 'U')

# Minimalna cena (groszowe spółki = manipulacja)
MIN_PRICE_QUALITY = 1.00

# Minimalny volume do scoringu
MIN_VOLUME_SCORING = 50_000

# Wiarygodne źródła newsów (wyższy score)
PREMIUM_NEWS_SOURCES = {
    'reuters', 'bloomberg', 'wsj', 'ft', 'cnbc', 'marketwatch',
    'sec.gov', 'businesswire', 'globenewswire', 'accessnewswire',
    'prnewswire', 'accesswire'
}


# ==================== SCORING ====================

def score_ticker(ticker_data, dark_pool_flow=None, finnhub_data=None,
                 uw_flow_data=None, news_data=None, technical_data=None):
    """
    Oblicza wynik zainteresowania dla tickera (0-100).

    ticker_data    — dane z Polygon (cena, wolumen, zmiana%, VWAP, high, low, open)
    dark_pool_flow — lista transakcji dark pool
    finnhub_data   — dict z earnings i insider data
    uw_flow_data   — dict z UW unusual options flow
    news_data      — lista newsów z Polygon
    technical_data — dict z RSI, EMA9, EMA21 z Polygon
    """
    score   = 0
    reasons = []

    price  = ticker_data.get('price', 0)
    vwap   = ticker_data.get('vwap', 0)
    high   = ticker_data.get('high', 0)
    low    = ticker_data.get('low', 0)
    open_p = ticker_data.get('open', price)
    change = ticker_data.get('change_pct', 0)
    ratio  = ticker_data.get('volume_ratio', 1.0)

    # ---- TIER 1: SMART MONEY (0-40 pkt) ----

    # UW Options Flow — traktujemy jako sygnał uwagi (kierunek niepewny)
    if uw_flow_data:
        call_put  = uw_flow_data.get('call_put_ratio', 1.0)
        unusual   = uw_flow_data.get('unusual', False)
        sentiment = uw_flow_data.get('sentiment', 'neutral')

        if unusual and call_put >= 5.0:
            score += 30
            reasons.append(f"UW unusual flow — ratio {call_put:.1f}x (ekstremalny, kierunek niepewny)")
        elif unusual and call_put >= 2.0:
            score += 20
            reasons.append(f"UW unusual flow — ratio {call_put:.1f}x")
        elif unusual:
            score += 12
            reasons.append(f"UW unusual flow (aktywność smart money)")

    # Dark Pool — bid/ask inference (kierunek niepewny bez weryfikacji)
    if dark_pool_flow:
        ticker_sym   = ticker_data.get('ticker', '')
        dp_trades    = [dp for dp in dark_pool_flow
                        if dp.get('ticker') == ticker_sym]

        if dp_trades:
            total_usd  = sum(dp.get('size_usd', 0) for dp in dp_trades)
            buy_usd    = sum(dp.get('size_usd', 0) for dp in dp_trades
                            if dp.get('side') == 'BUY')
            sell_usd   = sum(dp.get('size_usd', 0) for dp in dp_trades
                            if dp.get('side') == 'SELL')
            neutral    = total_usd - buy_usd - sell_usd

            if total_usd >= 1_000_000 and buy_usd > sell_usd:
                score += 20
                reasons.append(f"Dark pool BUY ${total_usd:,.0f} (bid/ask inference)")
            elif total_usd >= 1_000_000:
                score += 12
                reasons.append(f"Dark pool aktywność ${total_usd:,.0f} (kierunek niepewny)")
            elif total_usd >= 500_000:
                score += 8
                reasons.append(f"Dark pool ${total_usd:,.0f}")

    # ---- TIER 2: FUNDAMENTALNY KATALIZATOR (0-30 pkt) ----

    if news_data:
        bullish_premium = 0  # premium sources
        bullish_regular = 0
        bearish_count   = 0
        total_news      = len(news_data)

        for n in news_data:
            title       = n.get('title', '').lower()
            description = n.get('description', '').lower()
            publisher   = n.get('publisher', {})
            source      = publisher.get('name', '').lower() if isinstance(publisher, dict) else ''
            sentiment   = n.get('insights', [{}])[0].get('sentiment', '') \
                          if n.get('insights') else ''

            is_bullish = (
                sentiment == 'positive' or
                any(w in title for w in [
                    'fda approval', 'approved', 'breakthrough', 'contract',
                    'partnership', 'beat', 'upgrade', 'raises', 'grant',
                    'clearance', 'positive trial', 'positive data'
                ])
            )
            is_bearish = (
                sentiment == 'negative' or
                any(w in title for w in [
                    'downgrade', 'miss', 'recall', 'investigation',
                    'fraud', 'lawsuit', 'bankruptcy', 'delisted'
                ])
            )

            is_premium = any(ps in source for ps in PREMIUM_NEWS_SOURCES)

            if is_bullish:
                if is_premium:
                    bullish_premium += 1
                else:
                    bullish_regular += 1
            elif is_bearish:
                bearish_count += 1

        if bullish_premium >= 1:
            score += 25
            reasons.append(f"Bullish news (premium source): {bullish_premium}")
        elif bullish_regular >= 2:
            score += 18
            reasons.append(f"Bullish news: {bullish_regular} artykuły")
        elif bullish_regular == 1:
            score += 12
            reasons.append(f"Bullish news: 1 artykuł")
        elif bearish_count > 0:
            score += 5   # bearish też wart uwagi (short okazja)
            reasons.append(f"Bearish news: {bearish_count}")
        elif total_news > 0:
            score += 3
            reasons.append(f"News aktywność: {total_news} artykułów")

    # Finnhub earnings i insider
    if finnhub_data:
        earnings = finnhub_data.get('earnings')
        insider  = finnhub_data.get('insider')

        if earnings:
            days = earnings.get('days_until', 99)
            prev = earnings.get('previous_surprise', '')
            if days == 0:
                score += 25
                reasons.append(f"EARNINGS DZISIAJ! ({prev})")
            elif days <= 1:
                score += 20
                reasons.append(f"Earnings jutro ({prev})")
            elif days <= 3:
                score += 15
                reasons.append(f"Earnings za {days} dni ({prev})")
            elif days <= 7:
                score += 8
                reasons.append(f"Earnings za {days} dni")

        if insider and insider.get('type') == 'BUY':
            ival = insider.get('value_usd', 0)
            if ival >= 500_000:
                score += 20
                reasons.append(f"Insider BUY ${ival:,} (duża pozycja)")
            elif ival >= 100_000:
                score += 12
                reasons.append(f"Insider BUY ${ival:,}")
            elif ival >= 50_000:
                score += 8
                reasons.append(f"Insider BUY ${ival:,}")

    # ---- TIER 3: TECHNICZNE (0-20 pkt) ----

    if technical_data:
        rsi  = technical_data.get('rsi')
        ema9 = technical_data.get('ema9')
        ema21 = technical_data.get('ema21')

        # RSI sweet spot 40-65 (nie overbought, nie dead)
        if rsi is not None:
            if 40 <= rsi <= 65:
                score += 10
                reasons.append(f"RSI {rsi:.0f} (sweet spot 40-65)")
            elif 30 <= rsi < 40:
                score += 5
                reasons.append(f"RSI {rsi:.0f} (lekko oversold — potencjalny bounce)")
            elif rsi > 75:
                score -= 5
                reasons.append(f"RSI {rsi:.0f} (overbought — ryzyko reversal)")

        # EMA9 > EMA21 = momentum bullish
        if ema9 and ema21:
            if ema9 > ema21 * 1.002:  # 0.2% bufor
                score += 8
                reasons.append(f"EMA9 > EMA21 (momentum bullish)")
            elif ema9 < ema21 * 0.998:
                score -= 3
                reasons.append(f"EMA9 < EMA21 (momentum bearish)")

    # VWAP reclaim — cena właśnie przekroczyła VWAP
    if vwap > 0 and price > 0:
        vwap_diff_pct = ((price - vwap) / vwap) * 100
        if 0 < vwap_diff_pct <= 2.0:
            score += 10
            reasons.append(f"VWAP reclaim (cena {vwap_diff_pct:+.1f}% nad VWAP)")
        elif vwap_diff_pct > 2.0:
            score += 5
            reasons.append(f"Powyżej VWAP ({vwap_diff_pct:+.1f}%)")
        elif -1.0 <= vwap_diff_pct < 0:
            score += 3
            reasons.append(f"Blisko VWAP ({vwap_diff_pct:+.1f}%)")

    # HOD breakout — cena atakuje dzienny szczyt
    if high > 0 and price > 0:
        hod_diff_pct = ((price - high) / high) * 100
        if hod_diff_pct >= -1.0:  # w ciągu 1% od HOD
            score += 8
            reasons.append(f"HOD breakout (cena {hod_diff_pct:+.1f}% od szczytu)")

    # Range position — gdzie jest cena w dziennym zakresie
    if high > low > 0 and price > 0:
        day_range = high - low
        if day_range > 0:
            position = (price - low) / day_range
            if position >= 0.75:
                score += 5
                reasons.append(f"Range position {position:.0%} (górna część zakresu)")

    # ---- TIER 4: VOLUME (0-10 pkt) — ZMNIEJSZONY ----

    if 1.5 <= ratio < 3.0:
        score += 10
        reasons.append(f"Volume {ratio:.1f}x (wczesny sygnał)")
    elif 3.0 <= ratio < 5.0:
        score += 6
        reasons.append(f"Volume {ratio:.1f}x (wysoki)")
    elif ratio >= 5.0:
        score += 3
        reasons.append(f"Volume {ratio:.1f}x (może za późno)")
    elif ratio >= 1.2:
        score += 2
        reasons.append(f"Volume {ratio:.1f}x (podwyższony)")

    # Zmiana ceny — preferuj wczesny ruch
    abs_change = abs(change)
    if 3.0 <= abs_change <= 12.0 and change > 0:
        score += 5
        reasons.append(f"Zmiana {change:+.1f}% (wczesny ruch)")
    elif abs_change > 12.0 and change > 0:
        score += 2
        reasons.append(f"Zmiana {change:+.1f}% (duży ruch)")

    return score, reasons


# ==================== FILTROWANIE ====================

def is_excluded_ticker(ticker_data, polygon_api=None):
    """
    Sprawdza czy ticker powinien być wykluczony.
    
    Używa Polygon Ticker Types API jeśli dostępne,
    jako backup sprawdza sufiks tickera.
    """
    ticker = ticker_data.get('ticker', '').upper()
    price  = ticker_data.get('price', 0)
    volume = ticker_data.get('volume', 0)

    # Filtr ceny minimalnej
    if price < MIN_PRICE_QUALITY:
        return True, f"cena ${price:.2f} < $1.00"

    # Filtr volume minimalnego
    if volume < MIN_VOLUME_SCORING:
        return True, f"volume {volume:,} < {MIN_VOLUME_SCORING:,}"

    # Sprawdź typ tickera przez Polygon API (najdokładniejsze)
    ticker_type = ticker_data.get('ticker_type', '')
    if ticker_type and ticker_type.upper() in EXCLUDED_TYPES:
        return True, f"typ {ticker_type}"

    # Backup — sprawdź sufiks
    for suffix in EXCLUDED_SUFFIXES:
        if ticker.endswith(suffix) and len(ticker) > len(suffix):
            return True, f"sufiks {suffix}"

    # Wyklucz tickery z kropką (np. BRK.A, BRK.B) — zwykle nie small-cap
    if '.' in ticker and not ticker.endswith('.WS'):
        # Sprawdź czy to nie warrant (.WS) ani prawa (.RT)
        if ticker.endswith('.RT'):
            return True, "prawa (RT)"

    return False, ""


def apply_base_filters(universe, polygon_api=None):
    """
    Krok 1: Usuń tickery które nie spełniają kryteriów bazowych.
    """
    passed         = []
    rejected_count = 0
    rejected_reasons = {}

    for ticker in universe:
        price  = ticker.get('price', 0)
        volume = ticker.get('volume', 0)

        # Filtr ceny z config
        if not (CONFIG['min_price'] <= price <= CONFIG['max_price']):
            rejected_count += 1
            continue

        # Filtr volume z config
        if volume < CONFIG.get('min_volume', 100_000):
            rejected_count += 1
            continue

        # Filtr jakości (cena minimalna + typ)
        excluded, reason = is_excluded_ticker(ticker, polygon_api)
        if excluded:
            rejected_count += 1
            rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
            continue

        passed.append(ticker)

    logger.info(f"Pre-filter: {len(universe)} → {len(passed)} tickerów "
                f"(odrzucono {rejected_count} po filtrach bazowych)")

    if rejected_reasons:
        top_reasons = sorted(rejected_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
        logger.info(f"  Główne powody odrzucenia: "
                    f"{', '.join(f'{r}:{c}' for r,c in top_reasons)}")

    return passed


def rank_tickers(filtered_universe, dark_pool_flow=None, finnhub_cache=None,
                 uw_flow_cache=None, news_cache=None, technical_cache=None):
    """
    Krok 2: Oceń i posortuj tickery po wyniku zainteresowania.
    """
    scored = []

    for ticker_data in filtered_universe:
        ticker_symbol = ticker_data.get('ticker', '')

        finnhub_data   = finnhub_cache.get(ticker_symbol) if finnhub_cache else None
        uw_flow_data   = uw_flow_cache.get(ticker_symbol) if uw_flow_cache else None
        news_data      = news_cache.get(ticker_symbol) if news_cache else None
        technical_data = technical_cache.get(ticker_symbol) if technical_cache else None

        score, reasons = score_ticker(
            ticker_data,
            dark_pool_flow=dark_pool_flow,
            finnhub_data=finnhub_data,
            uw_flow_data=uw_flow_data,
            news_data=news_data,
            technical_data=technical_data,
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
                    uw_flow_cache=None, news_cache=None, technical_cache=None,
                    top_n=None, polygon_api=None):
    """
    Główna funkcja pre-filtra.
    Przyjmuje cały universe, zwraca TOP N tickerów dla Claude'a.
    """
    if top_n is None:
        top_n = CONFIG['max_tickers_for_claude']

    # Krok 1: Filtruj
    filtered = apply_base_filters(universe, polygon_api)

    if not filtered:
        logger.warning("Pre-filter: brak tickerów po filtrach bazowych")
        return []

    # Krok 2: Rankuj
    ranked = rank_tickers(
        filtered,
        dark_pool_flow=dark_pool_flow,
        finnhub_cache=finnhub_cache,
        uw_flow_cache=uw_flow_cache,
        news_cache=news_cache,
        technical_cache=technical_cache,
    )

    # Krok 3: Wybierz TOP N
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
                'reason':   f"UW dark pool ${size:,} — fast track (bid/ask inference)",
                'size_usd': size,
            })
            logger.info(f"UW Fast Track: {ticker} | ${size:,}")

    return fast_track


# ==================== TEST ====================

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  TEST: Pre-filter v3.0")
    print("="*55)

    # Symuluj dane
    test_universe = [
        # Świetny setup — smart money + news + techniczne
        {'ticker': 'GALT', 'price': 2.80, 'volume': 850000,
         'volume_ratio': 2.1, 'change_pct': 8.5,
         'vwap': 2.75, 'high': 2.85, 'low': 2.40, 'open': 2.50},

        # Pump & dump — duży volume bez fundamentów
        {'ticker': 'EHGO', 'price': 4.06, 'volume': 2100000,
         'volume_ratio': 43.5, 'change_pct': 108.9,
         'vwap': 3.20, 'high': 4.20, 'low': 1.90, 'open': 1.95},

        # Warrant — powinien być wykluczony
        {'ticker': 'NNAVW', 'price': 5.20, 'volume': 350000,
         'volume_ratio': 7.1, 'change_pct': -21.8,
         'vwap': 6.00, 'high': 6.50, 'low': 5.00, 'open': 6.20},

        # Groszowy — powinien być wykluczony
        {'ticker': 'TNON', 'price': 0.59, 'volume': 420000,
         'volume_ratio': 5.5, 'change_pct': 67.9,
         'vwap': 0.50, 'high': 0.65, 'low': 0.35, 'open': 0.35},

        # Solidny ticker z EMA bullish
        {'ticker': 'SOUN', 'price': 8.50, 'volume': 320000,
         'volume_ratio': 1.8, 'change_pct': 5.2,
         'vwap': 8.30, 'high': 8.60, 'low': 8.10, 'open': 8.15},
    ]

    # Symuluj uw_flow_cache
    uw_flow_cache = {
        'GALT': {
            'call_put_ratio': 17.22,
            'unusual': True,
            'sentiment': 'bullish',
            'call_volume': 65000,
            'put_volume': 3780,
        }
    }

    # Symuluj news_cache
    news_cache = {
        'GALT': [
            {
                'title': 'Galectin Therapeutics presents positive belapectin data at EASL 2026',
                'publisher': {'name': 'businesswire'},
                'insights': [{'sentiment': 'positive'}],
            }
        ]
    }

    # Symuluj technical_cache
    technical_cache = {
        'GALT': {'rsi': 58.3, 'ema9': 2.82, 'ema21': 2.65},
        'SOUN': {'rsi': 52.1, 'ema9': 8.45, 'ema21': 8.20},
        'EHGO': {'rsi': 82.5, 'ema9': 3.95, 'ema21': 2.10},
    }

    top5 = get_top_tickers(
        test_universe,
        uw_flow_cache=uw_flow_cache,
        news_cache=news_cache,
        technical_cache=technical_cache,
        top_n=5,
    )

    print(f"\n{'='*55}")
    print(f"  TOP {len(top5)} TICKERÓW DLA CLAUDE AI")
    print(f"{'='*55}")

    for i, t in enumerate(top5, 1):
        print(f"\n#{i} {t['ticker']} | Score: {t['score']} | "
              f"${t['price']:.2f} | {t['change_pct']:+.1f}% | "
              f"Vol: {t['volume_ratio']:.1f}x")
        for reason in t['reasons']:
            print(f"   → {reason}")

    print("\n" + "="*55)
    print("  Pre-filter v3.0 gotowy ✅")
    print("="*55)
