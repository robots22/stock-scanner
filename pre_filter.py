#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 3: PRE-FILTER v4.0
Zapisz jako pre_filter.py w folderze stock-scanner

Hierarchia scoring v4.0 - Universal, market-condition agnostic:
  TIER 0: Dyskwalifikatory
  TIER 1: Smart Money (UW flow, dark pool) - przed ruchem
  TIER 2: Fundamentalny katalizator (news, earnings, insider)
  TIER 3: Float + Gap - struktura ruchu
  TIER 4: Techniczne (VWAP, RSI, EMA, HOD)
  TIER 5: Volume momentum
  PENALTIES: Czerwone flagi

Cel: rozroznianie tickerow - score musi byc rozny dla roznych setupow
"""

from config import logger, CONFIG, get_dynamic_threshold
from datetime import datetime, timezone

EXCLUDED_TYPES     = {'WARRANT', 'RIGHT', 'UNIT', 'FUND', 'SP'}
EXCLUDED_SUFFIXES  = ('W', 'WS', 'WW', 'R', 'RT', 'U')
MIN_PRICE_QUALITY  = 0.50
MIN_VOLUME_SCORING = 50_000

PREMIUM_NEWS_SOURCES = {
    'reuters', 'bloomberg', 'wsj', 'ft', 'cnbc', 'marketwatch',
    'sec.gov', 'businesswire', 'globenewswire', 'accessnewswire',
    'prnewswire', 'accesswire', 'benzinga', 'thestreet',
}

# Catalyst Quality Score — jakosc eventu decyduje o wadze
CATALYST_HIGH = [
    'fda approval', 'fda approved', 'fda grants', 'fda clears',
    'breakthrough therapy', 'accelerated approval',
    'acquisition', 'merger', 'takeover', 'buyout',
    'going private', 'strategic review',
    'spacex', 'nasa contract', 'dod contract', 'government contract',
    'nasdaq compliance', 'listing restored', 'regains compliance',
]

CATALYST_MEDIUM = [
    'partnership', 'collaboration', 'license agreement', 'licensing deal',
    'contract awarded', 'contract won', 'awarded contract',
    'positive trial', 'positive results', 'positive data', 'phase 3',
    'earnings beat', 'beats estimates', 'raises guidance',
    'upgrade', 'price target raised',
    'breakthrough', 'clearance', 'approved',
    'grant', 'raises', 'secures funding',
]

CATALYST_LOW = [
    'conference', 'presentation', 'webinar', 'investor day',
    'at the', 'joins', 'appoints', 'names',
    'announces participation', 'to present',
    'quarterly results', 'annual report',
]

CATALYST_NEGATIVE = [
    'sec investigation', 'sec subpoena', 'class action', 'securities fraud',
    'short seller', 'short-seller', 'fraud allegation',
    'delisted', 'delisting', 'nasdaq deficiency',
    'bankruptcy', 'chapter 11', 'going concern',
    'recall', 'clinical hold', 'fda reject', 'complete response letter',
    'downgrade', 'miss', 'misses estimates', 'fails to',
]


def score_ticker(ticker_data, dark_pool_flow=None, finnhub_data=None,
                 uw_flow_data=None, news_data=None, technical_data=None):
    score   = 0
    reasons = []
    flags   = []  # czerwone flagi

    ticker = ticker_data.get('ticker', '')
    price  = ticker_data.get('price', 0)
    vwap   = ticker_data.get('vwap', 0)
    high   = ticker_data.get('high', 0)
    low    = ticker_data.get('low', 0)
    change = ticker_data.get('change_pct', 0)
    ratio  = ticker_data.get('volume_ratio', 1.0)
    gap    = ticker_data.get('gap_pct', 0.0)
    fshares = ticker_data.get('float_shares')

    # ================================================================
    # TIER 1: SMART MONEY (max 50 pkt) - sygnaly PRZED ruchem
    # ================================================================

    if uw_flow_data:
        call_put       = float(uw_flow_data.get('call_put_ratio', 1.0) or 1.0)
        unusual        = uw_flow_data.get('unusual', False)
        bullish_prem   = float(uw_flow_data.get('bullish_premium', 0) or 0)
        bearish_prem   = float(uw_flow_data.get('bearish_premium', 0) or 0)
        call_ask_side  = int(uw_flow_data.get('call_ask_side', 0) or 0)
        call_bid_side  = int(uw_flow_data.get('call_bid_side', 0) or 0)

        # Kierunek z ask/bid
        net_direction = call_ask_side - call_bid_side
        is_bullish_uw = (bullish_prem > bearish_prem) or (net_direction > 0)

        if unusual and call_put >= 5.0 and is_bullish_uw:
            score += 40
            reasons.append(f"UW unusual CALL flow {call_put:.1f}x (bullish confirmed)")
        elif unusual and call_put >= 5.0:
            score += 25
            reasons.append(f"UW unusual flow {call_put:.1f}x (kierunek niepewny)")
        elif unusual and call_put >= 2.0 and is_bullish_uw:
            score += 30
            reasons.append(f"UW unusual flow {call_put:.1f}x (bullish)")
        elif unusual and call_put >= 2.0:
            score += 18
            reasons.append(f"UW unusual flow {call_put:.1f}x")
        elif unusual:
            score += 10
            reasons.append("UW unusual flow (smart money aktywny)")

        # Bearish UW = red flag
        if unusual and call_put < 0.5:
            flags.append("UW bearish PUT flow")
            score -= 10

    if dark_pool_flow:
        dp_trades = [dp for dp in dark_pool_flow if dp.get('ticker') == ticker]
        if dp_trades:
            total_usd = sum(dp.get('size_usd', 0) for dp in dp_trades)
            buy_usd   = sum(dp.get('size_usd', 0) for dp in dp_trades
                           if dp.get('side') == 'BUY')
            sell_usd  = sum(dp.get('size_usd', 0) for dp in dp_trades
                           if dp.get('side') == 'SELL')

            if total_usd >= 1_000_000 and buy_usd > sell_usd:
                score += 20
                reasons.append(f"Dark pool BUY ${total_usd/1e6:.1f}M")
            elif total_usd >= 1_000_000:
                score += 10
                reasons.append(f"Dark pool ${total_usd/1e6:.1f}M (dir. niepewny)")
            elif total_usd >= 500_000:
                score += 6
                reasons.append(f"Dark pool ${total_usd/1e3:.0f}k")

            # Dark pool SELL = red flag
            if sell_usd > buy_usd * 2 and total_usd >= 500_000:
                flags.append("Dark pool SELL pressure")
                score -= 8

    # ================================================================
    # TIER 2: FUNDAMENTALNY KATALIZATOR (max 35 pkt)
    # ================================================================

    if news_data:
        best_catalyst   = None   # HIGH / MEDIUM / LOW / NEGATIVE
        best_catalyst_age_h = 999
        catalyst_counts = {'HIGH': 0, 'MEDIUM': 0, 'LOW': 0, 'NEGATIVE': 0}
        now_utc = datetime.now(timezone.utc)

        for n in news_data:
            title    = (n.get('title', '') or '').lower()
            desc     = (n.get('description', '') or '').lower()
            pub_utc  = n.get('published_utc', '')
            insights = n.get('insights', [])
            sentiment = insights[0].get('sentiment', '') if insights else ''

            # Wiek newsa
            news_age_h = 999
            if pub_utc:
                try:
                    pub_dt = datetime.fromisoformat(pub_utc.replace('Z', '+00:00'))
                    news_age_h = (now_utc - pub_dt).total_seconds() / 3600
                except Exception:
                    pass

            # Catalyst Quality Score
            catalyst = None
            if any(w in title or w in desc for w in CATALYST_NEGATIVE) or \
               sentiment == 'negative':
                catalyst = 'NEGATIVE'
            elif any(w in title or w in desc for w in CATALYST_HIGH):
                catalyst = 'HIGH'
            elif any(w in title or w in desc for w in CATALYST_MEDIUM) or \
                 sentiment == 'positive':
                catalyst = 'MEDIUM'
            elif any(w in title or w in desc for w in CATALYST_LOW):
                catalyst = 'LOW'

            if catalyst:
                catalyst_counts[catalyst] += 1
                # Zachowaj najlepszy swiezy katalizator
                priority = {'HIGH': 4, 'MEDIUM': 3, 'LOW': 1, 'NEGATIVE': 0}
                if catalyst != 'NEGATIVE':
                    if best_catalyst is None or \
                       priority.get(catalyst, 0) > priority.get(best_catalyst, 0) or \
                       (catalyst == best_catalyst and news_age_h < best_catalyst_age_h):
                        best_catalyst = catalyst
                        best_catalyst_age_h = news_age_h

        # Score bazowy wg jakosci katalizatora
        publisher = news_data[0].get('publisher', {}) if news_data else {}
        source = (publisher.get('name', '') or '').lower() \
                 if isinstance(publisher, dict) else ''
        is_premium = any(ps in source for ps in PREMIUM_NEWS_SOURCES)
        premium_mult = 1.4 if is_premium else 1.0

        if catalyst_counts['HIGH'] > 0:
            base = int(35 * premium_mult)
            score += base
            reasons.append(f"Catalyst HIGH x{catalyst_counts['HIGH']} "
                           f"({'premium ' if is_premium else ''}{base}pkt)")
        elif catalyst_counts['MEDIUM'] > 0:
            base = int(18 * premium_mult)
            score += base
            reasons.append(f"Catalyst MEDIUM x{catalyst_counts['MEDIUM']} "
                           f"({base}pkt)")
        elif catalyst_counts['LOW'] > 0:
            score += 4
            reasons.append(f"News LOW quality ({catalyst_counts['LOW']})")
        elif news_data:
            score += 2
            reasons.append(f"News aktywnosc ({len(news_data)})")

        # Timestamp bonus
        if best_catalyst in ('HIGH', 'MEDIUM') and best_catalyst_age_h < 999:
            if best_catalyst_age_h <= 1:
                score += 20
                reasons.append(f"NEWS SWIEZY ({best_catalyst_age_h*60:.0f} min temu!)")
            elif best_catalyst_age_h <= 4:
                score += 12
                reasons.append(f"News swiezy ({best_catalyst_age_h:.1f}h temu)")
            elif best_catalyst_age_h <= 12:
                score += 6
                reasons.append(f"News dzisiaj ({best_catalyst_age_h:.0f}h temu)")
            elif best_catalyst_age_h <= 24:
                score += 2
                reasons.append(f"News wczoraj")

        # Kary za negatywne
        if catalyst_counts['NEGATIVE'] > 0:
            penalty = catalyst_counts['NEGATIVE'] * 8
            flags.append(f"Negative catalyst x{catalyst_counts['NEGATIVE']}")
            score -= penalty

    if finnhub_data:
        earnings = finnhub_data.get('earnings')
        insider  = finnhub_data.get('insider')

        if earnings:
            days = earnings.get('days_until', 99)
            prev = earnings.get('previous_surprise', '')
            if days == 0:
                score += 25
                reasons.append(f"EARNINGS DZISIAJ ({prev})")
            elif days <= 1:
                score += 20
                reasons.append(f"Earnings jutro ({prev})")
            elif days <= 3:
                score += 15
                reasons.append(f"Earnings za {days}d ({prev})")
            elif days <= 7:
                score += 8
                reasons.append(f"Earnings za {days}d")

        if insider and insider.get('type') == 'BUY':
            ival = insider.get('value_usd', 0)
            if ival >= 100_000:
                score += 10
                reasons.append(f"Insider BUY ${ival/1e3:.0f}k")
            elif ival >= 50_000:
                score += 6
                reasons.append(f"Insider BUY ${ival/1e3:.0f}k")

    # ================================================================
    # TIER 3: FLOAT + GAP - struktura ruchu (max 30 pkt)
    # ================================================================

    # Float - niski float = wieksze ruchy
    if fshares:
        float_m = fshares / 1_000_000
        if float_m < 5:
            score += 20
            reasons.append(f"Float {float_m:.1f}M (ultra low)")
        elif float_m < 15:
            score += 15
            reasons.append(f"Float {float_m:.1f}M (low float)")
        elif float_m < 50:
            score += 8
            reasons.append(f"Float {float_m:.0f}M (mid float)")
        # High float > 200M = slabszy ruch
        elif float_m > 200:
            score -= 3

    # Gap analysis - jakos otwarcia
    if gap != 0:
        if 5.0 <= gap <= 25.0:
            score += 15
            reasons.append(f"Gap up {gap:+.1f}% (idealny)")
        elif 25.0 < gap <= 50.0:
            score += 10
            reasons.append(f"Gap up {gap:+.1f}% (duzy)")
        elif gap > 50.0:
            score += 5
            reasons.append(f"Gap up {gap:+.1f}% (ekstremalny - ryzyko fill)")
        elif gap < -10.0:
            flags.append(f"Gap down {gap:+.1f}%")
            score -= 8

    # ================================================================
    # TIER 4: TECHNICZNE (max 25 pkt)
    # ================================================================

    # VWAP
    if vwap > 0 and price > 0:
        vwap_pct = ((price - vwap) / vwap) * 100
        if 0 < vwap_pct <= 2.0:
            score += 12
            reasons.append(f"VWAP reclaim ({vwap_pct:+.1f}%)")
        elif 2.0 < vwap_pct <= 5.0:
            score += 8
            reasons.append(f"Powyzej VWAP ({vwap_pct:+.1f}%)")
        elif vwap_pct > 5.0:
            score += 4
            reasons.append(f"Daleko nad VWAP ({vwap_pct:+.1f}%)")
        elif -1.5 <= vwap_pct < 0:
            score += 3
            reasons.append(f"Blisko VWAP ({vwap_pct:+.1f}%)")
        elif vwap_pct < -5.0:
            flags.append(f"Ponizej VWAP ({vwap_pct:+.1f}%)")
            score -= 5

    # RSI i EMA
    if technical_data:
        rsi   = technical_data.get('rsi')
        ema9  = technical_data.get('ema9')
        ema21 = technical_data.get('ema21')

        if rsi is not None:
            if 45 <= rsi <= 65:
                score += 8
                reasons.append(f"RSI {rsi:.0f} (sweet spot)")
            elif 35 <= rsi < 45:
                score += 5
                reasons.append(f"RSI {rsi:.0f} (buduje momentum)")
            elif rsi < 30:
                score += 3
                reasons.append(f"RSI {rsi:.0f} (oversold bounce?)")
            elif 65 < rsi <= 75:
                score += 2
                reasons.append(f"RSI {rsi:.0f} (momentum)")
            elif rsi > 75:
                flags.append(f"RSI {rsi:.0f} overbought")
                score -= 8

        if ema9 and ema21:
            if ema9 > ema21 * 1.003:
                score += 8
                reasons.append("EMA9 > EMA21 (trend bullish)")
            elif ema9 > ema21:
                score += 4
                reasons.append("EMA9 > EMA21 (lekko bullish)")
            elif ema9 < ema21 * 0.997:
                flags.append("EMA9 < EMA21 bearish")
                score -= 5

    # HOD breakout
    if high > 0 and price > 0:
        hod_pct = ((price - high) / high) * 100
        if hod_pct >= 0:
            score += 8
            reasons.append("HOD breakout!")
        elif hod_pct >= -1.0:
            score += 5
            reasons.append(f"Blisko HOD ({hod_pct:+.1f}%)")

    # Range position
    if high > low > 0 and price > 0:
        day_range = high - low
        if day_range > 0:
            pos = (price - low) / day_range
            if pos >= 0.85:
                score += 4
                reasons.append(f"Range {pos:.0%} (gorny zakres)")

    # ================================================================
    # TIER 5: VOLUME MOMENTUM (max 20 pkt)
    # ================================================================

    # Rozrozniamy wczesny vs pozny ruch
    if ratio >= 10.0 and change >= 20.0:
        # Extreme momentum - moze byc pump lub prawdziwy runner
        score += 12
        reasons.append(f"Extreme: vol {ratio:.0f}x + {change:+.0f}%")
    elif ratio >= 5.0 and change >= 10.0:
        score += 15
        reasons.append(f"Strong momentum: vol {ratio:.1f}x + {change:+.1f}%")
    elif 2.0 <= ratio < 5.0 and 5.0 <= change <= 20.0:
        score += 20
        reasons.append(f"Sweet spot: vol {ratio:.1f}x + {change:+.1f}%")
    elif 1.5 <= ratio < 2.0 and 3.0 <= change <= 8.0:
        score += 15
        reasons.append(f"Wczesny ruch: vol {ratio:.1f}x + {change:+.1f}%")
    elif 1.5 <= ratio < 5.0:
        score += 8
        reasons.append(f"Volume {ratio:.1f}x (podwyzszony)")
    elif ratio >= 5.0:
        score += 6
        reasons.append(f"Volume {ratio:.1f}x (high - moze pozno)")

    # Zmiana ceny extra bonus
    abs_chg = abs(change)
    if 5.0 <= abs_chg <= 15.0 and change > 0:
        score += 5
        reasons.append(f"Zmiana {change:+.1f}%")
    elif abs_chg > 15.0 and change > 0:
        score += 3

    # ================================================================
    # PENALTIES - czerwone flagi
    # ================================================================

    # Cena groszowa (ale powyzej naszego minimum) - wieksze ryzyko
    if price < 1.0:
        score -= 5
        flags.append(f"Cena groszowa ${price:.2f}")

    # Bardzo wysoki ratio BEZ zmiany = podejrzane
    if ratio > 10.0 and abs(change) < 2.0:
        flags.append(f"Vol {ratio:.0f}x bez ruchu cenowego")
        score -= 10

    # Dodaj flagi do reasons
    for flag in flags:
        reasons.append(f"⚠️ {flag}")

    return score, reasons


# ================================================================
# FILTROWANIE BAZOWE
# ================================================================

def is_excluded_ticker(ticker_data, polygon_api=None):
    ticker = ticker_data.get('ticker', '').upper()
    price  = ticker_data.get('price', 0)
    volume = ticker_data.get('volume', 0)

    if price < MIN_PRICE_QUALITY:
        return True, f"cena ${price:.2f} < ${MIN_PRICE_QUALITY}"

    if volume < MIN_VOLUME_SCORING:
        return True, f"volume {volume:,} < {MIN_VOLUME_SCORING:,}"

    ticker_type = ticker_data.get('ticker_type', '')
    if ticker_type and ticker_type.upper() in EXCLUDED_TYPES:
        return True, f"typ {ticker_type}"

    for suffix in EXCLUDED_SUFFIXES:
        if ticker.endswith(suffix) and len(ticker) > len(suffix):
            return True, f"sufiks {suffix}"

    if '.' in ticker:
        if ticker.endswith('.RT') or ticker.endswith('.WS'):
            return True, "prawa/warrant (.RT/.WS)"

    return False, ""


def apply_base_filters(universe, polygon_api=None):
    passed = []
    rejected_count = 0
    rejected_reasons = {}

    for ticker in universe:
        price  = ticker.get('price', 0)
        volume = ticker.get('volume', 0)

        if not (CONFIG['min_price'] <= price <= CONFIG['max_price']):
            rejected_count += 1
            continue

        if volume < CONFIG.get('min_volume', 100_000):
            rejected_count += 1
            continue

        excluded, reason = is_excluded_ticker(ticker, polygon_api)
        if excluded:
            rejected_count += 1
            rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
            continue

        passed.append(ticker)

    logger.info(f"Pre-filter: {len(universe)} -> {len(passed)} "
                f"(odrzucono {rejected_count})")

    if rejected_reasons:
        top = sorted(rejected_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
        logger.info(f"  Powody: {', '.join(f'{r}:{c}' for r,c in top)}")

    return passed


def rank_tickers(filtered_universe, dark_pool_flow=None, finnhub_cache=None,
                 uw_flow_cache=None, news_cache=None, technical_cache=None):
    scored = []

    for ticker_data in filtered_universe:
        sym = ticker_data.get('ticker', '')

        score, reasons = score_ticker(
            ticker_data,
            dark_pool_flow  = dark_pool_flow,
            finnhub_data    = finnhub_cache.get(sym) if finnhub_cache else None,
            uw_flow_data    = uw_flow_cache.get(sym) if uw_flow_cache else None,
            news_data       = news_cache.get(sym) if news_cache else None,
            technical_data  = technical_cache.get(sym) if technical_cache else None,
        )

        scored.append({
            'ticker':       sym,
            'score':        score,
            'reasons':      reasons,
            'price':        ticker_data.get('price', 0),
            'change_pct':   ticker_data.get('change_pct', 0),
            'volume':       ticker_data.get('volume', 0),
            'volume_ratio': ticker_data.get('volume_ratio', 1.0),
            'gap_pct':      ticker_data.get('gap_pct', 0),
            'float_shares': ticker_data.get('float_shares'),
            'raw_data':     ticker_data,
        })

    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored


def get_consecutive_boost(ticker, signal_history_fn):
    """
    Consecutive Boost — ticker ktory dostat BUY 2x z rzedu = bonus.
    Ticker z AVOID 2x z rzedu = penalty.
    """
    if not signal_history_fn:
        return 0, ''
    try:
        history = signal_history_fn(ticker, limit=3)
        if not history or len(history) < 2:
            return 0, ''

        last_two = [h.get('verdict') for h in history[:2]]

        if last_two == ['BUY', 'BUY']:
            return 10, 'Consecutive BUY x2 (potwierdzony setup)'
        elif last_two == ['AVOID', 'AVOID']:
            return -10, 'Consecutive AVOID x2'
        elif last_two[0] == 'BUY':
            return 5, 'Poprzedni sygnał BUY'
    except Exception:
        pass
    return 0, ''


def get_top_tickers(universe, dark_pool_flow=None, finnhub_cache=None,
                    uw_flow_cache=None, news_cache=None, technical_cache=None,
                    top_n=None, polygon_api=None, signal_history_fn=None):
    if top_n is None:
        top_n = CONFIG['max_tickers_for_claude']

    filtered = apply_base_filters(universe, polygon_api)
    if not filtered:
        logger.warning("Pre-filter: brak tickerow po filtrach bazowych")
        return []

    ranked = rank_tickers(
        filtered,
        dark_pool_flow  = dark_pool_flow,
        finnhub_cache   = finnhub_cache,
        uw_flow_cache   = uw_flow_cache,
        news_cache      = news_cache,
        technical_cache = technical_cache,
    )

    min_score = get_dynamic_threshold()

    # Zastosuj Consecutive Boost przed finalnym rankingiem
    if signal_history_fn:
        for t in ranked:
            boost, reason = get_consecutive_boost(t['ticker'], signal_history_fn)
            if boost != 0:
                t['score']  += boost
                if reason:
                    t['reasons'].append(reason)
        ranked.sort(key=lambda x: x['score'], reverse=True)

    top = [t for t in ranked[:top_n * 3] if t['score'] >= min_score][:top_n]

    if not top:
        # Fallback - zwroc top_n bez filtra score
        top = ranked[:top_n]
        logger.info(f"Pre-filter: fallback (brak tickerow z min_score {min_score})")

    logger.info(f"Pre-filter TOP {len(top)} [threshold={min_score}]:") 
    for t in top:
        gap_str = f" gap{t['gap_pct']:+.0f}%" if t.get('gap_pct') else ""
        logger.info(f"  {t['ticker']:6s} | score: {t['score']:3d} | "
                    f"${t['price']:.2f} | {t['change_pct']:+.1f}%"
                    f" | vol {t['volume_ratio']:.1f}x{gap_str}")

    return top


def uw_fast_track(dark_pool_flow, current_top_tickers):
    if not dark_pool_flow:
        return []

    current_symbols = {t['ticker'] for t in current_top_tickers}
    fast_track      = []

    for dp in dark_pool_flow:
        ticker = dp.get('ticker', '')
        size   = dp.get('size_usd', 0)
        side   = dp.get('side', '')

        if ticker not in current_symbols and size >= 500_000 and side == 'BUY':
            fast_track.append({
                'ticker':   ticker,
                'reason':   f"UW dark pool ${size:,} BUY — fast track",
                'size_usd': size,
            })
            logger.info(f"UW Fast Track: {ticker} | ${size:,}")

    return fast_track


if __name__ == "__main__":
    print("Pre-filter v4.0 OK")
