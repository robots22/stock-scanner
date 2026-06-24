#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 14: POLYGON WEBSOCKET
Zapisz jako polygon_ws.py w folderze stock-scanner

Zadanie:
  Subskrybuje Polygon/Massive WebSocket AM feed (agregaty per minutę)
  dla universe tickerów small-cap $0.01-$15.
  Wykrywa volume spikes w czasie rzeczywistym i triggeruje
  natychmiastową analizę Claude — bez czekania na 5-minutowy cykl REST.

  UWAGA: Starter plan ma 15-minutowe opóźnienie danych.
  Ale WebSocket stream jest ciągły — każda minuta przychodzi automatycznie
  zamiast polling co 5 minut. Edge: wykrywamy spike 4 minuty wcześniej.

Feeds:
  AM.{ticker} — agregaty per minutę (OHLC + volume)
  
Historia zmian:
  v1.0 — pierwsza wersja
         - subskrypcja AM feed dla universe $0.01-$15
         - volume spike detection (2x poprzednia minuta)
         - fast_track queue → main.py
         - auto-reconnect przy zerwaniu połączenia
         - odświeżanie subskrypcji co 5 minut
"""

import json
import os
import threading
import time
from collections import defaultdict, deque

import websocket

from config import logger, CONFIG, POLYGON_API_KEY, now_chicago

# ==================== KONFIGURACJA ====================

WS_URL = "wss://socket.polygon.io/stocks"

# Próg volume spike — ile razy większy niż średnia poprzednich minut
VOLUME_SPIKE_MULTIPLIER = float(os.getenv('WS_VOLUME_SPIKE', '2.0'))

# Ile poprzednich minut trzymamy do obliczenia średniej
VOLUME_HISTORY_BARS = 4

# Minimalna wartość volume żeby w ogóle rozważać spike
MIN_VOLUME_FOR_SPIKE = CONFIG.get('min_volume', 100_000) / 10  # 10k

# Jak często odświeżamy subskrypcję universe (sekundy)
RESUBSCRIBE_INTERVAL = 300  # 5 minut


# ==================== GŁÓWNA KLASA ====================

class PolygonWebSocket:
    """
    WebSocket klient dla Polygon/Massive AM feed.
    
    Wykrywa volume spikes i dodaje tickery do fast_track_queue
    który main.py sprawdza co 30 sekund.
    
    Architektura:
    - Osobny wątek dla WebSocket connection
    - Thread-safe kolejka fast_track_queue
    - Buffer historii volume per ticker
    """

    def __init__(self, fast_track_queue, fast_track_lock):
        """
        fast_track_queue — lista do której dodajemy tickery ze spikami
        fast_track_lock  — threading.Lock() dla thread-safety
        """
        self.fast_track_queue = fast_track_queue
        self.fast_track_lock  = fast_track_lock

        # Historia volume per ticker (ostatnie N minut)
        self.volume_history = defaultdict(lambda: deque(maxlen=VOLUME_HISTORY_BARS))

        # Cache cen per ticker (z ostatniego bara)
        self.price_cache = {}

        # Aktualny universe tickerów (aktualizowany co 5 minut)
        self.universe_tickers = set()
        self.universe_lock    = threading.Lock()

        # Stan połączenia
        self.ws       = None
        self.running  = False
        self.connected = False
        self._thread  = None

        # Statystyki
        self.bars_received  = 0
        self.spikes_detected = 0
        self.last_bar_time   = None

        logger.info("PolygonWebSocket zainicjowany")

    # ==================== UNIVERSE ====================

    def update_universe(self, tickers):
        """
        Aktualizuje listę tickerów do subskrypcji.
        Wywoływane przez main.py co 5 minut po pobraniu universe z REST.
        """
        new_set = set(tickers)
        with self.universe_lock:
            old_set         = self.universe_tickers
            self.universe_tickers = new_set

        added   = new_set - old_set
        removed = old_set - new_set

        if added or removed:
            logger.info(f"WS universe: +{len(added)} -{len(removed)} tickerów "
                        f"(łącznie {len(new_set)})")
            if self.connected:
                self._resubscribe()

    def _get_subscriptions(self):
        """Zwraca listę subskrypcji AM dla aktualnego universe."""
        with self.universe_lock:
            tickers = list(self.universe_tickers)

        if not tickers:
            return []

        return [f"AM.{t}" for t in tickers]

    # ==================== WEBSOCKET CALLBACKS ====================

    def _on_open(self, ws):
        logger.info("WS połączono z Polygon")
        self.connected = True

    def _on_message(self, ws, message):
        """Przetwarza wiadomości z WebSocket."""
        try:
            data = json.loads(message)
            if not isinstance(data, list):
                data = [data]

            for event in data:
                ev = event.get('ev', '')

                if ev == 'connected':
                    logger.info("WS: connected — autoryzuję...")
                    self._authenticate()

                elif ev == 'auth_success':
                    logger.info("WS: autoryzacja OK — subskrybuję...")
                    self._resubscribe()

                elif ev == 'auth_failed':
                    logger.error("WS: autoryzacja FAILED — sprawdź POLYGON_API_KEY")

                elif ev == 'AM':
                    self._process_bar(event)

                elif ev == 'subscription_updated':
                    subs = event.get('subscriptions', [])
                    logger.info(f"WS: subskrypcja zaktualizowana — {len(subs)} feedów")

        except Exception as e:
            logger.error(f"WS message error: {e}")

    def _on_error(self, ws, error):
        logger.error(f"WS błąd: {error}")
        self.connected = False

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning(f"WS rozłączono: {close_status_code} {close_msg}")
        self.connected = False

        if self.running:
            logger.info("WS: próba reconnect za 5s...")
            time.sleep(5)
            self._connect()

    # ==================== AUTH & SUBSCRIBE ====================

    def _authenticate(self):
        """Wysyła klucz API do autoryzacji."""
        if self.ws:
            self.ws.send(json.dumps({
                'action': 'auth',
                'params': POLYGON_API_KEY,
            }))

    def _resubscribe(self):
        """Subskrybuje AM feed dla aktualnego universe."""
        subs = self._get_subscriptions()
        if not subs or not self.ws:
            return

        # Polygon przyjmuje max 1000 subskrypcji per wiadomość
        chunk_size = 1000
        for i in range(0, len(subs), chunk_size):
            chunk = subs[i:i + chunk_size]
            self.ws.send(json.dumps({
                'action': 'subscribe',
                'params': ','.join(chunk),
            }))
            logger.info(f"WS: subskrybuję {len(chunk)} tickerów "
                        f"({i+1}-{min(i+chunk_size, len(subs))} z {len(subs)})")

    # ==================== VOLUME SPIKE DETECTION ====================

    def _process_bar(self, event):
        """
        Przetwarza agregat minutowy (AM bar).
        Wykrywa volume spike i dodaje ticker do fast_track_queue.
        
        Pola AM bara:
          sym — ticker symbol
          o   — open
          h   — high
          l   — low
          c   — close
          v   — volume
          vw  — VWAP
          s   — start timestamp (ms)
          e   — end timestamp (ms)
        """
        ticker = event.get('sym', '')
        volume = event.get('v', 0)
        price  = event.get('c', 0)  # close ceny za minutę
        vwap   = event.get('vw', 0)
        high   = event.get('h', 0)
        low    = event.get('l', 0)

        if not ticker or not volume or not price:
            return

        self.bars_received += 1
        self.last_bar_time  = now_chicago()

        # Zaktualizuj cache ceny
        self.price_cache[ticker] = {
            'price': price,
            'vwap':  vwap,
            'high':  high,
            'low':   low,
        }

        # Pobierz historię volume
        history = self.volume_history[ticker]

        # Sprawdź spike (potrzebujemy min 2 poprzednich barów)
        if len(history) >= 2 and volume >= MIN_VOLUME_FOR_SPIKE:
            avg_volume = sum(history) / len(history)

            if avg_volume > 0:
                spike_ratio = volume / avg_volume

                if spike_ratio >= VOLUME_SPIKE_MULTIPLIER:
                    self._on_spike(ticker, volume, avg_volume, spike_ratio, price, vwap)

        # Dodaj obecny bar do historii
        history.append(volume)

    def _on_spike(self, ticker, volume, avg_volume, ratio, price, vwap):
        """
        Obsługuje wykryty volume spike.
        Dodaje ticker do fast_track_queue dla natychmiastowej analizy.
        """
        self.spikes_detected += 1

        logger.info(f"WS SPIKE: {ticker} | vol {volume:,} ({ratio:.1f}x avg) | "
                    f"${price:.2f} | VWAP ${vwap:.2f}")

        # Dodaj do fast_track_queue (thread-safe)
        with self.fast_track_lock:
            # Unikaj duplikatów — nie dodawaj jeśli już jest w kolejce
            existing = [t['ticker'] for t in self.fast_track_queue]
            if ticker not in existing:
                self.fast_track_queue.append({
                    'ticker':       ticker,
                    'reason':       f"WS volume spike {ratio:.1f}x avg w 1 minucie",
                    'volume':       volume,
                    'avg_volume':   avg_volume,
                    'spike_ratio':  ratio,
                    'price':        price,
                    'vwap':         vwap,
                    'timestamp':    now_chicago().isoformat(),
                })

    # ==================== CONNECTION MANAGEMENT ====================

    def _connect(self):
        """Nawiązuje połączenie WebSocket."""
        try:
            self.ws = websocket.WebSocketApp(
                WS_URL,
                on_open    = self._on_open,
                on_message = self._on_message,
                on_error   = self._on_error,
                on_close   = self._on_close,
            )
            self.ws.run_forever(
                ping_interval=30,
                ping_timeout=10,
            )
        except Exception as e:
            logger.error(f"WS connect error: {e}")
            self.connected = False

    def start(self):
        """Uruchamia WebSocket w osobnym wątku."""
        self.running = True
        self._thread = threading.Thread(
            target=self._connect,
            daemon=True,
            name="PolygonWS"
        )
        self._thread.start()
        logger.info("PolygonWebSocket uruchomiony")

    def stop(self):
        """Zatrzymuje WebSocket."""
        self.running = False
        if self.ws:
            self.ws.close()
        logger.info("PolygonWebSocket zatrzymany")

    def get_stats(self):
        return {
            'connected':      self.connected,
            'bars_received':  self.bars_received,
            'spikes_detected': self.spikes_detected,
            'universe_size':  len(self.universe_tickers),
            'last_bar':       self.last_bar_time.strftime('%H:%M:%S') 
                              if self.last_bar_time else '—',
        }


# ==================== TEST ====================

if __name__ == "__main__":
    import signal

    print("\n" + "="*55)
    print("  TEST: Polygon WebSocket AM feed")
    print("="*55)

    if not POLYGON_API_KEY:
        print("❌ Brak POLYGON_API_KEY w .env")
        exit(1)

    # Test kolejka
    test_queue = []
    test_lock  = threading.Lock()

    ws = PolygonWebSocket(test_queue, test_lock)

    # Subskrybuj kilka tickerów testowych
    ws.update_universe(['SOUN', 'GALT', 'AMC', 'MARA', 'RIOT',
                        'ASTS', 'LCID', 'NIO', 'PLUG', 'IONQ'])

    ws.start()

    print("\nNasłuchuję na AM bary... (Ctrl+C aby zatrzymać)\n")

    def handle_exit(sig, frame):
        print("\n\nZatrzymuję...")
        ws.stop()
        stats = ws.get_stats()
        print(f"\nStatystyki:")
        print(f"  Bary otrzymane:   {stats['bars_received']}")
        print(f"  Spiki wykryte:    {stats['spikes_detected']}")
        print(f"  Ostatni bar:      {stats['last_bar']}")
        print(f"\nFast track queue: {len(test_queue)} tickerów")
        for t in test_queue:
            print(f"  {t['ticker']}: {t['reason']}")
        exit(0)

    signal.signal(signal.SIGINT, handle_exit)

    # Wyświetlaj statystyki co 30 sekund
    while True:
        time.sleep(30)
        stats = ws.get_stats()
        print(f"[{now_chicago().strftime('%H:%M:%S')}] "
              f"Bary: {stats['bars_received']} | "
              f"Spiki: {stats['spikes_detected']} | "
              f"Queue: {len(test_queue)}")
