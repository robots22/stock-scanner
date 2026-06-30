#!/usr/bin/env python3
"""
STOCK SCANNER - MOMENTUM SCAN (Tryb 2) v2.0
Zapisz jako momentum_scan.py w folderze stock-scanner

Zadanie:
  Prosty momentum scanner bez Claude AI.
  Wykrywa nano/micro-cap runners przez gap + volume bezwzgledny.
  Zero kosztow Claude.

  Sygnaly zapisywane do bazy (verdict='MOMENTUM') dla self-learning.
  Telegram alerty wylaczone - tylko DB + log.

Triggery v2.0 (oparte na bezwzglednym volume, nie ratio):

  A) GAP_MONSTER: gap > 50% + volume > 500k
     → CELZ gap+222%, JEM gap+357%, BIYA gap+53%

  B) NANO_CAP: cena < $1 + volume > 5M + (gap > 5% LUB zmiana > 15%)
     → YOOV, LGCL, BIYA, BTCT

  C) MICRO_CAP: cena $1-$5 + volume > 3M + (gap > 10% LUB zmiana > 20%)
     → JEM, CELZ, CUPR

  D) LOW_FLOAT: float < 5M + volume > 1M + zmiana > 0
     → klasyczny low float runner

  E) ULTRA_RVOL: volume_ratio > 100x + zmiana > 0
     → klasyczny spike

Historia zmian:
  v1.0 - pierwsza wersja
  v1.1 - nowy format, timing
  v1.2 - silniejsze progi
  v1.3 - Telegram wylaczony, zapis do DB
  v2.0 - nowe triggery: bezwzgledny volume zamiast ratio
         nano/micro cap tiers, GAP_MONSTER
"""

from config import logger, CONFIG, now_chicago

MOMENTUM_CONFIG = {
    # Trigger A: GAP_MONSTER
    'trigger_a_gap':    50.0,
    'trigger_a_vol':    500_000,

    # Trigger B: NANO_CAP (cena < $1)
    'trigger_b_max_price': 1.00,
    'trigger_b_vol':    5_000_000,
    'trigger_b_gap':    5.0,
    'trigger_b_change': 15.0,

    # Trigger C: MICRO_CAP (cena $1-$5)
    'trigger_c_min_price': 1.00,
    'trigger_c_max_price': 5.00,
    'trigger_c_vol':    3_000_000,
    'trigger_c_gap':    10.0,
    'trigger_c_change': 20.0,

    # Trigger D: LOW_FLOAT
    'trigger_d_float':  5_000_000,
    'trigger_d_vol':    1_000_000,

    # Trigger E: ULTRA_RVOL (stary trigger jako backup)
    'trigger_e_rvol':   100.0,

    # Filtry bazowe
    'min_price':         0.10,  # nizej = OTC/delisted
    'max_price':         20.0,
    'min_volume':        500_000,

    # Wymagaj dodatniej zmiany
    'require_positive_change': True,

    # Cooldown i limity
    'cooldown_min':      15,
    'max_alerts_per_cycle': 5,  # wiecej bo nano-cap runners mnogie
}

EXCLUDED_SUFFIXES = ('W', 'WS', 'WW', 'R', 'RT', 'U')


class MomentumScanner:

    def __init__(self, polygon_api, telegram_send_fn, save_signal_fn=None):
        self.polygon        = polygon_api
        self.send            = telegram_send_fn
        self.save_signal_fn  = save_signal_fn
        self._alerted        = {}
        self._daily_alerts   = set()
        self._alert_date     = None
        logger.info("MomentumScanner v2.0 zainicjowany (nano/micro-cap triggers)")

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

        # Filtry bazowe
        if not (cfg['min_price'] <= price <= cfg['max_price']):
            return None, None
        if volume < cfg['min_volume']:
            return None, None
        if cfg['require_positive_change'] and change <= 0:
            return None, None

        # Trigger A: GAP_MONSTER
        if gap >= cfg['trigger_a_gap'] and volume >= cfg['trigger_a_vol']:
            return 'GAP_MONSTER', (
                'Gap ' + '{:+.0f}'.format(gap) + '% + vol ' +
                '{:.1f}'.format(volume/1_000_000) + 'M'
            )

        # Trigger B: NANO_CAP
        if (price < cfg['trigger_b_max_price'] and
                volume >= cfg['trigger_b_vol'] and
                (gap >= cfg['trigger_b_gap'] or change >= cfg['trigger_b_change'])):
            return 'NANO_CAP', (
                'Nano $' + '{:.2f}'.format(price) +
                ' vol ' + '{:.1f}'.format(volume/1_000_000) + 'M' +
                (' gap' + '{:+.0f}'.format(gap) + '%' if gap else
                 ' +' + '{:.0f}'.format(change) + '%')
            )

        # Trigger C: MICRO_CAP
        if (cfg['trigger_c_min_price'] <= price <= cfg['trigger_c_max_price'] and
                volume >= cfg['trigger_c_vol'] and
                (gap >= cfg['trigger_c_gap'] or change >= cfg['trigger_c_change'])):
            return 'MICRO_CAP', (
                'Micro $' + '{:.2f}'.format(price) +
                ' vol ' + '{:.1f}'.format(volume/1_000_000) + 'M' +
                (' gap' + '{:+.0f}'.format(gap) + '%' if gap else
                 ' +' + '{:.0f}'.format(change) + '%')
            )

        # Trigger D: LOW_FLOAT
        if (float_sh > 0 and float_sh <= cfg['trigger_d_float'] and
                volume >= cfg['trigger_d_vol']):
            float_m = float_sh / 1_000_000
            return 'LOW_FLOAT', (
                'Float ' + '{:.1f}'.format(float_m) + 'M' +
                ' vol ' + '{:.1f}'.format(volume/1_000_000) + 'M'
            )

        # Trigger E: ULTRA_RVOL (backup)
        if ratio >= cfg['trigger_e_rvol']:
            return 'ULTRA_RVOL', (
                'Vol ' + '{:.0f}'.format(ratio) + 'x ekstremalny'
            )

        return None, None

    def scan(self, universe):
        """
        Glowna funkcja skanowania.
        Zapisuje trafienia do bazy dla self-learning.
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

        # Priorytet: GAP_MONSTER > NANO_CAP > LOW_FLOAT > MICRO_CAP > ULTRA_RVOL
        # W ramach priorytetu: soruj po volume bezwzglednym
        priority = {
            'GAP_MONSTER': 5,
            'NANO_CAP':    4,
            'LOW_FLOAT':   3,
            'MICRO_CAP':   2,
            'ULTRA_RVOL':  1,
        }
        triggered.sort(
            key=lambda x: (
                priority.get(x[1], 0),
                x[0].get('volume', 0)
            ),
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
                'Tryb2 [' + trigger_name + ']: ' + ticker +
                ' | $' + '{:.2f}'.format(ticker_data.get('price', 0)) +
                ' | ' + trigger_desc +
                ' | chg ' + '{:+.1f}'.format(ticker_data.get('change_pct', 0)) + '%'
                ' [DB only]'
            )

        return alerts_sent

    def get_stats(self):
        return {
            'daily_alerts':    len(self._daily_alerts),
            'daily_tickers':   list(self._daily_alerts),
            'cooldown_active': sum(1 for t in self._alerted if not self._in_cooldown(t)),
        }
