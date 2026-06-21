#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 4: CLAUDE AI ANALITYK
Zapisz jako claude_analyst.py w folderze stock-scanner

Zadanie: analizuje TOP 5 tickerów i wydaje werdykt
BUY / WATCH / AVOID + uzasadnienie.

Claude dostaje:
- Dane rynkowe (cena, wolumen, zmiana%)
- Dark pool i options flow
- Newsy ostatnie 24h
- Eventy (earnings, insider transactions)
- Historię ostatnich 5 sygnałów dla tickera z bazy danych
"""

import json
import re
from datetime import datetime
from config import logger, CONFIG, CLAUDE_CONFIG, ANTHROPIC_API_KEY, DEMO_MODE, now_chicago

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    logger.warning("Biblioteka anthropic nie zainstalowana — "
                   "uruchom: pip install anthropic")


# ==================== BUDOWANIE PROMPTU ====================

def build_prompt(ticker_data, news, options_flow, signal_history):
    """
    Buduje prompt dla Claude'a z wszystkimi dostępnymi danymi.
    Im więcej danych tym lepsza analiza.
    """
    ticker = ticker_data.get('ticker', 'UNKNOWN')
    price  = ticker_data.get('price', 0)
    change = ticker_data.get('change_pct', 0)
    volume = ticker_data.get('volume', 0)
    ratio  = ticker_data.get('volume_ratio', 1.0)
    high   = ticker_data.get('high', 0)
    low    = ticker_data.get('low', 0)
    vwap   = ticker_data.get('vwap', 0)

    # Pre-filter reasons (dlaczego ticker trafił do TOP 5)
    reasons = ticker_data.get('reasons', [])

    prompt = f"""Analizuj ticker: {ticker}

=== DANE RYNKOWE ===
Cena:          ${price:.2f}
Zmiana:        {change:+.2f}%
Wolumen:       {volume:,}
Volume ratio:  {ratio:.1f}x średniej 30-dniowej
High/Low:      ${high:.2f} / ${low:.2f}
VWAP:          ${vwap:.2f}
Pozycja vs VWAP: {'POWYŻEJ' if price > vwap else 'PONIŻEJ'} ({((price-vwap)/vwap*100):+.1f}%)

=== DLACZEGO TEN TICKER (pre-filter) ===
"""
    if reasons:
        for r in reasons:
            prompt += f"• {r}\n"
    else:
        prompt += "• Wysoki wolumen względem średniej\n"

    # Dark pool
    dark_pool = ticker_data.get('dark_pool')
    if dark_pool:
        prompt += f"""
=== DARK POOL ===
Strona:        {dark_pool.get('side', 'UNKNOWN')}
Wartość:       ${dark_pool.get('size_usd', 0):,}
"""

    # Options flow
    if options_flow:
        prompt += f"""
=== OPTIONS FLOW ===
Call volume:   {options_flow.get('call_volume', 0):,}
Put volume:    {options_flow.get('put_volume', 0):,}
Call/Put ratio: {options_flow.get('call_put_ratio', 1.0):.2f}
Sentyment:     {options_flow.get('sentiment', 'neutral').upper()}
Unusual:       {'TAK ⚠️' if options_flow.get('unusual') else 'NIE'}
"""

    # Newsy
    prompt += "\n=== NEWSY (ostatnie 24h) ===\n"
    if news:
        for n in news:
            sentiment = n.get('sentiment', 'neutral')
            icon = '🟢' if sentiment == 'bullish' else '🔴' if sentiment == 'bearish' else '⚪'
            prompt += f"{icon} {n.get('title', '')}\n"
    else:
        prompt += "Brak newsów w ostatnich 24h\n"

    # Finnhub events
    earnings = ticker_data.get('earnings')
    insider  = ticker_data.get('insider')

    if earnings or insider:
        prompt += "\n=== EVENTY ===\n"
        if earnings:
            days = earnings.get('days_until', 0)
            prev = earnings.get('previous_surprise', '')
            eps  = earnings.get('estimate_eps', 0)
            prompt += (f"📅 Earnings za {days} dni | "
                       f"EPS estimate: ${eps:.2f} | "
                       f"Poprzedni raport: {prev}\n")
        if insider:
            itype   = insider.get('type', '')
            ival    = insider.get('value_usd', 0)
            iperson = insider.get('person', '')
            idays   = insider.get('days_ago', 0)
            icon    = '🟢' if itype == 'BUY' else '🔴'
            prompt += (f"{icon} Insider {itype}: ${ival:,} "
                       f"przez {iperson} ({idays} dni temu)\n")

    # Historia sygnałów
    prompt += "\n=== HISTORIA SYGNAŁÓW (ostatnie 5) ===\n"
    if signal_history:
        for sig in signal_history:
            verdict  = sig.get('verdict', '')
            sig_price = sig.get('price', 0)
            sig_date  = sig.get('date', '')
            outcome  = sig.get('outcome_1h', '')
            icon = '🟢' if verdict == 'BUY' else '🟡' if verdict == 'WATCH' else '🔴'
            outcome_str = f" → wynik 1h: {outcome}" if outcome else ""
            prompt += f"{icon} {sig_date}: {verdict} @ ${sig_price:.2f}{outcome_str}\n"
    else:
        prompt += "Brak historii — pierwszy sygnał dla tego tickera\n"

    prompt += f"""
