#!/usr/bin/env python3
"""
STOCK SCANNER - MOMENTUM SCAN (Tryb 2)
Zapisz jako momentum_scan.py w folderze stock-scanner

Zadanie:
  Prosty momentum scanner bez Claude AI.
  Wykrywa volume spikes + gap + float runners.
  Zero kosztow Claude.

  Sygnaly zapisywane do bazy (verdict='MOMENTUM') do przyszlej
  analizy self-learning. Telegram alerty wylaczone - tylko DB.

Triggery (wystarczy jeden, wszystkie wymagaja change > 0):
  A) RVOL > 5x + zmiana > 8%            (momentum)
  B) Float < 5M + RVOL > 50x            (low float runner)
  C) Gap > 15% + RVOL > 3x              (gap play)
  D) RVOL > 100x                         (ultra spike)

Historia zmian:
  v1.0 - pierwsza wersja
  v1.1 - nowy format entry/stop/target, timing
  v1.2 - silniejsze progi, change>0 wymagane
  v1.3 - Telegram wylaczony, zapis do DB dla self-learning
"""

from config import logger, CONFIG, now_chicago

MOMENTUM_CONFIG = {
    'trigger_a_rvol':    5.0,
    'trigger_a_change':  8.0,
    'trigger_b_float':   5_000_000,
    'trigger_b_rvol':    50.0,
    'trigger_c_gap':     15.0,
    'trigger_c_rvol':    3.0,
    'trigger_d_rvol':    100.0,
    'min_price':         0.50,
    'max_price':         20.0,
    'min_volume':        100_000,
    'cooldown_min':      15,
    'max_alerts_per_cycle': 3,
}

EXCLUDED_SUFFIXES = ('W', 'WS', 'WW', 'R', 'RT', 'U')


class MomentumScanner:

    def __init__(self, polygon_api, telegram_send_fn, save_signal_fn=None):
        self.polygon        = polygon_api
        self.send            = telegram_send_fn
        self.save_signal_fn  = save_signal_fn  # callback do zapisu w DB
        self._alerted        = {}
        self._daily_alerts   = set()
        self._alert_date     = None
        logger.info("MomentumScanner zainicjowany (DB-only, Telegram wylaczony)")

    def _reset_daily(self):
        today = now_chicago().date()
        if self._alert_date != today:
            self._daily_alerts = set()
            self._alert_date   = today

    def _is_excluded(self, ticker):
        t = ticker.upper()
        for suffix in EXCLUDED_SUFFIXES:
            if t.endswith(suffix) and len(t) > len(suffix):
                return True
        if '.' in t:
            return True
        return False

    def _in_cooldown(self, ticker):
        last = self._alerted.get(ticker)
        if not last:
            return False
        elapsed = (now_chicago() - last).total_seconds() / 60
        return elapsed < MOMENTUM_CONFIG['cooldown_min']

    def _get_trigger(self, ticker_data):
        ratio    = ticker_data.get('volume_ratio', 0)
        change   = ticker_data.get('change_pct', 0)
        gap      = ticker_data.get('gap_pct', 0)
        float_sh = ticker_data.get('float_shares') or 0
        volume   = ticker_data.get('volume', 0)
        price    = ticker_data.get('price', 0)
        cfg      = MOMENTUM_CONFIG

        if not (cfg['min_price'] <= price <= cfg['max_price']):
            return None, None
        if volume < cfg['min_volume']:
            return None, None

        if ratio >= cfg['trigger_d_rvol'] and change > 0:
            return 'ULTRA_SPIKE', 'Vol ' + '{:.0f}'.format(ratio) + 'x ekstremalny'

        if (float_sh > 0 and float_sh <= cfg['trigger_b_float']
                and ratio >= cfg['trigger_b_rvol'] and change > 0):
            float_m = float_sh / 1_000_000
            return 'LOW_FLOAT', 'Float ' + '{:.1f}'.format(float_m) + 'M + vol ' + '{:.0f}'.format(ratio) + 'x'

        if gap >= cfg['trigger_c_gap'] and ratio >= cfg['trigger_c_rvol'] and change > 0:
            return 'GAP_PLAY', 'Gap ' + '{:+.0f}'.format(gap) + '% + vol ' + '{:.1f}'.format(ratio) + 'x'

        if ratio >= cfg['trigger_a_rvol'] and change >= cfg['trigger_a_change']:
            return 'MOMENTUM', 'Vol ' + '{:.1f}'.format(ratio) + 'x + zmiana ' + '{:+.1f}'.format(change) + '%'

        return None, None

    def scan(self, universe):
        """
        Glowna funkcja skanowania.
        Zapisuje trafienia do bazy (jesli save_signal_fn podany).
        Telegram alerty WYLACZONE.
        """
        self._reset_daily()

        if not universe:
            return 0

        triggered = []

        for ticker_data in universe:
            ticker = ticker_data.get('ticker', '')

            if self._is_excluded(ticker):
                continue
            if self._in_cooldown(ticker):
                continue

            trigger_name, trigger_desc = self._get_trigger(ticker_data)
            if not trigger_name:
                continue

            triggered.append((ticker_data, trigger_name, trigger_desc))

        priority = {'ULTRA_SPIKE': 4, 'LOW_FLOAT': 3, 'GAP_PLAY': 2, 'MOMENTUM': 1}
        triggered.sort(
            key=lambda x: (priority.get(x[1], 0), x[0].get('volume_ratio', 0)),
            reverse=True
        )

        alerts_sent = 0
        max_alerts  = MOMENTUM_CONFIG['max_alerts_per_cycle']

        for ticker_data, trigger_name, trigger_desc in triggered[:max_alerts]:
            ticker = ticker_data.get('ticker', '')

            # Zapisz do bazy (do analizy / self-learning)
            if self.save_signal_fn:
                try:
                    self.save_signal_fn(ticker_data, trigger_name, trigger_desc)
                except Exception as e:
                    logger.warning(f"Momentum DB save error {ticker}: {e}")

            self._alerted[ticker] = now_chicago()
            self._daily_alerts.add(ticker)
            alerts_sent += 1

            logger.info(
                'Momentum [' + trigger_name + ']: ' + ticker +
                ' | $' + '{:.2f}'.format(ticker_data.get('price', 0)) +
                ' | vol ' + '{:.0f}'.format(ticker_data.get('volume_ratio', 0)) + 'x' +
                ' [DB only]'
            )

        return alerts_sent

    def get_stats(self):
        return {
            'daily_alerts':    len(self._daily_alerts),
            'daily_tickers':   list(self._daily_alerts),
            'cooldown_active': sum(1 for t in self._alerted if not self._in_cooldown(t)),
        }
