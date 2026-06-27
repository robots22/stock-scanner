#!/usr/bin/env python3
"""
STOCK SCANNER - MOMENTUM SCAN (Tryb 2)
Zapisz jako momentum_scan.py w folderze stock-scanner

Zadanie:
  Prosty momentum scanner bez Claude AI.
  Wykrywa volume spikes + gap + float runners.
  Zero kosztow Claude — czysty signal oparty na danych.

Triggery (wystarczy jeden):
  A) RVOL > 3x + zmiana > 5%           (momentum)
  B) Float < 5M + RVOL > 50x           (low float runner)
  C) Gap > 10% + RVOL > 2x             (gap play)
  D) RVOL > 100x (ekstremalny spike)   (ultra spike)

Format alertu:
  Bez BUY/WATCH/AVOID
  Prosto: ticker, zmiana, volume, float, gap
  Trader sam decyduje

Historia zmian:
  v1.0 - pierwsza wersja
"""

from config import logger, CONFIG, now_chicago
from datetime import datetime

# ==================== PARAMETRY ====================

MOMENTUM_CONFIG = {
    # Trigger A: momentum
    'trigger_a_rvol':   3.0,
    'trigger_a_change': 5.0,

    # Trigger B: low float runner
    'trigger_b_float':  5_000_000,
    'trigger_b_rvol':   50.0,

    # Trigger C: gap play
    'trigger_c_gap':    10.0,
    'trigger_c_rvol':   2.0,

    # Trigger D: ultra spike
    'trigger_d_rvol':   100.0,

    # Cena
    'min_price': 0.50,
    'max_price': 20.0,  # szerszy zakres niz tryb 1

    # Volume
    'min_volume': 50_000,

    # Cooldown per ticker (minuty)
    'cooldown_min': 15,

    # Max alertow na cykl
    'max_alerts_per_cycle': 5,
}

# Wykluczenia
EXCLUDED_SUFFIXES = ('W', 'WS', 'WW', 'R', 'RT', 'U')