=== TWOJE ZADANIE ===
Na podstawie powyższych danych wydaj werdykt dla {ticker}.
Pamiętaj: szukamy prawdziwych ruchów, nie manipulacji (pump & dump).
Volume ratio > 3x bez fundamentalnego powodu to czerwona flaga.

Odpowiedz DOKŁADNIE w tym formacie:
WERDYKT: [BUY/WATCH/AVOID]
PEWNOŚĆ: [WYSOKA/ŚREDNIA/NISKA]
UZASADNIENIE: [2-3 zdania]
RYZYKO: [główne ryzyko w jednym zdaniu]"""

    return prompt


# ==================== PARSOWANIE ODPOWIEDZI ====================

def parse_claude_response(response_text, ticker):
    """
    Parsuje odpowiedź Claude'a na strukturowany słownik.
    Obsługuje małe różnice w formatowaniu.
    """
    result = {
        'ticker':         ticker,
        'verdict':        None,
        'confidence':     None,
        'justification':  None,
        'risk':           None,
        'raw_response':   response_text,
        'timestamp':      now_chicago().isoformat(),
    }

    lines = response_text.strip().split('\n')

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith('WERDYKT:'):
            val = line.split(':', 1)[1].strip().upper()
            if 'BUY' in val:
                result['verdict'] = 'BUY'
            elif 'WATCH' in val:
                result['verdict'] = 'WATCH'
            elif 'AVOID' in val:
                result['verdict'] = 'AVOID'

        elif line.startswith('PEWNOŚĆ:'):
            val = line.split(':', 1)[1].strip().upper()
            if 'WYSOKA' in val or 'HIGH' in val:
                result['confidence'] = 'WYSOKA'
            elif 'ŚREDNIA' in val or 'MEDIUM' in val:
                result['confidence'] = 'ŚREDNIA'
            elif 'NISKA' in val or 'LOW' in val:
                result['confidence'] = 'NISKA'

        elif line.startswith('UZASADNIENIE:'):
            result['justification'] = line.split(':', 1)[1].strip()

        elif line.startswith('RYZYKO:'):
            result['risk'] = line.split(':', 1)[1].strip()

    # Fallback jeśli parsowanie nie wyszło
    if not result['verdict']:
        logger.warning(f"Nie udało się sparsować werdyktu dla {ticker}")
        logger.warning(f"Odpowiedź Claude'a: {response_text[:200]}")
        result['verdict'] = 'WATCH'
        result['confidence'] = 'NISKA'
        result['justification'] = 'Błąd parsowania odpowiedzi.'
        result['risk'] = 'Nieznane — sprawdź logi.'

    return result


# ==================== MOCK CLAUDE (DEMO) ====================

def mock_claude_analyze(ticker_data, news, options_flow, signal_history):
    """
    Symuluje odpowiedź Claude'a w trybie DEMO.
    Używane gdy brak klucza ANTHROPIC_API_KEY.
    """
    import random

    ticker  = ticker_data.get('ticker', 'UNKNOWN')
    ratio   = ticker_data.get('volume_ratio', 1.0)
    change  = ticker_data.get('change_pct', 0)
    reasons = ticker_data.get('reasons', [])

    # Prosta logika mock — im wyższy score tym bardziej bullish
    score = ticker_data.get('score', 0)

    if score >= 50:
        verdict    = 'BUY'
        confidence = 'WYSOKA' if score >= 70 else 'ŚREDNIA'
        just = (f"{ticker} pokazuje silne sygnały: volume {ratio:.1f}x średniej, "
                f"zmiana {change:+.1f}%. "
                f"{'Dark pool i options flow potwierdzają kierunek.' if options_flow else 'Momentum sprzyja pozycji długiej.'}")
        risk = "Możliwy fałszywy breakout — obserwuj wolumen w kolejnych minutach."

    elif score >= 25:
        verdict    = 'WATCH'
        confidence = 'ŚREDNIA'
        just = (f"{ticker} na radarze — volume {ratio:.1f}x ale brak potwierdzenia "
                f"ze strony dark pool lub opcji. Zmiana {change:+.1f}% wymaga "
                f"weryfikacji czy jest podtrzymana.")
        risk = "Brak wystarczającego potwierdzenia sygnału — ryzyko fałszywego ruchu."

    else:
        verdict    = 'AVOID'
        confidence = 'WYSOKA'
        just = (f"{ticker} nie spełnia kryteriów — volume ratio {ratio:.1f}x "
                f"i zmiana {change:+.1f}% niewystarczające. "
                f"Brak katalizatora fundamentalnego.")
        risk = "Ryzyko wejścia bez katalizatora — możliwy ruch w obie strony."

    # Symuluj opóźnienie API
    import time
    time.sleep(0.3)

    return {
        'ticker':        ticker,
        'verdict':       verdict,
        'confidence':    confidence,
        'justification': just,
        'risk':          risk,
        'raw_response':  f"[MOCK] {verdict}",
        'timestamp':     now_chicago().isoformat(),
        'demo_mode':     True,
    }


# ==================== GŁÓWNY ANALITYK ====================

class ClaudeAnalyst:
    """
    Główny analityk AI systemu.
    W trybie DEMO używa mock_claude_analyze().
    W trybie LIVE używa prawdziwego Claude API.
    """

    def __init__(self):
        self.demo_mode = DEMO_MODE or not ANTHROPIC_API_KEY

        if self.demo_mode:
            logger.info("ClaudeAnalyst: tryb DEMO (mock odpowiedzi)")
            self.client = None
        else:
            if not ANTHROPIC_AVAILABLE:
                raise ImportError("Zainstaluj: pip install anthropic")
            self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            logger.info("ClaudeAnalyst: tryb LIVE (prawdziwe API)")

        self.total_calls    = 0
        self.total_cost_usd = 0.0

    def analyze(self, ticker_data, news=None, options_flow=None,
                signal_history=None):
        """
        Analizuje jeden ticker i zwraca werdykt.

        ticker_data    — dict z danymi rynkowymi (z pre_filter)
        news           — lista newsów z Polygon
        options_flow   — dict z UW options flow
        signal_history — lista ostatnich sygnałów z bazy danych
        """
        ticker = ticker_data.get('ticker', 'UNKNOWN')
        news           = news or []
        signal_history = signal_history or []

        logger.info(f"Claude analizuje: {ticker}")

        if self.demo_mode:
            result = mock_claude_analyze(
                ticker_data, news, options_flow, signal_history
            )
        else:
            result = self._call_claude_api(
                ticker_data, news, options_flow, signal_history
            )

        # Log werdyktu
        icon = '🟢' if result['verdict'] == 'BUY' \
               else '🟡' if result['verdict'] == 'WATCH' \
               else '🔴'
        logger.info(f"{icon} {ticker}: {result['verdict']} "
                    f"({result['confidence']}) — {result['justification'][:60]}...")

        return result

    def analyze_batch(self, top_tickers, polygon_api=None, uw_api=None,
                      db=None):
        """
        Analizuje listę tickerów (TOP 5) i zwraca wyniki.

        top_tickers — lista z pre_filter.get_top_tickers()
        polygon_api — instancja Polygon/MockPolygon (do newsów)
        uw_api      — instancja UW/MockUW (do options flow)
        db          — instancja bazy danych (do historii sygnałów)
        """
        results = []

        for ticker_data in top_tickers:
            ticker = ticker_data.get('ticker', 'UNKNOWN')

            # Pobierz newsy
            news = []
            if polygon_api:
                try:
                    news = polygon_api.get_news(ticker, limit=3)
                except Exception as e:
                    logger.warning(f"Błąd pobierania newsów dla {ticker}: {e}")

            # Pobierz options flow
            options_flow = None
            if uw_api:
                try:
                    options_flow = uw_api.get_options_flow(ticker)
                except Exception as e:
                    logger.warning(f"Błąd options flow dla {ticker}: {e}")

            # Pobierz historię sygnałów z bazy
            signal_history = []
            if db:
                try:
                    signal_history = db.get_signal_history(
                        ticker,
                        limit=CLAUDE_CONFIG['signal_history_count']
                    )
                except Exception as e:
                    logger.warning(f"Błąd historii dla {ticker}: {e}")

            # Analiza
            result = self.analyze(
                ticker_data,
                news=news,
                options_flow=options_flow,
                signal_history=signal_history,
            )
            results.append(result)

        logger.info(f"Claude przeanalizował {len(results)} tickerów | "
                    f"Łącznie wywołań: {self.total_calls}")
        return results

    def _call_claude_api(self, ticker_data, news, options_flow, signal_history):
        """
        Prawdziwe wywołanie Claude API.
        Używane tylko w trybie LIVE.
        """
        ticker = ticker_data.get('ticker', 'UNKNOWN')

        prompt = build_prompt(ticker_data, news, options_flow, signal_history)

        try:
            response = self.client.messages.create(
                model=CLAUDE_CONFIG['model'],
                max_tokens=CLAUDE_CONFIG['max_tokens'],
                system=CLAUDE_CONFIG['system_prompt'],
                messages=[
                    {'role': 'user', 'content': prompt}
                ]
            )

            response_text = response.content[0].text
            self.total_calls += 1

            # Szacunkowy koszt (Sonnet: $3/M input, $15/M output)
            input_tokens  = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000
            self.total_cost_usd += cost
            logger.info(f"Claude API: {input_tokens} in / {output_tokens} out | "
                        f"koszt: ${cost:.4f} | łącznie: ${self.total_cost_usd:.4f}")

            return parse_claude_response(response_text, ticker)

        except Exception as e:
            logger.error(f"Błąd Claude API dla {ticker}: {e}")
            return {
                'ticker':        ticker,
                'verdict':       'WATCH',
                'confidence':    'NISKA',
                'justification': f'Błąd API: {str(e)[:100]}',
                'risk':          'Błąd analizy — sprawdź logi.',
                'raw_response':  str(e),
                'timestamp':     now_chicago().isoformat(),
            }

    def get_stats(self):
        """Zwraca statystyki użycia API"""
        return {
            'total_calls':    self.total_calls,
            'total_cost_usd': round(self.total_cost_usd, 4),
            'demo_mode':      self.demo_mode,
        }


# ==================== TEST ====================

if __name__ == "__main__":
    from mock_polygon import MockPolygon, MockUnusualWhales, MockFinnhub
    from pre_filter import get_top_tickers

    print("\n" + "="*50)
    print("  TEST: Claude AI Analityk")
    print("="*50)

    # Pobierz dane
    polygon = MockPolygon()
    uw      = MockUnusualWhales()
    fh      = MockFinnhub()

    universe       = polygon.get_universe()
    dark_pool_flow = uw.get_dark_pool_flow()

    finnhub_cache = {}
    for t in universe:
        ticker = t['ticker']
        earnings = fh.get_earnings_calendar(ticker)
        insider  = fh.get_insider_transactions(ticker)
        if earnings or insider:
            finnhub_cache[ticker] = {
                'earnings': earnings,
                'insider':  insider,
            }
        # Dodaj do ticker_data żeby pre_filter mógł je uwzględnić
        t['earnings'] = earnings
        t['insider']  = insider

    # Wyłoń TOP 5
    top5 = get_top_tickers(
        universe,
        dark_pool_flow=dark_pool_flow,
        finnhub_cache=finnhub_cache,
        top_n=5
    )

    print(f"\nTOP {len(top5)} tickerów wyłonionych przez pre-filter")

    # Analiza przez Claude
    analyst = ClaudeAnalyst()
    results = analyst.analyze_batch(
        top5,
        polygon_api=polygon,
        uw_api=uw,
    )

    # Wyświetl wyniki
    print(f"\n{'='*50}")
    print("  WYNIKI ANALIZY CLAUDE AI")
    print(f"{'='*50}")

    for r in results:
        icon = '🟢' if r['verdict'] == 'BUY' \
               else '🟡' if r['verdict'] == 'WATCH' \
               else '🔴'
        print(f"\n{icon} {r['ticker']} — {r['verdict']} ({r['confidence']})")
        print(f"   {r['justification']}")
        print(f"   ⚠️  {r['risk']}")

    stats = analyst.get_stats()
    print(f"\n{'='*50}")
    print(f"  Wywołań API: {stats['total_calls']}")
    print(f"  Koszt: ${stats['total_cost_usd']:.4f}")
    print(f"  Tryb: {'DEMO' if stats['demo_mode'] else 'LIVE'}")
    print("="*50)
    print("  Plik 4 gotowy ✅")
    print("="*50)