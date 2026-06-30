#!/usr/bin/env python3
"""
STOCK SCANNER - MOMENTUM SCAN (Tryb 2) v2.0
Zapisz jako momentum_scan.py w folderze stock-scanner

Triggery v2.0 (bezwzgledny volume, nie ratio):
  A) GAP_MONSTER: gap > 50% + volume > 500k
  B) NANO_CAP:    cena < $1 + volume > 5M + (gap > 5% LUB zmiana > 15%)
  C) MICRO_CAP:   cena $1-$5 + volume > 3M + (gap > 10% LUB zmiana > 20%)
  D) LOW_FLOAT:   float < 5M + volume > 1M
  E) ULTRA_RVOL:  ratio > 100x (backup)

Format alertu: taki sam jak Tryb 1 ale z headerem TRYB 2.
Zapis do DB (verdict=MOMENTUM) dla self-learning.
"""

from config import logger, CONFIG, now_chicago

MOMENTUM_CONFIG = {
    'trigger_a_gap':       50.0,
    'trigger_a_vol':       500_000,
    'trigger_b_max_price': 1.00,
    'trigger_b_vol':       5_000_000,
    'trigger_b_gap':       5.0,
    'trigger_b_change':    15.0,
    'trigger_c_min_price': 1.00,
    'trigger_c_max_price': 5.00,
    'trigger_c_vol':       3_000_000,
    'trigger_c_gap':       10.0,
    'trigger_c_change':    20.0,
    'trigger_d_float':     5_000_000,
    'trigger_d_vol':       1_000_000,
    'trigger_e_rvol':      100.0,
    'min_price':           0.10,
    'max_price':           20.0,
    'min_volume':          500_000,
    'require_positive_change': True,
    'cooldown_min':        15,
    'max_alerts_per_cycle': 5,
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
        logger.info("MomentumScanner v2.0 zainicjowany")

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
        if cfg['require_positive_change'] and change <= 0:
            return None, None

        if gap >= cfg['trigger_a_gap'] and volume >= cfg['trigger_a_vol']:
            return 'GAP_MONSTER', 'Gap ' + '{:+.0f}'.format(gap) + '% vol ' + '{:.1f}'.format(volume/1_000_000) + 'M'

        if (price < cfg['trigger_b_max_price'] and
                volume >= cfg['trigger_b_vol'] and
                (gap >= cfg['trigger_b_gap'] or change >= cfg['trigger_b_change'])):
            return 'NANO_CAP', ('$' + '{:.2f}'.format(price) +
                ' vol ' + '{:.1f}'.format(volume/1_000_000) + 'M' +
                (' gap' + '{:+.0f}'.format(gap) + '%' if gap else ' +' + '{:.0f}'.format(change) + '%'))

        if (cfg['trigger_c_min_price'] <= price <= cfg['trigger_c_max_price'] and
                volume >= cfg['trigger_c_vol'] and
                (gap >= cfg['trigger_c_gap'] or change >= cfg['trigger_c_change'])):
            return 'MICRO_CAP', ('$' + '{:.2f}'.format(price) +
                ' vol ' + '{:.1f}'.format(volume/1_000_000) + 'M' +
                (' gap' + '{:+.0f}'.format(gap) + '%' if gap else ' +' + '{:.0f}'.format(change) + '%'))

        if (float_sh > 0 and float_sh <= cfg['trigger_d_float'] and
                volume >= cfg['trigger_d_vol']):
            float_m = float_sh / 1_000_000
            return 'LOW_FLOAT', 'Float ' + '{:.1f}'.format(float_m) + 'M vol ' + '{:.1f}'.format(volume/1_000_000) + 'M'

        if ratio >= cfg['trigger_e_rvol']:
            return 'ULTRA_RVOL', 'Vol ' + '{:.0f}'.format(ratio) + 'x'

        return None, None

    def _format_alert(self, ticker_data, trigger_name, trigger_desc):
        """Format jak Tryb 1 ale z headerem TRYB 2."""
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

        # Trigger emoji
        t_emoji = {
            'GAP_MONSTER': chr(128640),
            'NANO_CAP':    chr(9889) + chr(9889),
            'MICRO_CAP':   chr(9889),
            'LOW_FLOAT':   chr(128011),
            'ULTRA_RVOL':  chr(128308) + chr(128308),
        }.get(trigger_name, chr(9889))

        # Float string
        float_str = ''
        if float_sh:
            if float_sh < 1_000_000:
                float_str = ' | Float ' + '{:.0f}'.format(float_sh/1000) + 'k'
            else:
                float_str = ' | Float ' + '{:.1f}'.format(float_sh/1_000_000) + 'M'

        # Gap string
        gap_str = (' | Gap ' + '{:+.0f}'.format(gap) + '%') if gap else ''

        # VWAP
        vwap_pct = 0
        vwap_str = ''
        if vwap and price:
            vwap_pct = ((price - vwap) / vwap) * 100
            vwap_str = ' | VWAP ' + '{:+.1f}'.format(vwap_pct) + '%'

        # HOD
        hod_str = ''
        if high and price and ((price - high) / high) * 100 >= -1.0:
            hod_str = ' | ' + chr(128293) + 'HOD'

        # Entry suggestion
        if vwap and price and abs(vwap_pct) <= 5:
            entry_price  = round(vwap * 1.005, 2)
            entry_reason = 'VWAP'
        else:
            entry_price  = price
            entry_reason = 'current'

        stop_price   = round(entry_price * 0.95, 2)   # -5% (wiekszy stop dla nano/micro)
        target_price = round(entry_price * 1.15, 2)   # +15% (agresywny target)
        risk         = entry_price - stop_price
        reward       = target_price - entry_price
        rr           = round(reward / risk, 1) if risk > 0 else 3.0

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
            t_emoji + ' <b>TRYB 2 ' + dash + ' ' + ticker + '</b> [' + trigger_name + ']',
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
            '  Stop:    $' + '{:.2f}'.format(stop_price) + ' (-5%)',
            '  Target:  $' + '{:.2f}'.format(target_price) + ' (+15%)',
            '  R/R:     ' + '{:.1f}'.format(rr) + ':1',
            '',
            chr(128203) + ' ' + trigger_desc,
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

        priority = {'GAP_MONSTER': 5, 'NANO_CAP': 4, 'LOW_FLOAT': 3, 'MICRO_CAP': 2, 'ULTRA_RVOL': 1}
        triggered.sort(
            key=lambda x: (priority.get(x[1], 0), x[0].get('volume', 0)),
            reverse=True
        )

        alerts_sent = 0
        for ticker_data, trigger_name, trigger_desc in triggered[:MOMENTUM_CONFIG['max_alerts_per_cycle']]:
            ticker = ticker_data.get('ticker', '')

            # Zapisz do bazy
            if self.save_signal_fn:
                try:
                    self.save_signal_fn(ticker_data, trigger_name, trigger_desc)
                except Exception as e:
                    logger.warning(f"Momentum DB save error {ticker}: {e}")

            # Wyslij Telegram
            message = self._format_alert(ticker_data, trigger_name, trigger_desc)
            self.send(message)

            self._alerted[ticker] = now_chicago()
            self._daily_alerts.add(ticker)
            alerts_sent += 1

            logger.info(
                'Tryb2 [' + trigger_name + ']: ' + ticker +
                ' $' + '{:.2f}'.format(ticker_data.get('price', 0)) +
                ' ' + trigger_desc
            )

        return alerts_sent

    def get_stats(self):
        return {
            'daily_alerts':    len(self._daily_alerts),
            'daily_tickers':   list(self._daily_alerts),
            'cooldown_active': sum(1 for t in self._alerted if not self._in_cooldown(t)),
        }
