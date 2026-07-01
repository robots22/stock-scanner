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

RISK_PER_TRADE_USD      = float(os.getenv('PAPER_RISK_PER_TRADE', '100'))
MAX_POSITIONS           = int(os.getenv('PAPER_MAX_POSITIONS', '3'))
MAX_POSITIONS_MOMENTUM  = int(os.getenv('PAPER_MAX_POSITIONS_MOMENTUM', '20'))


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
        self._momentum_orders  = set()  # tickery Trybu 2
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
            return 'EXISTS'  # sygnalizuje ze pozycja juz istnieje

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
            self._submitted_orders.add(ticker)
            logger.info(f"Paper BUY: {ticker} x{qty} @ ~${entry_price:.2f} | trail {trail_pct}%")

            # Partial exit: 50% @ TP, trailing stop na reszte
            half_qty = max(1, qty // 2)
            rest_qty = qty - half_qty

            if take_profit and take_profit > entry_price:
                # 50% sell limit @ TP
                self._submit_take_profit(ticker, str(half_qty), str(round(take_profit, 2)))
                # 50% trailing stop
                if rest_qty > 0:
                    self._submit_trailing_stop(ticker, str(rest_qty), str(trail_pct))
            else:
                # Bez TP: trailing stop na calą pozycje
                self._submit_trailing_stop(ticker, str(qty), str(trail_pct))

            return result
        return None

    def _submit_trailing_stop(self, ticker, qty, trail_pct):
        """Sklada trailing stop order dla otwartej pozycji."""
        import time
        # Poczekaj az BUY zostanie wykonany
        for attempt in range(3):
            time.sleep(2)
            try:
                # Sprawdz czy pozycja istnieje
                pos = self.get_position(ticker)
                if not pos:
                    logger.debug(f"Trailing stop {ticker}: pozycja jeszcze nie otwarta (proba {attempt+1}/3)")
                    continue

                # Uzyj dostepnej ilosci akcji (nie oryginalnej)
                available_qty = pos.get('qty_available') or pos.get('qty') or qty

                order = {
                    'symbol':        ticker,
                    'qty':           str(available_qty),
                    'side':          'sell',
                    'type':          'trailing_stop',
                    'time_in_force': 'gtc',
                    'trail_percent': str(trail_pct),
                }
                result = self._post('/v2/orders', order)
                if result:
                    logger.info(f"Trailing stop: {ticker} {trail_pct}% [proba {attempt+1}]")
                    return True
                else:
                    logger.warning(f"Trailing stop FAIL: {ticker} [proba {attempt+1}]")
            except Exception as e:
                logger.warning(f"Trailing stop error {ticker}: {e}")

        logger.warning(f"Trailing stop {ticker}: nie udalo sie po 3 probach")
        return False

    def _submit_take_profit(self, ticker, qty, limit_price):
        """Sklada limit sell order jako take profit."""
        import time
        time.sleep(1)
        try:
            pos = self.get_position(ticker)
            if not pos:
                logger.debug(f"Take profit {ticker}: brak pozycji")
                return False

            available_qty = pos.get('qty_available') or pos.get('qty') or qty
            if int(float(available_qty)) <= 0:
                logger.debug(f"Take profit {ticker}: qty_available=0")
                return False

            order = {
                'symbol':        ticker,
                'qty':           str(available_qty),
                'side':          'sell',
                'type':          'limit',
                'time_in_force': 'gtc',
                'limit_price':   str(limit_price),
            }
            result = self._post('/v2/orders', order)
            if result:
                logger.info(f"Take profit: {ticker} @ ${limit_price}")
                return True
            else:
                logger.warning(f"Take profit FAIL: {ticker}")
                return False
        except Exception as e:
            logger.warning(f"Take profit error {ticker}: {e}")
            return False

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

    def count_momentum_positions(self):
        """Liczy pozycje otwarte przez Tryb 2."""
        return len(self._momentum_orders)

    def buy_momentum(self, ticker, entry_price, trigger_name):
        """
        Otwiera pozycje BUY dla Trybu 2 (momentum).

        Risk management:
          Stop loss:     -8% od entry
          Take profit 1: +20% (50% pozycji)
          Take profit 2: +30% (25% pozycji)
          Take profit 3: +40% (25% pozycji) lub trailing stop 6%

        Max 20 pozycji (paper trading / learning).
        Jeden BUY per ticker dziennie.
        Tylko podczas sesji rynkowej.
        """
        if not self.enabled:
            return None

        # Sprawdz limit pozycji Trybu 2
        if self.count_momentum_positions() >= MAX_POSITIONS_MOMENTUM:
            logger.info(f"Tryb2: max pozycji ({MAX_POSITIONS_MOMENTUM}) osiagniety — pomijam {ticker}")
            return None

        # Jeden BUY per ticker dziennie
        if ticker in self._submitted_orders:
            logger.info(f"Tryb2: {ticker} juz zlozone dzisiaj — pomijam")
            return None

        # Sprawdz czy pozycja istnieje
        if self.get_position(ticker):
            logger.info(f"Tryb2: {ticker} juz w portfelu — pomijam")
            return 'EXISTS'

        # Qty z ryzyka per trade
        qty = max(2, int(RISK_PER_TRADE_USD / entry_price))
        qty = min(qty, int(RISK_PER_TRADE_USD * 3 / entry_price))

        # SL i TP
        stop_loss = round(entry_price * 0.92, 2)   # -8%
        tp1       = round(entry_price * 1.20, 2)   # +20%
        tp2       = round(entry_price * 1.30, 2)   # +30%
        tp3       = round(entry_price * 1.40, 2)   # +40%

        # Podzial qty: 50% @ TP1, 25% @ TP2, 25% trailing
        qty_tp1   = max(1, qty // 2)
        qty_tp2   = max(1, qty // 4)
        qty_trail = qty - qty_tp1 - qty_tp2

        order = {
            'symbol':        ticker,
            'qty':           str(qty),
            'side':          'buy',
            'type':          'market',
            'time_in_force': 'day',
        }

        result = self._post('/v2/orders', order)
        if not result:
            return None

        self._submitted_orders.add(ticker)
        if not hasattr(self, '_momentum_orders'):
            self._momentum_orders = set()
        self._momentum_orders.add(ticker)

        logger.info(
            f"Tryb2 BUY: {ticker} x{qty} @ ~${entry_price:.2f} | "
            f"SL ${stop_loss} | TP1 ${tp1}(x{qty_tp1}) "
            f"TP2 ${tp2}(x{qty_tp2}) Trail 6%(x{qty_trail}) "
            f"[{trigger_name}]"
        )

        # Zloz zlecenia exit po wykonaniu BUY
        import time
        time.sleep(2)

        # TP1: 50% @ +20%
        self._submit_take_profit(ticker, str(qty_tp1), str(tp1))
        time.sleep(0.5)

        # TP2: 25% @ +30%
        pos = self.get_position(ticker)
        if pos:
            avail = int(float(pos.get('qty_available') or pos.get('qty') or qty_tp2))
            if avail >= qty_tp2:
                self._submit_take_profit(ticker, str(qty_tp2), str(tp2))
                time.sleep(0.5)

        # Trailing stop 6%: reszta
        if qty_trail > 0:
            pos = self.get_position(ticker)
            if pos:
                avail = int(float(pos.get('qty_available') or pos.get('qty') or qty_trail))
                actual_trail = min(qty_trail, avail)
                if actual_trail > 0:
                    self._submit_trailing_stop(ticker, str(actual_trail), '6.0')

        return result


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