class MomentumScanner:
    """
    Prosty momentum scanner bez AI.
    Uruchamiany co 5 minut jako Tryb 2.
    """

    def __init__(self, polygon_api, telegram_send_fn):
        self.polygon   = polygon_api
        self.send      = telegram_send_fn
        self._alerted  = {}  # {ticker: last_alert_time}
        self._daily_alerts = set()  # tickery alertowane dzisiaj
        self._alert_date   = None
        logger.info("MomentumScanner zainicjowany")

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
        """
        Sprawdza czy ticker spelnia ktorykolwiek trigger.
        Zwraca (trigger_name, opis) lub (None, None).
        """
        ratio       = ticker_data.get('volume_ratio', 0)
        change      = ticker_data.get('change_pct', 0)
        gap         = ticker_data.get('gap_pct', 0)
        float_sh    = ticker_data.get('float_shares') or 0
        volume      = ticker_data.get('volume', 0)
        price       = ticker_data.get('price', 0)

        cfg = MOMENTUM_CONFIG

        # Filtr bazowy
        if not (cfg['min_price'] <= price <= cfg['max_price']):
            return None, None
        if volume < cfg['min_volume']:
            return None, None

        # Trigger D: ultra spike (najwyzszy priorytet)
        if ratio >= cfg['trigger_d_rvol']:
            return 'ULTRA_SPIKE', f"Vol {ratio:.0f}x ekstremalny"

        # Trigger B: low float runner
        if (float_sh > 0 and
                float_sh <= cfg['trigger_b_float'] and
                ratio >= cfg['trigger_b_rvol']):
            float_m = float_sh / 1_000_000
            return 'LOW_FLOAT', f"Float {float_m:.1f}M + vol {ratio:.0f}x"

        # Trigger C: gap play
        if gap >= cfg['trigger_c_gap'] and ratio >= cfg['trigger_c_rvol']:
            return 'GAP_PLAY', f"Gap {gap:+.0f}% + vol {ratio:.1f}x"

        # Trigger A: momentum
        if ratio >= cfg['trigger_a_rvol'] and change >= cfg['trigger_a_change']:
            return 'MOMENTUM', f"Vol {ratio:.1f}x + zmiana {change:+.1f}%"

        return None, None

    def _format_alert(self, ticker_data, trigger_name, trigger_desc):
        """Formatuje alert Telegram bez BUY/WATCH/AVOID."""
        ticker   = ticker_data.get('ticker', '')
        price    = ticker_data.get('price', 0)
        change   = ticker_data.get('change_pct', 0)
        volume   = ticker_data.get('volume', 0)
        ratio    = ticker_data.get('volume_ratio', 0)
        gap      = ticker_data.get('gap_pct', 0)
        float_sh = ticker_data.get('float_shares')
        vwap     = ticker_data.get('vwap', 0)
        high     = ticker_data.get('high', 0)
        time_str = now_chicago().strftime('%H:%M CST')
        dash     = chr(8212)

        trigger_emoji = {
            'ULTRA_SPIKE': chr(128308) + chr(128308),
            'LOW_FLOAT':   chr(9889) + chr(9889),
            'GAP_PLAY':    chr(128200),
            'MOMENTUM':    chr(9889),
        }.get(trigger_name, chr(9889))

        # Float string
        float_str = ''
        if float_sh:
            if float_sh < 1_000_000:
                float_str = f" | Float {float_sh/1000:.0f}k"
            else:
                float_str = f" | Float {float_sh/1_000_000:.1f}M"

        # Gap string
        gap_str = f" | Gap {gap:+.0f}%" if gap else ''

        # VWAP string
        vwap_str = ''
        if vwap and price:
            vwap_pct = ((price - vwap) / vwap) * 100
            vwap_str = f" | VWAP {vwap_pct:+.1f}%"

        # HOD string
        hod_str = ''
        if high and price:
            hod_pct = ((price - high) / high) * 100
            if hod_pct >= -1.0:
                hod_str = ' | HOD!'

        lines = [
            trigger_emoji + ' <b>' + ticker + '</b> ' + dash + ' ' + trigger_desc,
            chr(9200) + ' ' + time_str,
            '',
            chr(128176) + ' $' + '{:.2f}'.format(price) +
            ' (' + '{:+.1f}'.format(change) + '%)',
            chr(128202) + ' Vol: ' + '{:,}'.format(volume) +
            ' (' + '{:.0f}'.format(ratio) + 'x)' +
            float_str + gap_str + vwap_str + hod_str,
            '',
            chr(128203) + ' Tryb 2 ' + dash + ' momentum alert (bez AI)',
            chr(9888) + ' Wejscie na wlasna odpowiedzialnosc',
        ]

        return chr(10).join(lines)

    def scan(self, universe):
        """
        Glowna funkcja skanowania.
        Przyjmuje universe z polygon_api.get_universe().
        """
        self._reset_daily()

        if not universe:
            return 0

        alerts_sent = 0
        triggered   = []

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

        # Sortuj po: ultra spike > low float > gap > momentum
        priority = {'ULTRA_SPIKE': 4, 'LOW_FLOAT': 3, 'GAP_PLAY': 2, 'MOMENTUM': 1}
        triggered.sort(
            key=lambda x: (priority.get(x[1], 0), x[0].get('volume_ratio', 0)),
            reverse=True
        )

        # Wyslij max N alertow
        max_alerts = MOMENTUM_CONFIG['max_alerts_per_cycle']
        for ticker_data, trigger_name, trigger_desc in triggered[:max_alerts]:
            ticker  = ticker_data.get('ticker', '')
            message = self._format_alert(ticker_data, trigger_name, trigger_desc)

            if self.send(message):
                self._alerted[ticker]     = now_chicago()
                self._daily_alerts.add(ticker)
                alerts_sent += 1
                logger.info(
                    f"Momentum [{trigger_name}]: {ticker} | "
                    f"${ticker_data.get('price', 0):.2f} | "
                    f"vol {ticker_data.get('volume_ratio', 0):.0f}x"
                )

        return alerts_sent

    def get_stats(self):
        return {
            'daily_alerts':    len(self._daily_alerts),
            'daily_tickers':   list(self._daily_alerts),
            'cooldown_active': sum(1 for t in self._alerted
                                   if not self._in_cooldown(t)),
        }
