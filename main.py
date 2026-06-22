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

from config import (logger, CONFIG, DEMO_MODE, now_chicago,
                    is_market_open, is_premarket, is_aftermarket,
                    get_market_status, get_min_volume, CHICAGO_TZ)
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
from telegram_alerts import (alert_signal, alert_retrigger, alert_take_profit,
                              send_hourly_dashboard, send_startup_message,
                              send_shutdown_message)
from telegram_bot import start_bot_thread, system_state, manual_queue, manual_queue_lock


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

        # Kolejka manualnych analiz (z Telegram /analyze TICKER)
        self.manual_queue       = []
        self.manual_cost_usd    = 0.0
        self._manual_lock       = threading.Lock()

        # Telegram bot (nasłuchuje komend) — v2.0 używa start_bot_thread
        self.bot = None  # inicjowany w run()

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

        # 3. Pobierz dane Finnhub dla universe
        finnhub_cache = {}
        for t in universe:
            ticker = t['ticker']
            earnings = self.fh.get_earnings_calendar(ticker)
            insider  = self.fh.get_insider_transactions(ticker)
            t['earnings'] = earnings
            t['insider']  = insider
            if earnings or insider:
                finnhub_cache[ticker] = {
                    'earnings': earnings,
                    'insider':  insider,
                }

        # 4. Pre-filter → TOP 5
        top5 = get_top_tickers(
            universe,
            dark_pool_flow=dark_pool_flow,
            finnhub_cache=finnhub_cache,
            top_n=CONFIG['max_tickers_for_claude'],
        )

        if not top5:
            logger.warning("Pre-filter: brak tickerów — pomijam analizę")
            return

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
            signal_id = save_signal(result, ticker_data)
            self._send_alert(result, ticker_data)

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
                # częściej niż co 5 minut
                last_trigger = self.trigger_cooldown.get(ticker)
                if last_trigger:
                    elapsed = (now_chicago() - last_trigger).total_seconds()
                    if elapsed < 300:
                        continue

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

        budget = CONFIG['manual_analysis_budget_usd'] / 22
        if self.manual_cost_usd >= budget:
            logger.warning(f"Manual analysis: dzienny limit ${budget:.2f} "
                           f"przekroczony")
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
                self._send_alert(result, ticker_data)

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

        # Uruchom Telegram bot v2.0 w osobnym wątku
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

                # Cykl UW (co 1 minutę)
                uw_due = (self.last_uw_scan is None or
                          (now - self.last_uw_scan).total_seconds()
                          >= CONFIG['uw_scan_interval'])

                if uw_due:
                    try:
                        self.run_uw_scan()
                    except Exception as e:
                        logger.error(f"Błąd cyklu UW: {e}")

                # Cykl główny (co 5 minut)
                main_due = (self.last_main_scan is None or
                            (now - self.last_main_scan).total_seconds()
                            >= CONFIG['main_scan_interval'])

                if main_due:
                    try:
                        self.run_main_scan()
                    except Exception as e:
                        logger.error(f"Błąd cyklu głównego: {e}")

                # Extended hours (pre/after-market) — co 15 minut
                in_extended = (CONFIG['premarket_enabled'] and is_premarket()) or                               (CONFIG['aftermarket_enabled'] and is_aftermarket())

                if in_extended:
                    ext_interval = (CONFIG['premarket_scan_interval']
                                    if is_premarket()
                                    else CONFIG['aftermarket_scan_interval'])
                    ext_due = (self.last_extended_scan is None or
                               (now - self.last_extended_scan).total_seconds()
                               >= ext_interval)
                    if ext_due:
                        try:
                            self.run_extended_scan()
                        except Exception as e:
                            logger.error(f"Błąd extended hours: {e}")

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
