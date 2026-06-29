#!/usr/bin/env python3
"""
STOCK SCANNER - ALPACA PAPER TRADER
Zapisz jako alpaca_trader.py w folderze stock-scanner

Zadanie:
- Automatycznie sklada zlecenia w Alpaca Paper Trading
- BUY signal  -> otwiera pozycje (bracket order z SL/TP)
- SELL signal -> zamyka pozycje (market order)
- Sledzi otwarte pozycje

Historia zmian:
    v1.0 - pierwsza wersja, paper trading
"""

import os
import requests
from config import logger, CONFIG, now_chicago
from dotenv import load_dotenv
import pathlib

_ENV_PATH = pathlib.Path(__file__).parent / '.env'
load_dotenv(dotenv_path=_ENV_PATH, override=True)

PAPER_KEY    = os.getenv('ALPACA_PAPER_KEY', '')
PAPER_SECRET = os.getenv('ALPACA_PAPER_SECRET', '')
PAPER_URL    = 'https://paper-api.alpaca.markets'

RISK_PER_TRADE_USD = float(os.getenv('PAPER_RISK_PER_TRADE', '100'))
MAX_POSITIONS      = int(os.getenv('PAPER_MAX_POSITIONS', '3'))


class AlpacaPaperTrader:

    def __init__(self):
        if not PAPER_KEY or not PAPER_SECRET:
            logger.warning("AlpacaPaperTrader: brak ALPACA_PAPER_KEY w .env — wylaczony")
            self.enabled = False
            return

        self.enabled = True
        self.session = requests.Session()
        self.session.headers.update({
            'APCA-API-KEY-ID':     PAPER_KEY,
            'APCA-API-SECRET-KEY': PAPER_SECRET,
            'Content-Type':        'application/json',
        })
        self._pending_trailing = {}
        self._submitted_orders = set()  # tickery z juz zlozonym zleceniem BUY
        logger.info("AlpacaPaperTrader zainicjowany — Paper Trading")

    def _get(self, endpoint):
        try:
            r = self.session.get(f"{PAPER_URL}{endpoint}", timeout=10)
            if r.status_code == 200:
                return r.json()
            logger.warning(f"Alpaca GET {endpoint}: {r.status_code}")
            return None
        except Exception as e:
            logger.error(f"Alpaca GET error: {e}")
            return None

    def _post(self, endpoint, data):
        try:
            r = self.session.post(f"{PAPER_URL}{endpoint}", json=data, timeout=10)
            if r.status_code in (200, 201):
                return r.json()
            logger.warning(f"Alpaca POST {endpoint}: {r.status_code} — {r.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"Alpaca POST error: {e}")
            return None

    def _delete(self, endpoint):
        try:
            r = self.session.delete(f"{PAPER_URL}{endpoint}", timeout=10)
            return r.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Alpaca DELETE error: {e}")
            return False

    def get_account(self):
        return self._get('/v2/account')

    def get_positions(self):
        data = self._get('/v2/positions')
        return data if data else []

    def get_position(self, ticker):
        return self._get(f'/v2/positions/{ticker}')

    def count_open_positions(self):
        positions = self.get_positions()
        return len(positions) if positions else 0

    def buy(self, ticker, entry_price, stop_loss=None, take_profit=None):
        """
        Otwiera pozycje BUY.
        Uzywa bracket order z trailing stop jezeli dostepne.
        Qty = RISK_PER_TRADE_USD / risk_per_share
        """
        if not self.enabled:
            return None

        if self.count_open_positions() >= MAX_POSITIONS:
            logger.warning(f"Paper trader: max pozycji ({MAX_POSITIONS}) — pomijam {ticker}")
            return None

        if self.get_position(ticker):
            logger.info(f"Paper trader: {ticker} juz w portfelu — pomijam")
            return None

        if ticker in self._submitted_orders:
            logger.info(f"Paper trader: {ticker} zlecenie juz zlozone — pomijam")
            return None

        # Oblicz qty
        if stop_loss and stop_loss > 0 and entry_price > stop_loss:
            risk_per_share = entry_price - stop_loss
            qty = max(1, int(RISK_PER_TRADE_USD / risk_per_share))
        else:
            qty = max(1, int(RISK_PER_TRADE_USD / entry_price))

        max_qty = int(RISK_PER_TRADE_USD * 3 / entry_price)
        qty     = min(qty, max_qty)

        # Trailing stop percent
        # Oblicz na podstawie stop_loss jezeli dostepny
        # Inaczej domyslnie 4%
        if stop_loss and stop_loss > 0 and entry_price > stop_loss:
            trail_pct = round((entry_price - stop_loss) / entry_price * 100, 1)
            trail_pct = max(3.0, min(trail_pct, 6.0))  # klamp 3-6%
        else:
            trail_pct = 4.0

        order = {
            'symbol':        ticker,
            'qty':           str(qty),
            'side':          'buy',
            'type':          'market',
            'time_in_force': 'day',
        }

        result = self._post('/v2/orders', order)

        if result:
            self._submitted_orders.add(ticker)  # zapamietaj ze zlecenie zlozone
            logger.info(f"Paper BUY: {ticker} x{qty} @ ~${entry_price:.2f} | trailing {trail_pct}%")
            self._submit_trailing_stop(ticker, str(qty), str(trail_pct))
            return result
        return None

    def _submit_trailing_stop(self, ticker, qty, trail_pct):
        """Sklada trailing stop order dla otwartej pozycji."""
        import time
        time.sleep(1)  # poczekaj na wykonanie BUY
        try:
            order = {
                'symbol':        ticker,
                'qty':           qty,
                'side':          'sell',
                'type':          'trailing_stop',
                'time_in_force': 'gtc',
                'trail_percent': trail_pct,
            }
            result = self._post('/v2/orders', order)
            if result:
                logger.info(f"Trailing stop: {ticker} {trail_pct}%")
            else:
                logger.warning(f"Trailing stop FAIL: {ticker}")
        except Exception as e:
            logger.warning(f"Trailing stop error {ticker}: {e}")

    def sell(self, ticker, reason='SELL_SIGNAL'):
        """Zamyka pozycje dla tickera."""
        if not self.enabled:
            return None

        position = self.get_position(ticker)
        if not position:
            logger.info(f"Paper trader: {ticker} brak pozycji do zamkniecia")
            return None

        price   = float(position.get('current_price', 0))
        pnl     = float(position.get('unrealized_pl', 0))
        pnl_pct = float(position.get('unrealized_plpc', 0)) * 100

        result = self._delete(f'/v2/positions/{ticker}')
        if result:
            self._submitted_orders.discard(ticker)  # pozwol na nowy BUY
            logger.info(f"Paper SELL: {ticker} @ ${price:.2f} | P&L: ${pnl:.2f} ({pnl_pct:+.1f}%) | {reason}")
            return {'ticker': ticker, 'pnl': pnl, 'pnl_pct': pnl_pct}
        return None

    def get_portfolio_summary(self):
        """Podsumowanie portfela Paper."""
        if not self.enabled:
            return None

        account   = self.get_account()
        positions = self.get_positions()

        if not account:
            return None

        equity    = float(account.get('equity', 0))
        cash      = float(account.get('cash', 0))
        buy_pwr   = float(account.get('buying_power', 0))
        total_pnl = sum(float(p.get('unrealized_pl', 0)) for p in positions)
        pnl_pct   = (total_pnl / equity * 100) if equity > 0 else 0

        return {
            'equity':        equity,
            'cash':          cash,
            'buying_power':  buy_pwr,
            'positions':     len(positions),
            'total_pnl':     total_pnl,
            'total_pnl_pct': pnl_pct,
            'open_positions': [
                {
                    'ticker':  p.get('symbol'),
                    'qty':     p.get('qty'),
                    'entry':   float(p.get('avg_entry_price', 0)),
                    'current': float(p.get('current_price', 0)),
                    'pnl':     float(p.get('unrealized_pl', 0)),
                    'pnl_pct': float(p.get('unrealized_plpc', 0)) * 100,
                }
                for p in positions
            ]
        }


if __name__ == "__main__":
    print("\n" + "="*55)
    print("  TEST: Alpaca Paper Trader")
    print("="*55)

    trader = AlpacaPaperTrader()

    if not trader.enabled:
        print("\n❌ Brak kluczy Paper — dodaj ALPACA_PAPER_KEY i ALPACA_PAPER_SECRET do .env")
        exit(1)

    account = trader.get_account()
    if account:
        print(f"\n✅ Konto Paper:")
        print(f"  Equity:       ${float(account.get('equity', 0)):,.2f}")
        print(f"  Cash:         ${float(account.get('cash', 0)):,.2f}")
        print(f"  Buying Power: ${float(account.get('buying_power', 0)):,.2f}")
        print(f"  Status:       {account.get('status')}")
    else:
        print("\n❌ Blad polaczenia z Alpaca Paper")
        exit(1)

    positions = trader.get_positions()
    print(f"\n✅ Otwarte pozycje: {len(positions)}")
    for p in positions:
        print(f"  {p.get('symbol'):6s} | P&L: ${float(p.get('unrealized_pl', 0)):+.2f}")

    print("\n" + "="*55)
    print("  AlpacaPaperTrader gotowy ✅")
    print("="*55)
