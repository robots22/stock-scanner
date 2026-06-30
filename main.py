#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 7: GŁÓWNA PĘTLA
Zapisz jako main.py w folderze stock-scanner

Uruchomienie:
    python main.py

Zatrzymanie:
    Ctrl+C

Historia zmian:
    v1.0 — pierwsza wersja, monitoring przez Polygon (price_snapshots)
    v1.1 — monitoring przeniesiony na Unusual Whales (dark pool + options flow)
           - usunięto save_price_snapshot() z cyklu UW
           - _check_active_signals() pobiera UW options flow i dark pool co minute
           - triggery: TAKE_PROFIT, PRICE_REVERSAL, DARKPOOL_SELL,
                       OPTIONS_BEARISH, UW_ACTIVITY_GONE
    v1.2 — dodano trigger_cooldown (5 min) dla aktywnych BUY
           - zapobiega mnozeniu re-analiz tego samego tickera co minute
    v1.3 — podłączono prawdziwy UnusualWhalesAPI (DEMO_MODE = False)
           - Polygon i Finnhub nadal Mock do czasu ich wrapperów
           - UW: prawdziwy dark pool flow i options flow
    v1.4 — podłączono prawdziwy PolygonAPI (DEMO_MODE = False)
           - Finnhub nadal Mock do czasu jego wrappera
           - Polygon: prawdziwy universe, newsy, historia wolumenu
    v1.5 — podłączono prawdziwy FinnhubAPI (DEMO_MODE = False)
           - wszystkie trzy źródła danych są teraz prawdziwe
           - Polygon + UW + Finnhub: pełny LIVE mode bez Mock
    v1.6 — dodano AlpacaAPI jako backup danych rynkowych
           - get_ticker_with_fallback(): Polygon → Alpaca automatycznie
           - self.alpaca = None w trybie DEMO
    v1.7 — dodano extended hours (pre/after-market) i manualną analizę
    v1.8 — dodano filtr pierwszych 15 minut po otwarciu rynku

           - ceny small-cap niereliable w pierwszych minutach sesji
           - pomija cykl główny i UW scan do 8:45 CST
           - pre-market: 4:00-8:30 CST, co 15 min, vol > 10k
           - after-market: 15:00-20:00 CST, co 15 min, vol > 10k
           - Claude tylko dla tickerów z katalizatorem w extended hours
           - manual_queue: kolejka manualnych analiz z Telegram /analyze
