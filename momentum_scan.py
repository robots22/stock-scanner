#!/usr/bin/env python3
"""
STOCK SCANNER - MOMENTUM SCAN (Tryb 2)
Zapisz jako momentum_scan.py w folderze stock-scanner

Zadanie:
  Prosty momentum scanner bez Claude AI.
  Wykrywa volume spikes + gap + float runners.
  Zero kosztow Claude.

Triggery (wystarczy jeden):
  A) RVOL > 3x + zmiana > 5%           (momentum)
  B) Float < 5M + RVOL > 50x           (low float runner)
  C) Gap > 10% + RVOL > 2x             (gap play)
  D) RVOL > 100x                        (ultra spike)

Historia zmian:
  v1.0 - pierwsza wersja
  v1.1 - nowy format: TRYB 2 widoczny, entry/stop/target, timing WCZESNY/SREDNI/POZNY
"""

from config import logger, CONFIG, now_chicago

MOMENTUM_CONFIG = {
    'trigger_a_rvol':    3.0,
    'trigger_a_change':  5.0,
    'trigger_b_float':   5_000_000,
    'trigger_b_rvol':    50.0,
    'trigger_c_gap':     10.0,
    'trigger_c_rvol':    2.0,
    'trigger_d_rvol':    100.0,
    'min_price':         0.50,
    'max_price':         20.0,
    'min_volume':        50_000,
    'cooldown_min':      15,
    'max_alerts_per_cycle': 5,
}

EXCLUDED_SUFFIXES = ('W', 'WS', 'WW', 'R', 'RT', 'U')


class MomentumScanner:

    def __init__(self, polygon_api, telegram_send_fn):
        self.polygon       = polygon_api
        self.send          = telegram_send_fn
        self._alerted      = {}
        self._daily_alerts = set()
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

        if ratio >= cfg['trigger_d_rvol']:
            return 'ULTRA_SPIKE', 'Vol ' + '{:.0f}'.format(ratio) + 'x ekstremalny'

        if float_sh > 0 and float_sh <= cfg['trigger_b_float'] and ratio >= cfg['trigger_b_rvol']:
            float_m = float_sh / 1_000_000
            return 'LOW_FLOAT', 'Float ' + '{:.1f}'.format(float_m) + 'M + vol ' + '{:.0f}'.format(ratio) + 'x'

        if gap >= cfg['trigger_c_gap'] and ratio >= cfg['trigger_c_rvol']:
            return 'GAP_PLAY', 'Gap ' + '{:+.0f}'.format(gap) + '% + vol ' + '{:.1f}'.format(ratio) + 'x'

        if ratio >= cfg['trigger_a_rvol'] and change >= cfg['trigger_a_change']:
            return 'MOMENTUM', 'Vol ' + '{:.1f}'.format(ratio) + 'x + zmiana ' + '{:+.1f}'.format(change) + '%'

        return None, None

    def _format_alert(self, ticker_data, trigger_name, trigger_desc):
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

        # Float
        float_str = ''
        if float_sh:
            if float_sh < 1_000_000:
                float_str = ' | Float ' + '{:.0f}'.format(float_sh / 1000) + 'k'
            else:
                float_str = ' | Float ' + '{:.1f}'.format(float_sh / 1_000_000) + 'M'

        # Gap
        gap_str = (' | Gap ' + '{:+.0f}'.format(gap) + '%') if gap else ''

        # VWAP
        vwap_pct = 0
        vwap_str = ''
        if vwap and price:
            vwap_pct = ((price - vwap) / vwap) * 100
            vwap_str = ' | VWAP ' + '{:+.1f}'.format(vwap_pct) + '%'

        # HOD
        hod_str = ''
        if high and price:
            hod_pct = ((price - high) / high) * 100
            if hod_pct >= -1.0:
                hod_str = ' | ' + chr(128293) + 'HOD'

        # Entry suggestion
        if vwap and price and abs(vwap_pct) <= 5:
            entry_price  = round(vwap * 1.005, 2)
            entry_reason = 'VWAP'
        else:
            entry_price  = price
            entry_reason = 'current'

        stop_price   = round(entry_price * 0.97, 2)
        target_price = round(entry_price * 1.06, 2)
        risk         = entry_price - stop_price
        reward       = target_price - entry_price
        rr           = round(reward / risk, 1) if risk > 0 else 2.0

        # Timing
        n = now_chicago()
        open_time   = n.replace(hour=8, minute=30, second=0, microsecond=0)
        elapsed_min = (n - open_time).total_seconds() / 60 if n > open_time else 0

        if elapsed_min < 30:
            timing = chr(128994) + ' WCZESNY'
        elif elapsed_min < 90:
            timing = chr(128993) + ' SREDNI'
        else:
            timing = chr(128308) + ' POZNY'

        lines = [
            chr(9889) + chr(9889) + ' <b>TRYB 2 ' + dash + ' ' + ticker + '</b> ' + trigger_emoji,
            chr(9200) + ' ' + time_str + ' | ' + timing,
            '',
            chr(128176) + ' $' + '{:.2f}'.format(price) +
            ' (' + '{:+.1f}'.format(change) + '%)' +
            gap_str + float_str,
            chr(128202) + ' Vol: ' + '{:,}'.format(volume) +
            ' (' + '{:.0f}'.format(ratio) + 'x)' +
            vwap_str + hod_str,
            '',
            chr(128073) + ' <b>SUGEROWANY ENTRY:</b>',
            '  Wejscie: <b>$' + '{:.2f}'.format(entry_price) + '</b> [' + entry_reason + ']',
            '  Stop:    $' + '{:.2f}'.format(stop_price) + ' (-3%)',
            '  Target:  $' + '{:.2f}'.format(target_price) + ' (+6%)',
            '  R/R:     ' + '{:.1f}'.format(rr) + ':1',
            '',
            chr(9888) + ' Decyzja i ryzyko po stronie tradera',
        ]

        return chr(10).join(lines)

    def scan(self, universe):
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

        alerts_sent  = 0
        max_alerts   = MOMENTUM_CONFIG['max_alerts_per_cycle']

        for ticker_data, trigger_name, trigger_desc in triggered[:max_alerts]:
            ticker  = ticker_data.get('ticker', '')
            message = self._format_alert(ticker_data, trigger_name, trigger_desc)

            if self.send(message):
                self._alerted[ticker]     = now_chicago()
                self._daily_alerts.add(ticker)
                alerts_sent += 1
                logger.info(
                    'Momentum [' + trigger_name + ']: ' + ticker +
                    ' | $' + '{:.2f}'.format(ticker_data.get('price', 0)) +
                    ' | vol ' + '{:.0f}'.format(ticker_data.get('volume_ratio', 0)) + 'x'
                )

        return alerts_sent

    def get_stats(self):
        return {
            'daily_alerts':    len(self._daily_alerts),
            'daily_tickers':   list(self._daily_alerts),
            'cooldown_active': sum(1 for t in self._alerted if not self._in_cooldown(t)),
        }
