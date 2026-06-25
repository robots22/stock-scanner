#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 3: PRE-FILTER v3.0
Zapisz jako pre_filter.py w folderze stock-scanner

Hierarchia scoring v3.0:
TIER 1 - Smart Money (przed ruchem): UW flow, dark pool
TIER 2 - Katalizator: news, earnings, insider
TIER 3 - Techniczne: RSI, EMA, VWAP, HOD
TIER 4 - Volume (zmniejszony)

Historia zmian:
    v1.0 - pierwsza wersja
    v2.0 - options flow i news jako liderzy
    v3.0 - pelna hierarchia, RSI/EMA/VWAP/HOD, cena min $1, WW suffix
"""

from config import logger, CONFIG

EXCLUDED_TYPES    = {'WARRANT', 'RIGHT', 'UNIT', 'FUND', 'SP'}
EXCLUDED_SUFFIXES = ('W', 'WS', 'WW', 'R', 'RT', 'U')
MIN_PRICE_QUALITY = 1.00
MIN_VOLUME_SCORING = 50_000
PREMIUM_NEWS_SOURCES = {
    'reuters', 'bloomberg', 'wsj', 'ft', 'cnbc', 'marketwatch',
    'sec.gov', 'businesswire', 'globenewswire', 'accessnewswire',
    'prnewswire', 'accesswire'
}


def score_ticker(ticker_data, dark_pool_flow=None, finnhub_data=None,
                 uw_flow_data=None, news_data=None, technical_data=None):
    score   = 0
    reasons = []

    price  = ticker_data.get('price', 0)
    vwap   = ticker_data.get('vwap', 0)
    high   = ticker_data.get('high', 0)
    low    = ticker_data.get('low', 0)
    change = ticker_data.get('change_pct', 0)
    ratio  = ticker_data.get('volume_ratio', 1.0)

    # TIER 1: SMART MONEY (0-50 pkt)
    if uw_flow_data:
        call_put = uw_flow_data.get('call_put_ratio', 1.0)
        unusual  = uw_flow_data.get('unusual', False)

        if unusual and call_put >= 5.0:
            score += 30
            reasons.append(f"UW unusual flow {call_put:.1f}x (ekstremalny)")
        elif unusual and call_put >= 2.0:
            score += 20
            reasons.append(f"UW unusual flow {call_put:.1f}x")
        elif unusual:
            score += 12
            reasons.append("UW unusual flow (smart money aktywny)")

    if dark_pool_flow:
        ticker_sym = ticker_data.get('ticker', '')
        dp_trades  = [dp for dp in dark_pool_flow if dp.get('ticker') == ticker_sym]
        if dp_trades:
            total_usd = sum(dp.get('size_usd', 0) for dp in dp_trades)
            buy_usd   = sum(dp.get('size_usd', 0) for dp in dp_trades if dp.get('side') == 'BUY')
            sell_usd  = sum(dp.get('size_usd', 0) for dp in dp_trades if dp.get('side') == 'SELL')

            if total_usd >= 1_000_000 and buy_usd > sell_usd:
                score += 20
                reasons.append(f"Dark pool BUY ${total_usd:,.0f}")
            elif total_usd >= 1_000_000:
                score += 12
                reasons.append(f"Dark pool ${total_usd:,.0f} (kierunek niepewny)")
            elif total_usd >= 500_000:
                score += 8
                reasons.append(f"Dark pool ${total_usd:,.0f}")

    # TIER 2: KATALIZATOR (0-30 pkt)
    if news_data:
        bullish_premium = bullish_regular = bearish_count = 0
        for n in news_data:
            title     = n.get('title', '').lower()
            publisher = n.get('publisher', {})
            source    = publisher.get('name', '').lower() if isinstance(publisher, dict) else ''
            sentiment = n.get('insights', [{}])[0].get('sentiment', '') if n.get('insights') else ''

            is_bullish = sentiment == 'positive' or any(w in title for w in [
                'fda approval', 'approved', 'breakthrough', 'contract', 'partnership',
                'beat', 'upgrade', 'raises', 'grant', 'clearance', 'positive trial',
                'positive data', 'acquisition', 'merger', 'license'
            ])
            is_bearish = sentiment == 'negative' or any(w in title for w in [
                'downgrade', 'miss', 'recall', 'investigation', 'fraud',
                'lawsuit', 'bankruptcy', 'delisted', 'fails'
            ])
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
            reasons.append("Bullish news (premium source) - katalizator")
        elif bullish_regular >= 2:
            score += 18
            reasons.append(f"Bullish news: {bullish_regular} artykuly")
        elif bullish_regular == 1:
            score += 12
            reasons.append("Bullish news: katalizator")
        elif bearish_count > 0:
            score += 3
            reasons.append(f"Bearish news: {bearish_count}")
        elif news_data:
            score += 2
            reasons.append(f"News aktywnosc: {len(news_data)} artykulow")

    if finnhub_data:
        earnings = finnhub_data.get('earnings')
        insider  = finnhub_data.get('insider')

        if earnings:
            days = earnings.get('days_until', 99)
            prev = earnings.get('previous_surprise', '')
            if days <= 1:
                score += 20
                reasons.append(f"Earnings za {days} dni ({prev})")
            elif days <= 3:
                score += 15
                reasons.append(f"Earnings za {days} dni ({prev})")
            elif days <= 7:
                score += 8
                reasons.append(f"Earnings za {days} dni")

        if insider and insider.get('type') == 'BUY':
            ival = insider.get('value_usd', 0)
            if ival >= 50_000:
                score += 5
                reasons.append(f"Insider BUY ${ival:,}")

    # TIER 3: TECHNICZNE (0-30 pkt)
    if 1.5 <= ratio <= 2.5 and 3.0 <= change <= 8.0:
        score += 25
        reasons.append(f"Sweet spot: vol {ratio:.1f}x + zmiana {change:+.1f}%")
    elif 2.5 < ratio <= 4.0 and 8.0 < change <= 15.0:
        score += 15
        reasons.append(f"Momentum: vol {ratio:.1f}x + zmiana {change:+.1f}%")
    elif 1.5 <= ratio <= 2.5:
        score += 12
        reasons.append(f"Volume {ratio:.1f}x (wczesny sygnal)")
    elif 2.5 < ratio <= 4.0:
        score += 8
        reasons.append(f"Volume {ratio:.1f}x (wysoki)")

    if vwap > 0 and price > 0:
        vwap_pct = ((price - vwap) / vwap) * 100
        if 0 < vwap_pct <= 2.0:
            score += 10
            reasons.append(f"VWAP reclaim ({vwap_pct:+.1f}% nad VWAP)")
        elif vwap_pct > 2.0:
            score += 5
            reasons.append(f"Powyzej VWAP ({vwap_pct:+.1f}%)")
        elif -1.0 <= vwap_pct < 0:
            score += 3
            reasons.append(f"Blisko VWAP ({vwap_pct:+.1f}%)")

    if technical_data:
        rsi  = technical_data.get('rsi')
        ema9 = technical_data.get('ema9')
        ema21= technical_data.get('ema21')

        if rsi is not None:
            if 40 <= rsi <= 65:
                score += 8
                reasons.append(f"RSI {rsi:.0f} (sweet spot)")
            elif 30 <= rsi < 40:
                score += 4
                reasons.append(f"RSI {rsi:.0f} (oversold bounce?)")
            elif rsi > 75:
                score -= 8
                reasons.append(f"RSI {rsi:.0f} (overbought - ryzyko reversal)")

        if ema9 and ema21:
            if ema9 > ema21 * 1.002:
                score += 7
                reasons.append("EMA9 > EMA21 (momentum bullish)")
            elif ema9 < ema21 * 0.998:
                score -= 4
                reasons.append("EMA9 < EMA21 (momentum bearish)")

    if high > 0 and price > 0:
        hod_pct = ((price - high) / high) * 100
        if hod_pct >= -1.0:
            score += 5
            reasons.append(f"HOD breakout ({hod_pct:+.1f}% od szczytu)")

    # TIER 4: VOLUME LATE (0-8 pkt)
    if ratio > 5.0:
        score += 8
        reasons.append(f"Volume {ratio:.1f}x (moze za pozno)")
    if change > 15.0:
        score += 5
        reasons.append(f"Zmiana {change:+.1f}% (duzy ruch)")

    return score, reasons


def is_excluded_ticker(ticker_data, polygon_api=None):
    ticker = ticker_data.get('ticker', '').upper()
    price  = ticker_data.get('price', 0)
    volume = ticker_data.get('volume', 0)

    if price < MIN_PRICE_QUALITY:
        return True, f"cena ${price:.2f} < $1.00"

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

    logger.info(f"Pre-filter: {len(universe)} -> {len(passed)} tickerow "
                f"(odrzucono {rejected_count} po filtrach bazowych)")

    if rejected_reasons:
        top_reasons = sorted(rejected_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
        logger.info(f"  Glowne powody odrzucenia: "
                    f"{', '.join(f'{r}:{c}' for r,c in top_reasons)}")

    return passed


def rank_tickers(filtered_universe, dark_pool_flow=None, finnhub_cache=None,
                 uw_flow_cache=None, news_cache=None, technical_cache=None):
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
    if top_n is None:
        top_n = CONFIG['max_tickers_for_claude']

    filtered = apply_base_filters(universe, polygon_api)

    if not filtered:
        logger.warning("Pre-filter: brak tickerow po filtrach bazowych")
        return []

    ranked = rank_tickers(
        filtered,
        dark_pool_flow=dark_pool_flow,
        finnhub_cache=finnhub_cache,
        uw_flow_cache=uw_flow_cache,
        news_cache=news_cache,
        technical_cache=technical_cache,
    )

    top = ranked[:top_n]

    logger.info(f"Pre-filter: wyloniono TOP {len(top)} tickerow dla Claude'a")
    for t in top:
        logger.info(f"  {t['ticker']:6s} | score: {t['score']:3d} | "
                    f"${t['price']:.2f} | {t['change_pct']:+.1f}% | "
                    f"vol {t['volume_ratio']:.1f}x")

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
                'reason':   f"UW dark pool ${size:,} — fast track",
                'size_usd': size,
            })
            logger.info(f"UW Fast Track: {ticker} | ${size:,}")

    return fast_track


if __name__ == "__main__":
    print("Pre-filter v3.0 OK")