"""

import time
import signal
import sys
import threading
from datetime import datetime, timedelta

from config import (logger, CONFIG, CLAUDE_CONFIG, DEMO_MODE, now_chicago,
                    is_market_open, is_premarket, is_aftermarket,
                    get_market_status, get_min_volume, get_dynamic_threshold,
                    CHICAGO_TZ)
from datetime import datetime
from mock_polygon import MockPolygon, MockUnusualWhales, MockFinnhub
from uw_api import UnusualWhalesAPI
from polygon_api import PolygonAPI
from finnhub_api import FinnhubAPI
from alpaca_api import AlpacaAPI, get_ticker_with_fallback
from pre_filter import get_top_tickers, uw_fast_track
from claude_analyst import ClaudeAnalyst
from database import (init_db, save_signal, get_signal_history,
                      get_active_buy_signals, check_retrigger_conditions,
                      save_retrigger, close_signal, update_outcomes,
                      get_stats)
from telegram_alerts import (alert_signal, alert_manual, alert_retrigger, alert_take_profit,
                              send_hourly_dashboard, send_startup_message,
                              send_shutdown_message, send_presession_watchlist,
                              send_afterhours_catalyst, send_message)
from telegram_bot import (start_bot_thread, manual_queue, manual_queue_lock,
                          system_paused, system_paused_lock, system_state)
from alpaca_trader import AlpacaPaperTrader
from momentum_scan import MomentumScanner
from float_cache import FloatCache


# ==================== GŁÓWNA KLASA ====================

class StockScanner:

    def __init__(self):
        self.running       = False
        self.demo_mode     = DEMO_MODE
        self._lock         = threading.Lock()

        # Inicjalizacja komponentów
        logger.info("Inicjalizacja komponentów...")

        # API (Mock lub prawdziwe)
        if self.demo_mode:
            self.polygon = MockPolygon()
            self.uw      = MockUnusualWhales()
            self.fh      = MockFinnhub()
            logger.info("Tryb DEMO — używam Mock API")
        else:
            # Etap 2 — wszystkie prawdziwe API
            self.polygon = PolygonAPI()        # ← prawdziwy Polygon
            self.uw      = UnusualWhalesAPI()  # ← prawdziwy UW
            self.fh      = FinnhubAPI()        # ← prawdziwy Finnhub
            self.alpaca  = AlpacaAPI()         # ← backup danych
            logger.info("Tryb LIVE — Polygon + UW + Finnhub + Alpaca backup")

        # Alpaca (tylko w trybie LIVE)
        if not self.demo_mode:
            pass  # już zainicjowany wyżej
        else:
            self.alpaca = None

        # Claude analityk
        self.analyst = ClaudeAnalyst()

        # Stan systemu
        self.current_top5       = []
        self.last_main_scan     = None
        self.last_uw_scan       = None
        self.last_dashboard     = None
        self.last_outcome_check = None
        self.scan_count         = 0
        self.alert_count        = 0

        # Cooldown alertów — zapobiega duplikatom
        self.alert_cooldown = {}

        # Cooldown triggerów — zapobiega re-analizie tego samego tickera
        # częściej niż raz na 5 minut
        self.trigger_cooldown = {}

        # Extended hours
        self.last_extended_scan = None

        # WATCH escalation tracker {ticker: count}
        self._watch_count = {}

        # Dzienny licznik BUY sygnalow - Opcja C Power Windows
        self._buy_count_today      = 0
        self._buy_count_open       = 0
        self._buy_count_midday     = 0
        self._buy_count_power_hour = 0
        self._buy_count_date       = None
        self._buy_tickers_today    = set()  # jeden BUY per ticker dziennie



        # Kolejka manualnych analiz (z Telegram /analyze TICKER)
        self.manual_queue       = []
        self.manual_cost_usd    = 0.0
        self._manual_lock       = threading.Lock()

        # Alpaca Paper Trader
        self.trader = AlpacaPaperTrader()

        # Momentum Scanner (Tryb 2) - bez Claude
        self.momentum = MomentumScanner(
            polygon_api     = self.polygon,
            telegram_send_fn = send_message,
        )

        # Float Cache - persystowany, odswiezany co 7 dni
        self.float_cache = FloatCache(polygon_api=self.polygon)

        # Telegram bot v2.0
        self.bot = None  # uruchamiany w run()

        # Sygnał Ctrl+C
        signal.signal(signal.SIGINT,  self._shutdown_handler)
        signal.signal(signal.SIGTERM, self._shutdown_handler)

        logger.info(f"StockScanner zainicjowany — "
                    f"tryb {'DEMO' if self.demo_mode else 'LIVE'}")

    # ==================== CYKL GŁÓWNY (5 min) ====================

    def run_main_scan(self):
        """
        Główny cykl skanowania — co 5 minut.
        Polygon + Finnhub → pre-filter → TOP 5 → Claude AI
        """
        logger.info(f"=== CYKL GŁÓWNY #{self.scan_count + 1} ===")

        # Pomijaj pierwsze 15 minut po otwarciu rynku
        if self._is_market_open_filter():
            filter_min = CONFIG.get('market_open_filter_minutes', 15)
            logger.info(f"Cykl główny pominięty — pierwsze {filter_min} min sesji")
            return

        # 1. Pobierz universe tickerów
        universe = self.polygon.get_universe()
        if not universe:
            logger.warning("Brak danych z Polygon — pomijam cykl")
            return

        # 2. Pobierz dark pool flow (UW)
        dark_pool_flow = self.uw.get_dark_pool_flow()

        # 3. Pobierz UW unusual flow (market-wide) i news dla pre-filtra
        uw_flow_cache = {}
        news_cache    = {}

        try:
            # Pobierz tickery z unusual options activity (market-wide)
            unusual_tickers = self.uw.get_tickers_with_unusual_flow(
                min_premium=20_000, limit=30
            )
            for t in unusual_tickers:
                ticker = t['ticker']
                uw_flow_cache[ticker] = {
                    'call_volume':    t.get('call_premium', 0),
                    'put_volume':     t.get('put_premium', 0),
                    'call_put_ratio': round(
                        t.get('call_premium', 0) /
                        max(t.get('put_premium', 1), 1), 2
                    ),
                    'unusual':   True,
                    'sentiment': 'bullish' if t.get('bullish') else 'bearish',
                }
            logger.info(f"UW unusual flow: {len(uw_flow_cache)} tickerów "
                        f"z options activity")
        except Exception as e:
            logger.warning(f"UW flow cache błąd: {e}")

        # News cache — market-wide scan ostatnie 2h
        # Pobieramy wszystkie swieze newsy naraz zamiast per-ticker
        # Nie przegapiamy tickerow z newsem ale niskim volume
        try:
            news_cache = self.polygon.get_recent_news_tickers(hours=2, limit=100)
        except Exception as e:
            logger.warning(f"Market news scan error: {e}")
            # Fallback: per-ticker dla TOP 30
            for t in sorted(universe, key=lambda x: x.get('volume_ratio', 0),
                            reverse=True)[:30]:
                try:
                    news = self.polygon.get_news(t['ticker'], limit=2)
                    if news:
                        news_cache[t['ticker']] = news
                except Exception:
                    pass

        # Pobierz RSI i EMA dla TOP 20 tickerów (wczesne sygnały)
        technical_cache = {}
        universe_rsi = sorted(
            universe,
            key=lambda x: x.get('volume_ratio', 0),
            reverse=True
        )[:20]

        for t in universe_rsi:
            ticker = t['ticker']
            try:
                rsi = self.polygon.get_rsi(ticker, window=14, timespan='minute')
                ema9, ema21 = self.polygon.get_ema(ticker, window=9, timespan='minute')
                if rsi or ema9:
                    technical_cache[ticker] = {
                        'rsi':  rsi,
                        'ema9': ema9,
                        'ema21': ema21,
                    }
            except Exception:
                pass

        if technical_cache:
            logger.info(f"Technical cache: {len(technical_cache)} tickerów z RSI/EMA")

        # Float dla calego universe przez FloatCache
        # Cache persystowany - gap nadal z get_float_and_gap dla TOP 20
        self.float_cache.enrich_universe(universe)

        # Gap dla TOP 20 po volume_ratio
        float_gap_cache = {}
        for t in universe_rsi:
            ticker = t['ticker']
            try:
                fg = self.polygon.get_float_and_gap(ticker)
                if fg.get('gap_pct'):
                    t['gap_pct']    = fg.get('gap_pct', 0)
                    t['prev_close'] = fg.get('prev_close', 0)
                    float_gap_cache[ticker] = fg
                # Float juz dodany przez float_cache.enrich_universe
            except Exception:
                pass

        logger.info(f"FloatCache stats: {self.float_cache.get_stats()}")

        # 4. Pre-filter → TOP 5 z options flow, news i technicznych
        top5 = get_top_tickers(
            universe,
            dark_pool_flow  = dark_pool_flow,
            uw_flow_cache   = uw_flow_cache,
            news_cache      = news_cache,
            technical_cache = technical_cache,
            top_n           = CONFIG['max_tickers_for_claude'],
            signal_history_fn = get_signal_history,
        )

        # Tryb 2: Momentum scan (bez Claude, bez kosztow)
        # Przekaz float_shares z cache do universe
        if is_market_open():
            try:
                for t in universe:
                    ticker = t.get('ticker', '')
                    if ticker in float_gap_cache:
                        t['float_shares'] = float_gap_cache[ticker].get('float_shares')
                n_alerts = self.momentum.scan(universe)
                if n_alerts:
                    logger.info(f"Momentum scan: {n_alerts} alertow wyslanych")
            except Exception as e:
                logger.warning(f"Momentum scan error: {e}")

        if not top5:
            logger.warning("Pre-filter: brak tickerów — pomijam analizę")
            return

        # 5. Pobierz dane Finnhub tylko dla TOP 5
        for t in top5:
            ticker = t['ticker']
            earnings = self.fh.get_earnings_calendar(ticker)
            insider  = self.fh.get_insider_transactions(ticker)
            t['earnings'] = earnings
            t['insider']  = insider
            if t.get('raw_data'):
                t['raw_data']['earnings'] = earnings
                t['raw_data']['insider']  = insider

        with self._lock:
            self.current_top5 = top5

        # 5. Analiza przez Claude AI
        results = self.analyst.analyze_batch(
            top5,
            polygon_api=self.polygon,
            uw_api=self.uw,
            db=self,
        )

        # 6. Zapisz sygnały i wyślij alerty
        for result, ticker_data in zip(results, top5):
            ticker  = result.get('ticker', '')
            verdict = result.get('verdict', 'WATCH')

            # Reset licznikow o nowym dniu
            today = now_chicago().date()
            if self._buy_count_date != today:
                self._buy_count_today      = 0
                self._buy_count_open       = 0
                self._buy_count_midday     = 0
                self._buy_count_power_hour = 0
                self._buy_count_date       = today
                self._buy_tickers_today    = set()
                logger.info(f"Power Windows reset: nowy dzien {today}")

            # Jeden BUY per ticker dziennie
            if verdict == 'BUY' and ticker in self._buy_tickers_today:
                logger.info(f"{ticker}: BUY juz wyslany dzisiaj -> WATCH")
                result['verdict'] = 'WATCH'
                verdict           = 'WATCH'

            # Limit BUY - Opcja C Power Windows
            # Poza godzinami rynkowymi = brak BUY
            if verdict == 'BUY':
                n          = now_chicago()
                confidence = result.get('confidence', '')
                is_wysoka  = (confidence == 'WYSOKA')

                if not is_market_open():
                    result['verdict'] = 'WATCH'
                    verdict           = 'WATCH'
                    logger.debug(f"{ticker}: BUY poza sesja -> WATCH")

                elif self._buy_count_today >= CONFIG.get('max_buy_signals_per_day', 15):
                    if not is_wysoka:
                        result['verdict'] = 'WATCH'
                        verdict           = 'WATCH'
                        logger.info(f"Dzienny limit BUY (15) -> {ticker} WATCH")
                    else:
                        logger.info(f"Dzienny limit BUY (15) ale WYSOKA -> {ticker} BUY override")
                        self._buy_count_today += 1

                else:
                    open_start = n.replace(hour=8,  minute=30, second=0, microsecond=0)
                    open_end   = n.replace(hour=10, minute=0,  second=0, microsecond=0)
                    midday_end = n.replace(hour=14, minute=0,  second=0, microsecond=0)
                    power_end  = n.replace(hour=15, minute=0,  second=0, microsecond=0)

                    if open_start <= n < open_end:
                        if self._buy_count_open >= CONFIG.get('max_buy_open', 7) and not is_wysoka:
                            result['verdict'] = 'WATCH'
                            verdict           = 'WATCH'
                            logger.info(f"Open limit (7) -> {ticker} WATCH")
                        else:
                            self._buy_count_open  += 1
                            self._buy_count_today += 1
                            self._buy_tickers_today.add(ticker)

                    elif open_end <= n < midday_end:
                        if self._buy_count_midday >= CONFIG.get('max_buy_midday', 5) and not is_wysoka:
                            result['verdict'] = 'WATCH'
                            verdict           = 'WATCH'
                            logger.info(f"Midday limit (5) -> {ticker} WATCH")
                        else:
                            self._buy_count_midday += 1
                            self._buy_count_today  += 1
                            self._buy_tickers_today.add(ticker)

                    elif midday_end <= n < power_end:
                        if self._buy_count_power_hour >= CONFIG.get('max_buy_power_hour', 3) and not is_wysoka:
                            result['verdict'] = 'WATCH'
                            verdict           = 'WATCH'
                            logger.info(f"Power hour limit (3) -> {ticker} WATCH")
                        else:
                            self._buy_count_power_hour += 1
                            self._buy_count_today      += 1
                            self._buy_tickers_today.add(ticker)
                    else:
                        self._buy_count_today += 1
                        self._buy_tickers_today.add(ticker)

            # WATCH escalation — 3x WATCH z rzędu = eskaluj do BUY
            # ALE tylko gdy score >= 40 (ticker musi miec realny katalizator)
            if verdict == 'WATCH':
                self._watch_count[ticker] = self._watch_count.get(ticker, 0) + 1
                if (self._watch_count[ticker] >= 3 and
                        ticker_data.get('score', 0) >= 40):
                    logger.info(f"WATCH escalation: {ticker} ({self._watch_count[ticker]}x WATCH) — eskalacja do BUY")
                    result['verdict']        = 'BUY'
                    result['confidence']     = 'SREDNIA'
                    result['justification']  = (
                        result.get('justification', '') +
                        f" [Auto-escalacja: {self._watch_count[ticker]}x WATCH z rzędu]"
                    )
                    verdict = 'BUY'
            elif verdict == 'BUY':
                self._watch_count.pop(ticker, None)  # reset po BUY
            elif verdict == 'AVOID':
                self._watch_count.pop(ticker, None)  # reset po AVOID

            signal_id = save_signal(result, ticker_data,
                                    polygon_api=self.polygon)
            # Dodaj SL/TP do result dla alertu Telegram
            if result.get('verdict') == 'BUY' and signal_id:
                try:
                    conn = get_connection()
                    c    = conn.cursor()
                    c.execute(
                        'SELECT stop_loss, take_profit, rr_ratio, '
                        'risk_pct, reward_pct, sl_basis '
                        'FROM signals WHERE id = ?',
                        (signal_id,)
                    )
                    row = c.fetchone()
                    conn.close()
                    if row:
                        result['stop_loss']   = row[0]
                        result['take_profit'] = row[1]
                        result['rr_ratio']    = row[2]
                        result['risk_pct']    = row[3]
                        result['reward_pct']  = row[4]
                        result['sl_basis']    = row[5]
                except Exception:
                    pass
            self._send_alert(result, ticker_data)

            # Alpaca Paper — otwórz pozycję przy BUY
            if result.get('verdict') == 'BUY' and self.trader.enabled:
                try:
                    trade_result = self.trader.buy(
                        ticker=result.get('ticker'),
                        entry_price=ticker_data.get('price', 0),
                        stop_loss=result.get('stop_loss'),
                        take_profit=result.get('take_profit'),
                    )
                    if trade_result == 'EXISTS':
                        price = ticker_data.get('price', 0)
                        send_message(
                            chr(9888) + ' <b>' + result.get('ticker', '') + '</b> BUY ' +
                            result.get('confidence', '') + ' - pozycja juz otwarta ' +
                            '($' + '{:.2f}'.format(price) + ')'
                        )
                except Exception as e:        
                    logger.error(f"Alpaca BUY error: {e}")        

        # 7. Zaktualizuj automatyczne wyniki
        update_outcomes(self.polygon)

        self.scan_count         += 1
        self.last_main_scan      = now_chicago()

        # Synchronizuj system_state z telegram_bot v2.0
        system_state['scan_count'] = self.scan_count
        system_state['last_scan']  = self.last_main_scan.strftime('%H:%M')
        system_state['daily_cost'] = self.analyst.daily_cost_usd
        system_state['weekly_cost'] = self.analyst.total_cost_usd

        logger.info(f"Cykl główny zakończony — "
                    f"przeanalizowano {len(results)} tickerów")

    # ==================== CYKL UW (1 min) ====================

    def _is_market_open_filter(self):
        """
        Zwraca True jeśli jesteśmy w pierwszych N minutach po otwarciu rynku.
        W tym czasie ceny small-cap są niereliable — pomijamy sygnały.
        """
        if not is_market_open():
            return False
        n = now_chicago()
        market_open = n.replace(hour=8, minute=30, second=0, microsecond=0)
        elapsed_minutes = (n - market_open).total_seconds() / 60
        filter_minutes  = CONFIG.get('market_open_filter_minutes', 15)
        return elapsed_minutes < filter_minutes

    def run_uw_scan(self):
        """
        Cykl Unusual Whales — co 1 minutę.
        Sprawdza dark pool flow i fast-track nowe tickery.
        Sprawdza triggery dla aktywnych BUY.
        """
        # Pomijaj pierwsze 15 minut po otwarciu rynku
        if self._is_market_open_filter():
            elapsed = int((now_chicago() - now_chicago().replace(
                hour=8, minute=30, second=0, microsecond=0
            )).total_seconds() / 60)
            filter_min = CONFIG.get('market_open_filter_minutes', 15)
            logger.info(f"UW scan pominięty — pierwsze {filter_min} min sesji "
                        f"({elapsed} min po otwarciu)")
            return

        # 1. Dark pool fast track
        dark_pool_flow = self.uw.get_dark_pool_flow()

        with self._lock:
            current_top = self.current_top5

        fast_track = uw_fast_track(dark_pool_flow, current_top)

        if fast_track:
            logger.info(f"UW Fast Track: {len(fast_track)} nowych tickerów")
            for ft in fast_track:
                logger.info(f"  ⚡ {ft['ticker']}: {ft['reason']}")
                # Fast track ticker trafia natychmiast do Claude
                # Pobierz dane dla tickera
                ticker_data = self.polygon.get_ticker_details(ft['ticker'])
                if ticker_data:
                    ticker_data['ticker']  = ft['ticker']
                    ticker_data['reasons'] = [ft['reason']]
                    ticker_data['score']   = 60  # UW fast track = wysoki priorytet

                    result = self.analyst.analyze(
                        ticker_data,
                        news=self.polygon.get_news(ft['ticker']),
                        options_flow=self.uw.get_options_flow(ft['ticker']),
                    )
                    save_signal(result, ticker_data)
                    self._send_alert(result, ticker_data)

        # 2. Monitorowanie aktywnych BUY — sprawdź triggery
        self._check_active_signals()

        self.last_uw_scan = now_chicago()

    # ==================== MONITORING AKTYWNYCH BUY ====================

    def _check_active_signals(self):
        """
        Sprawdza triggery re-analizy dla aktywnych sygnałów BUY.
        Wywoływane co 1 minutę przez cykl UW.
        """
        active_signals = get_active_buy_signals()

        if not active_signals:
            return

        for sig in active_signals:
            ticker = sig['ticker']

            try:
                # Cooldown — nie re-analizuj tego samego tickera
                # czesciej niz co 15 minut po BUY sygnale
                last_trigger = self.trigger_cooldown.get(ticker)
                if last_trigger:
                    elapsed = (now_chicago() - last_trigger).total_seconds()
                    if elapsed < 900:  # 15 minut
                        continue

                # Dodatkowy cooldown — nie re-analizuj w ciagu 15 min od BUY sygnalu
                from database import get_connection
                try:
                    conn = get_connection()
                    c    = conn.cursor()
                    c.execute(
                        'SELECT timestamp FROM signals WHERE ticker=? '
                        'AND verdict="BUY" ORDER BY id DESC LIMIT 1',
                        (ticker,)
                    )
                    row = c.fetchone()
                    conn.close()
                    if row:
                        from datetime import datetime
                        buy_time = datetime.fromisoformat(row[0])
                        if buy_time.tzinfo is None:
                            buy_time = buy_time.replace(tzinfo=now_chicago().tzinfo)
                        else:
                            buy_time = buy_time.astimezone(now_chicago().tzinfo)
                        since_buy = (now_chicago() - buy_time).total_seconds()
                        if since_buy < 900:  # 15 min po BUY
                            continue
                except Exception:
                    pass

                # Pobierz dane UW dla tickera (główne źródło monitoringu)
                options_flow   = self.uw.get_options_flow(ticker)
                dark_pool_flow = self.uw.get_dark_pool_flow()
                dp_for_ticker  = next(
                    (dp for dp in dark_pool_flow
                     if dp.get('ticker') == ticker), {}
                )

                # Pobierz cenę z Polygon (z fallback na Alpaca)
                if self.alpaca:
                    current_data = get_ticker_with_fallback(
                        ticker, self.polygon, self.alpaca
                    )
                else:
                    current_data = self.polygon.get_ticker_details(ticker)

                # Złącz dane UW z ceną
                uw_data = {
                    **(options_flow or {}),
                    'price':          current_data.get('price', sig['price']),
                    'dark_pool_side': dp_for_ticker.get('side', ''),
                    'dark_pool_size': dp_for_ticker.get('size_usd', 0),
                }

                # Sprawdź triggery oparte na UW
                trigger, details = check_retrigger_conditions(
                    sig, uw_data
                )

                if not trigger:
                    continue

                logger.info(f"Trigger [{trigger}] dla {ticker}: {details}")
                self.trigger_cooldown[ticker] = now_chicago()

                current_price = uw_data.get('price', sig['price'])

                # Take profit — alert bez re-analizy Claude'a
                if trigger == 'TAKE_PROFIT':
                    gain = ((current_price - sig['price'])
                            / sig['price'] * 100)
                    alert_take_profit(ticker, sig['price'],
                                      current_price, gain)
                    save_retrigger(sig['id'], ticker, trigger,
                                   details, 'BUY', None)
                    close_signal(sig['id'], f"TAKE_PROFIT +{gain:.1f}%")
                    continue

                # Trigger UW — re-analiza przez Claude
                ticker_data = {
                    'ticker':       ticker,
                    'price':        current_price,
                    'change_pct':   current_data.get('change_pct', 0),
                    'volume':       current_data.get('volume', 0),
                    'volume_ratio': current_data.get('volume_ratio', 1.0),
                    'score':        40,
                    'reasons':      [f"RE-ANALIZA UW: {details}"],
                }

                new_result = self.analyst.analyze(
                    ticker_data,
                    news=self.polygon.get_news(ticker),
                    options_flow=self.uw.get_options_flow(ticker),
                    signal_history=get_signal_history(ticker),
                )

                new_verdict = new_result.get('verdict', 'WATCH')

                # Zapisz trigger i wyślij alert
                save_retrigger(sig['id'], ticker, trigger, details,
                               'BUY', new_verdict)

                alert_retrigger(
                    ticker=ticker,
                    trigger=trigger,
                    details=details,
                    old_verdict='BUY',
                    new_verdict=new_verdict,
                    current_price=current_price,
                    entry_price=sig['price'],
                )

                # Zamknij monitorowanie jeśli zmienił się na AVOID
                if new_verdict == 'AVOID':
                    close_signal(sig['id'], f"RE-ANALIZA → {new_verdict}")
                    # Alpaca Paper — zamknij pozycję
                    if self.trader.enabled:
                        try:
                            self.trader.sell(ticker, reason='BUY_TO_AVOID')
                        except Exception as e:
                            logger.error(f"Paper trader SELL error: {e}")

                # Zamknij przy STOP_LOSS
                if trigger == 'STOP_LOSS':
                    close_signal(sig['id'], 'STOP_LOSS')
                    if self.trader.enabled:
                        try:
                            self.trader.sell(ticker, reason='STOP_LOSS')
                        except Exception as e:
                            logger.error(f"Paper trader SELL SL error: {e}")

            except Exception as e:
                logger.error(f"Błąd monitorowania {ticker}: {e}")

    # ==================== ALERTY ====================

    def _send_alert(self, result, ticker_data):
        """
        Wysyła alert z cooldown — zapobiega duplikatom.
        """
        ticker  = result.get('ticker', '')
        verdict = result.get('verdict', '')
        key     = f"{ticker}_{verdict}"

        # Sprawdź cooldown
        last_alert = self.alert_cooldown.get(key)
        if last_alert:
            elapsed = (now_chicago() - last_alert).total_seconds()
            if elapsed < CONFIG['duplicate_alert_cooldown']:
                logger.info(f"Alert cooldown: {ticker} {verdict} "
                            f"({int(elapsed)}s temu)")
                return

        sent = alert_signal(result, ticker_data)
        if sent:
            self.alert_cooldown[key] = now_chicago()
            self.alert_count += 1

    # ==================== DASHBOARD ====================

    def run_dashboard(self):
        """Wysyła godzinne podsumowanie na Telegram"""
        stats          = get_stats()
        active_signals = get_active_buy_signals()

        # Top sygnały z ostatniej godziny
        top_today = []
        from database import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                SELECT ticker, verdict, confidence, price
                FROM signals
                WHERE timestamp > datetime('now', '-1 hour')
                AND verdict IN ('BUY', 'WATCH')
                ORDER BY timestamp DESC
                LIMIT 5
            ''')
            top_today = [dict(row) for row in c.fetchall()]
        finally:
            conn.close()

        send_hourly_dashboard(stats, active_signals, top_today)
        self.last_dashboard = now_chicago()
        logger.info("Dashboard wysłany")

    # ==================== INTERFEJS DLA BAZY ====================

    def get_signal_history(self, ticker, limit=5):
        """
        Wrapper dla claude_analyst.analyze_batch() —
        dostarcza historię sygnałów z bazy.
        """
        return get_signal_history(ticker, limit)

    # ==================== EXTENDED HOURS ====================

    def run_premarket_watchlist(self):
        """
        Pre-market watchlist 8:00 CST.
        Top 10 gainerow premarket - wysyla na Telegram przed otwarciem.
        Trader wie co obserwowac PRZED dzwonkiem.
        """
        logger.info("=== PRE-MARKET WATCHLIST ===")

        universe = self.polygon.get_universe(
            min_volume=CONFIG.get('min_volume_extended', 10_000)
        )
        if not universe:
            return

        self.float_cache.enrich_universe(universe)

        candidates = [
            t for t in universe
            if (0.10 <= t.get('price', 0) <= 15.0
                and t.get('change_pct', 0) > 0
                and t.get('volume', 0) >= 50_000)
        ]
        if not candidates:
            logger.info("Pre-market watchlist: brak kandydatow")
            return

        candidates.sort(key=lambda x: abs(x.get('gap_pct', 0)), reverse=True)
        top10 = candidates[:10]

        time_str = now_chicago().strftime('%H:%M CST')
        dash = chr(8212)
        lines_msg = [
            chr(128270) + chr(128293) + ' <b>PRE-MARKET WATCHLIST ' + dash + ' ' + time_str + '</b>',
            'Top gainerzy przed otwarciem. Rynek otwiera sie o 8:30 CST.',
            '',
        ]

        for idx, t in enumerate(top10, 1):
            ticker   = t.get('ticker', '')
            price    = t.get('price', 0)
            change   = t.get('change_pct', 0)
            gap      = t.get('gap_pct', 0)
            volume   = t.get('volume', 0)
            float_sh = t.get('float_shares')

            float_str = ''
            if float_sh:
                if float_sh < 1_000_000:
                    float_str = ' | Float ' + '{:.0f}'.format(float_sh/1000) + 'k'
                elif float_sh < 10_000_000:
                    float_str = ' | Float ' + '{:.1f}'.format(float_sh/1_000_000) + 'M'

            gap_str = ' | Gap ' + '{:+.0f}'.format(gap) + '%' if gap else ''

            if float_sh and float_sh < 5_000_000 and abs(gap) > 20:
                quality = ' ' + chr(128293) + chr(128293)
            elif abs(gap) > 50 or (float_sh and float_sh < 5_000_000):
                quality = ' ' + chr(128293)
            else:
                quality = ''

            lines_msg.append(
                str(idx) + '. <b>' + ticker + '</b>' + quality +
                ' $' + '{:.2f}'.format(price) +
                ' (' + '{:+.1f}'.format(change) + '%)' +
                gap_str + float_str +
                ' | vol ' + '{:.0f}'.format(volume/1000) + 'k'
            )

        lines_msg += ['', chr(9201) + ' Rynek otwiera sie za ~30 min. Przygotuj sie!']
        from telegram_alerts import send_message
        send_message(chr(10).join(lines_msg))
        logger.info(f"Pre-market watchlist wyslany: {len(top10)} tickerow")

    # ==================== EXTENDED HOURS ====================

    def run_premarket_scan(self):
        """
        Pre-market scan 8:00-8:30 CST.
        News-first — szuka katalizatorow przed otwarciem.
        Uzywa nizszego progu volume i market-wide news scan.
        """
        logger.info("=== PRE-MARKET SCAN ===")

        # Pobierz universe — pre-market uzywa danych z poprzedniej sesji
        # volume z poprzedniego dnia (nie dzisiejsze pre-market)
        universe = self.polygon.get_universe(
            min_volume=CONFIG.get('min_volume_extended', 10_000)
        )

        if not universe:
            # Fallback — pobierz ze snapshotu bez filtru volume
            logger.info("Pre-market: brak danych volume, probuje bez filtru")
            universe = self.polygon.get_universe(min_volume=0)

        if not universe:
            logger.info("Pre-market: brak danych z Polygon")
            return

        logger.info(f"Pre-market: {len(universe)} tickerow w universe")

        # News cache — market-wide scan ostatnie 4h (szersze okno pre-market)
        news_cache = {}
        try:
            news_cache = self.polygon.get_recent_news_tickers(hours=4, limit=100)
        except Exception as e:
            logger.warning(f"Pre-market news scan error: {e}")
            for t in sorted(filtered, key=lambda x: x.get('volume', 0), reverse=True)[:50]:
                try:
                    news = self.polygon.get_news(t['ticker'], limit=3)
                    if news:
                        news_cache[t['ticker']] = news
                except Exception:
                    pass

        # UW unusual flow
        uw_flow_cache = {}
        try:
            unusual = self.uw.get_tickers_with_unusual_flow(
                min_premium=20_000, limit=30
            )
            for t in unusual:
                ticker = t['ticker']
                uw_flow_cache[ticker] = {
                    'call_volume':    t.get('call_premium', 0),
                    'put_volume':     t.get('put_premium', 0),
                    'call_put_ratio': round(t.get('call_premium', 0) / max(t.get('put_premium', 1), 1), 2),
                    'unusual':        True,
                    'sentiment':      'bullish' if t.get('bullish') else 'bearish',
                }
            logger.info(f"Pre-market: {len(uw_flow_cache)} tickerow z UW flow")
        except Exception as e:
            logger.warning(f"Pre-market UW flow error: {e}")

        # Dark pool
        dark_pool = self.uw.get_dark_pool_flow()

        # Finnhub earnings
        finnhub_cache = {}
        for t in filtered:
            ticker = t['ticker']
            earnings = self.fh.get_earnings_calendar(ticker)
            insider  = self.fh.get_insider_transactions(ticker)
            if earnings or insider:
                finnhub_cache[ticker] = {'earnings': earnings, 'insider': insider}

        # Pre-filter — bez limitu cenowego dla pre-market
        # ticker z newsem lub UW flow moze byc ponizej $1
        ranked = []
        from pre_filter import rank_tickers
        ranked = rank_tickers(
            filtered,
            dark_pool_flow=dark_pool,
            uw_flow_cache=uw_flow_cache,
            news_cache=news_cache,
            finnhub_cache=finnhub_cache,
        )

        # Filtruj tylko tickery z jakims katalizatorem (score > 10)
        min_score = 10
        candidates = [t for t in ranked if t['score'] >= min_score]

        if not candidates:
            logger.info("Pre-market: brak tickerow z katalizatorem")
            return

        top3 = candidates[:3]
        logger.info(f"Pre-market TOP {len(top3)} do analizy:")
        for t in top3:
            logger.info(f"  {t['ticker']:6s} | score: {t['score']:3d} | "
                        f"${t['price']:.2f} | {' | '.join(t['reasons'][:2])}")

        # Analiza Claude
        results = self.analyst.analyze_batch(
            top3,
            polygon_api=self.polygon,
            uw_api=self.uw,
            db=self,
        )

        for result, ticker_data in zip(results, top3):
            signal_id = save_signal(result, ticker_data, polygon_api=self.polygon)
            if result.get('verdict') == 'BUY' and signal_id:
                try:
                    from database import get_connection
                    conn = get_connection()
                    c    = conn.cursor()
                    c.execute(
                        'SELECT stop_loss, take_profit, rr_ratio, risk_pct, reward_pct, sl_basis '
                        'FROM signals WHERE id = ?', (signal_id,)
                    )
                    row = c.fetchone()
                    conn.close()
                    if row:
                        result['stop_loss']   = row[0]
                        result['take_profit'] = row[1]
                        result['rr_ratio']    = row[2]
                        result['risk_pct']    = row[3]
                        result['reward_pct']  = row[4]
                        result['sl_basis']    = row[5]
                except Exception:
                    pass
            self._send_alert(result, ticker_data)

            if result.get('verdict') == 'BUY' and self.trader.enabled:
                try:
                    self.trader.buy(
                        ticker=result.get('ticker'),
                        entry_price=ticker_data.get('price', 0),
                        stop_loss=result.get('stop_loss'),
                        take_profit=result.get('take_profit'),
                    )
                except Exception as e:
                    logger.error(f"Pre-market Paper trader BUY error: {e}")

        self.last_extended_scan = now_chicago()
        self.scan_count        += 1
        logger.info(f"Pre-market: przeanalizowano {len(results)} tickerow")

        # Pre-session Watchlist o 8:20 CST
        n = now_chicago()
        watchlist_window = n.replace(hour=8, minute=20, second=0, microsecond=0)
        watchlist_end    = n.replace(hour=8, minute=30, second=0, microsecond=0)
        if watchlist_window <= n < watchlist_end and not getattr(self, '_watchlist_sent_today', False):
            send_presession_watchlist(top3)
            self._watchlist_sent_today = True
            logger.info("Pre-session watchlist wyslany")

    def run_afterhours_catalyst_scan(self):
        """
        After-hours catalyst scan 15:00-20:00 CST.
        Szuka Catalyst HIGH/MEDIUM po zamknieciu rynku.
        NIE wydaje BUY — informuje o potencjalnym setupie na jutro.
        """
        logger.info("=== AFTER-HOURS CATALYST SCAN ===")

        from pre_filter import CATALYST_HIGH, CATALYST_MEDIUM
        from datetime import datetime, timezone, timedelta

        # Market-wide news z ostatnich 2h
        try:
            news_cache = self.polygon.get_recent_news_tickers(hours=2, limit=100)
        except Exception as e:
            logger.warning(f"AH news scan error: {e}")
            return

        if not news_cache:
            logger.info("After-hours: brak swiezych newsow")
            return

        now_utc   = datetime.now(timezone.utc)
        found     = 0
        alerted   = getattr(self, '_ah_alerted_today', set())

        for ticker, articles in news_cache.items():
            if ticker in alerted:
                continue

            for article in articles:
                title    = (article.get('title', '') or '').lower()
                desc     = (article.get('description', '') or '').lower()
                pub_utc  = article.get('published_utc', '')
                publisher = article.get('publisher', {})
                source   = (publisher.get('name', '') or '') if isinstance(publisher, dict) else ''

                # Wiek newsa
                age_h = 999
                if pub_utc:
                    try:
                        pub_dt = datetime.fromisoformat(pub_utc.replace('Z', '+00:00'))
                        age_h  = (now_utc - pub_dt).total_seconds() / 3600
                    except Exception:
                        pass

                if age_h > 2:
                    continue

                # Sprawdz catalyst quality
                catalyst = None
                if any(w in title or w in desc for w in CATALYST_HIGH):
                    catalyst = 'HIGH'
                elif any(w in title or w in desc for w in CATALYST_MEDIUM):
                    catalyst = 'MEDIUM'

                if not catalyst:
                    continue

                # Pobierz cene
                try:
                    td = self.polygon.get_ticker_details(ticker)
                    price = td.get('price', 0) or td.get('prev_close', 0)
                except Exception:
                    price = 0

                if price <= 0 or price > CONFIG['max_price']:
                    continue

                logger.info(f"AH Catalyst {catalyst}: {ticker} | {article.get('title', '')[:60]}")

                send_afterhours_catalyst(
                    ticker       = ticker,
                    catalyst_type = catalyst,
                    title        = article.get('title', ''),
                    source       = source,
                    age_h        = age_h,
                    price        = price,
                )

                alerted.add(ticker)
                found += 1
                break  # jeden alert per ticker

        self._ah_alerted_today = alerted
        if found:
            logger.info(f"After-hours: wyslano {found} alertow katalitycznych")
        else:
            logger.info("After-hours: brak nowych katalizatorow")

        # Reset flagi o polnocy
        n = now_chicago()
        if n.hour == 0:
            self._ah_alerted_today = set()

    def run_extended_scan(self):
        """
        Skan pre-market lub after-market — co 15 minut.
        Claude analizuje tylko tickery z katalizatorem (earnings, FDA, insider).
        Wolumen minimalny: 10,000 (zamiast 100,000 podczas sesji).
        """
        status = get_market_status()
        logger.info(f"=== CYKL EXTENDED HOURS ({status}) ===")

        # Pobierz universe z niższym progiem wolumenu
        universe = self.polygon.get_universe()
        if not universe:
            logger.info("Extended hours: brak danych z Polygon")
            return

        # Filtruj po niższym wolumenie dla extended hours
        min_vol  = CONFIG['min_volume_extended']
        filtered = [t for t in universe
                    if t.get('volume', 0) >= min_vol]

        if not filtered:
            logger.info(f"Extended hours: brak tickerów z vol >= {min_vol:,}")
            return

        # Pobierz dane Finnhub
        finnhub_cache = {}
        for t in filtered:
            ticker       = t['ticker']
            t['earnings'] = self.fh.get_earnings_calendar(ticker)
            t['insider']  = self.fh.get_insider_transactions(ticker)
            if t['earnings'] or t['insider']:
                finnhub_cache[ticker] = {
                    'earnings': t['earnings'],
                    'insider':  t['insider'],
                }

        # Tylko tickery z katalizatorem (jeśli tryb oszczędny)
        if not CONFIG['claude_extended_hours_all']:
            with_catalyst = [t for t in filtered
                             if t.get('earnings') or t.get('insider')]
            logger.info(f"Extended hours: {len(filtered)} tickerów → "
                        f"{len(with_catalyst)} z katalizatorem")
            if not with_catalyst:
                logger.info("Extended hours: brak tickerów z katalizatorem")
                return
            filtered = with_catalyst

        # Pre-filter → TOP 5
        dark_pool = self.uw.get_dark_pool_flow()
        top5 = get_top_tickers(
            filtered,
            dark_pool_flow=dark_pool,
            finnhub_cache=finnhub_cache,
            top_n=CONFIG['max_tickers_for_claude'],
        )

        if not top5:
            return

        # Analiza Claude
        results = self.analyst.analyze_batch(
            top5,
            polygon_api=self.polygon,
            uw_api=self.uw,
            db=self,
        )

        for result, ticker_data in zip(results, top5):
            save_signal(result, ticker_data)
            self._send_alert(result, ticker_data)

        self.last_extended_scan = now_chicago()
        self.scan_count        += 1
        logger.info(f"Extended hours: przeanalizowano {len(results)} tickerów")

    # ==================== MANUALNA ANALIZA ====================

    def queue_manual_analysis(self, ticker):
        """
        Dodaje ticker do kolejki manualnej analizy.
        Wywoływane przez Telegram bot po komendzie /analyze TICKER.
        """
        ticker = ticker.upper().strip()
        with manual_queue_lock:
            if ticker not in manual_queue:
                manual_queue.append(ticker)
                logger.info(f"Manual queue: dodano {ticker}")
                return True
        return False

    def run_manual_analysis(self):
        """
        Przetwarza kolejkę manualnych analiz.
        Wywołuje Claude dla każdego tickera z kolejki.
        """
        with manual_queue_lock:
            queue = manual_queue.copy()
            manual_queue.clear()

        if not queue:
            return

        budget = CONFIG.get('manual_analysis_daily_usd',
                             CLAUDE_CONFIG.get('daily_budget_usd', 2.27))
        if self.manual_cost_usd >= budget:
            logger.warning(f"Manual analysis: dzienny limit ${budget:.2f} przekroczony")
            from telegram_alerts import send_message
            send_message(f"⚠️ Dzienny limit manualnych analiz ${budget:.2f} przekroczony.")
            return

        for ticker in queue:
            try:
                logger.info(f"Manualna analiza: {ticker}")

                # Pobierz dane
                ticker_data = self.polygon.get_ticker_details(ticker)
                if not ticker_data or ticker_data.get('price', 0) <= 0:
                    if self.alpaca:
                        ticker_data = self.alpaca.get_ticker_details(ticker)

                if not ticker_data:
                    logger.warning(f"Manualna analiza: brak danych dla {ticker}")
                    continue

                ticker_data['ticker']  = ticker
                ticker_data['score']   = 50
                ticker_data['reasons'] = ['Manualna analiza (żądanie użytkownika)']
                ticker_data['earnings'] = self.fh.get_earnings_calendar(ticker)
                ticker_data['insider']  = self.fh.get_insider_transactions(ticker)

                result = self.analyst.analyze(
                    ticker_data,
                    news=self.polygon.get_news(ticker),
                    options_flow=self.uw.get_options_flow(ticker),
                    signal_history=get_signal_history(ticker),
                )

                # Śledź koszt manualnych analiz osobno
                cost = CONFIG.get('cost_per_call_usd', 0.0028)
                self.manual_cost_usd += cost

                save_signal(result, ticker_data)
                # Manualna analiza — omijaj cooldown, wysylaj zawsze
                alert_manual(result, ticker_data)
                self.alert_count += 1

                logger.info(f"Manualna analiza {ticker}: "
                            f"{result['verdict']} ({result['confidence']})")

            except Exception as e:
                logger.error(f"Błąd manualnej analizy {ticker}: {e}")

    # ==================== GŁÓWNA PĘTLA ====================

    def run(self):
        """Uruchamia system — główna pętla"""
        self.running = True

        print(f"""
{'='*55}
  {' STOCK SCANNER v1.0 ':=^53}
  Tryb:    {'DEMO (MockPolygon)' if self.demo_mode else 'LIVE'}
  Rynek:   {get_market_status()}
  Czas:    {now_chicago().strftime('%Y-%m-%d %H:%M:%S')} CST
  Cykl:    co {CONFIG['main_scan_interval']//60} min (UW: co {CONFIG['uw_scan_interval']}s)
  TOP:     {CONFIG['max_tickers_for_claude']} tickerów → Claude AI
{'='*55}
""")

        # Inicjalizacja bazy
        init_db()

        # Startup alert
        send_startup_message(demo_mode=self.demo_mode)

        # Uruchom Telegram bot v2.0
        start_bot_thread()

        logger.info("System uruchomiony — Ctrl+C aby zatrzymać")

        # Pierwsze uruchomienie natychmiast
        try:
            self.run_main_scan()
        except Exception as e:
            logger.error(f"Błąd pierwszego cyklu: {e}")

        # Główna pętla
        while self.running:
            try:
                now = now_chicago()

                # Sprawdź czy system jest zapauzowany
                with system_paused_lock:
                    paused = system_paused

                if paused:
                    time.sleep(30)
                    continue

                # Cykl UW (co 1 minutę)
                uw_due = (self.last_uw_scan is None or
                          (now - self.last_uw_scan).total_seconds()
                          >= CONFIG['uw_scan_interval'])

                if uw_due:
                    try:
                        self.run_uw_scan()
                    except Exception as e:
                        logger.error(f"Błąd cyklu UW: {e}")

                # Reset flagi watchlist o polnocy
                if now_chicago().hour == 0:
                    self._watchlist_sent_today = False

                # Cykl glowny (co 5 minut) — tylko dni handlowe
                is_weekend = now_chicago().weekday() >= 5
                main_due = (
                    not is_weekend and
                    is_market_open() and
                    (self.last_main_scan is None or
                     (now - self.last_main_scan).total_seconds()
                     >= CONFIG['main_scan_interval'])
                )

                if is_weekend:
                    pass  # weekend — brak skanow, brak kosztow Claude
                elif main_due:
                    try:
                        self.run_main_scan()
                    except Exception as e:
                        logger.error(f"Błąd cyklu głównego: {e}")

                # Extended hours (pre/after-market)
                in_extended = (CONFIG['premarket_enabled'] and is_premarket()) or \
                               (CONFIG['aftermarket_enabled'] and is_aftermarket())

                if in_extended:
                    ext_interval = (CONFIG['premarket_scan_interval']
                                    if is_premarket()
                                    else CONFIG['aftermarket_scan_interval'])
                    ext_due = (self.last_extended_scan is None or
                               (now - self.last_extended_scan).total_seconds()
                               >= ext_interval)
                    if ext_due:
                        try:
                            # Pre-market 8:00-8:30 CST
                            if is_premarket():
                                n = now_chicago()
                                premarket_open = n.replace(hour=8, minute=0, second=0, microsecond=0)
                                if n >= premarket_open:
                                    # Watchlist o 8:00-8:10 CST
                                    watchlist_end = n.replace(hour=8, minute=10, second=0, microsecond=0)
                                    if n < watchlist_end and not getattr(self, '_watchlist_sent_today', False):
                                        self.run_premarket_watchlist()
                                        self._watchlist_sent_today = True
                                    self.run_premarket_scan()
                                else:
                                    self.run_extended_scan()
                            # After-hours 15:00-20:00 CST
                            elif is_aftermarket():
                                n = now_chicago()
                                ah_end = n.replace(hour=20, minute=0, second=0, microsecond=0)
                                if n < ah_end:
                                    self.run_afterhours_catalyst_scan()
                                else:
                                    self.run_extended_scan()
                            else:
                                self.run_extended_scan()
                        except Exception as e:
                            logger.error(f"Blad extended hours: {e}")

                # Manualna analiza z kolejki
                if manual_queue:
                    try:
                        self.run_manual_analysis()
                    except Exception as e:
                        logger.error(f"Błąd manualnej analizy: {e}")

                # Dashboard (co godzinę)
                dashboard_due = (self.last_dashboard is None or
                                 (now - self.last_dashboard).total_seconds()
                                 >= 3600)

                if dashboard_due:
                    try:
                        self.run_dashboard()
                    except Exception as e:
                        logger.error(f"Błąd dashboardu: {e}")

                # Status w konsoli co 1 minutę
                self._print_status()

                # Śpij 30 sekund
                time.sleep(30)

            except Exception as e:
                logger.error(f"Błąd głównej pętli: {e}")
                time.sleep(30)

    def _print_status(self):
        """Drukuje krótki status do konsoli"""
        stats = get_stats()
        now   = now_chicago()
        print(f"\r⏱ {now.strftime('%H:%M:%S')} CST | "
              f"Rynek: {get_market_status()} | "
              f"Skanów: {self.scan_count} | "
              f"Sygnałów: {stats.get('total_signals', 0)} | "
              f"Alertów: {self.alert_count} | "
              f"Monit.: {stats.get('active_monitoring', 0)}",
              end='', flush=True)

    # ==================== SHUTDOWN ====================

    def _shutdown_handler(self, sig, frame):
        """Obsługuje Ctrl+C i SIGTERM"""
        print("\n")
        logger.info("Sygnał zatrzymania otrzymany...")
        self.shutdown()

    def shutdown(self):
        """Czyste zatrzymanie systemu"""
        self.running = False
        print("\n" + "="*55)
        print("  ZATRZYMYWANIE SYSTEMU...")
        print("="*55)

        # Telegram bot v2.0 zatrzymuje się automatycznie (daemon thread)



        stats = get_stats()
        send_shutdown_message(stats)

        analyst_stats = self.analyst.get_stats()
        print(f"\n📊 Podsumowanie sesji:")
        print(f"  Skanów:      {self.scan_count}")
        print(f"  Alertów:     {self.alert_count}")
        print(f"  API calls:   {analyst_stats['total_calls']}")
        print(f"  Koszt API:   ${analyst_stats['total_cost_usd']:.4f}")
        print(f"  Sygnałów DB: {stats.get('total_signals', 0)}")
        print("\n✅ System zatrzymany")
        sys.exit(0)


# ==================== URUCHOMIENIE ====================

if __name__ == "__main__":
    scanner = StockScanner()
    scanner.run()
