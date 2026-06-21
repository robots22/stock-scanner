#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 13: TELEGRAM BOT HANDLER
Zapisz jako telegram_bot.py w folderze stock-scanner

Zadanie:
- Nasłuchuje komend od użytkownika na Telegram
- /analyze TICKER → dodaje ticker do kolejki manualnej analizy
- /status        → pokazuje aktualny status systemu
- /top           → pokazuje TOP 5 ostatnich sygnałów
- /help          → lista komend

Działa w osobnym wątku obok głównej pętli main.py.

Historia zmian:
    v1.0 — pierwsza wersja, komendy /analyze /status /top /help
"""

import requests
import threading
import time
from config import (logger, CONFIG, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                    TELEGRAM_CHAT_IDS, now_chicago, get_market_status)


# ==================== KONFIGURACJA ====================

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
POLL_INTERVAL = 2  # sekundy między sprawdzaniem nowych wiadomości


# ==================== WYSYŁANIE ====================

def send(text, parse_mode='HTML'):
    """Wysyła wiadomość na wszystkie skonfigurowane Chat ID"""
    if not CONFIG['telegram_enabled']:
        print(f"\n📱 TELEGRAM BOT: {text[:100]}")
        return True

    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            requests.post(
                f"{TELEGRAM_API}/sendMessage",
                data={
                    'chat_id':    chat_id,
                    'text':       text[:4096],
                    'parse_mode': parse_mode,
                },
                timeout=10,
            )
        except Exception as e:
            logger.error(f"Telegram bot send error {chat_id}: {e}")
    return True


# ==================== KOMENDY ====================

def handle_analyze(ticker, scanner):
    """Obsługuje komendę /analyze TICKER"""
    ticker = ticker.upper().strip()

    if not ticker or not ticker.isalpha():
        send(f"❌ Nieprawidłowy ticker: <b>{ticker}</b>\n"
             f"Przykład: /analyze SOUN")
        return

    if len(ticker) > 6:
        send(f"❌ Ticker za długi: <b>{ticker}</b>")
        return

    added = scanner.queue_manual_analysis(ticker)

    if added:
        send(f"🔍 <b>{ticker}</b> dodany do analizy\n"
             f"Wynik pojawi się za chwilę...")
    else:
        send(f"⏳ <b>{ticker}</b> już jest w kolejce")


def handle_status(scanner):
    """Obsługuje komendę /status"""
    from database import get_stats, get_active_buy_signals
    stats   = get_stats()
    active  = get_active_buy_signals()
    analyst = scanner.analyst.get_stats()
    now     = now_chicago()

    text = f"""📊 <b>STATUS SYSTEMU</b>
Czas: {now.strftime('%H:%M:%S')} CST
Rynek: {get_market_status()}
Tryb: {'DEMO' if scanner.demo_mode else 'LIVE'}

📈 Sesja:
  Skanów:    {scanner.scan_count}
  Sygnałów:  {stats.get('total_signals', 0)}
  Alertów:   {scanner.alert_count}
  Monit. BUY: {len(active)}

🤖 Claude API:
  Wywołań:   {analyst['total_calls']}
  Koszt:     ${analyst['total_cost_usd']:.4f}
  Manual:    ${scanner.manual_cost_usd:.4f}

📋 Kolejka manual: {len(scanner.manual_queue)}"""

    send(text)


def handle_top(scanner):
    """Obsługuje komendę /top — ostatnie 5 sygnałów"""
    from database import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute('''
            SELECT ticker, verdict, confidence, price, timestamp
            FROM signals
            ORDER BY id DESC
            LIMIT 5
        ''')
        rows = c.fetchall()
    finally:
        conn.close()

    if not rows:
        send("📋 Brak sygnałów w bazie")
        return

    text = "📋 <b>OSTATNIE 5 SYGNAŁÓW</b>\n\n"
    for row in rows:
        ticker, verdict, confidence, price, ts = row
        icon = '🟢' if verdict == 'BUY' else '🟡' if verdict == 'WATCH' else '🔴'
        time_str = ts[11:16] if ts else ''
        text += f"{icon} <b>{ticker}</b> — {verdict} @ ${price:.2f} ({time_str})\n"

    send(text)


def handle_help():
    """Obsługuje komendę /help"""
    text = """🤖 <b>STOCK SCANNER — KOMENDY</b>

/analyze TICKER — analizuj ticker natychmiast
  Przykład: /analyze SOUN

/status — status systemu i koszty API

/top — ostatnie 5 sygnałów

/help — ta wiadomość

💡 Sygnały BUY/WATCH z wysoką pewnością
są wysyłane automatycznie co 5 minut."""
    send(text)


# ==================== GŁÓWNA KLASA BOTA ====================

class TelegramBot:
    """
    Telegram bot nasłuchujący komend użytkownika.
    Działa w osobnym wątku — nie blokuje głównej pętli.
    """

    def __init__(self, scanner):
        self.scanner    = scanner
        self.running    = False
        self.last_update_id = 0
        self._thread    = None

        # Pobierz ostatnie update_id żeby nie procesować starych wiadomości
        self._init_offset()

        logger.info("TelegramBot zainicjowany")

    def _init_offset(self):
        """Pobiera ostatni update_id żeby ignorować stare wiadomości"""
        try:
            response = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={'limit': 1, 'offset': -1},
                timeout=10,
            )
            data = response.json()
            results = data.get('result', [])
            if results:
                self.last_update_id = results[-1]['update_id']
                logger.info(f"TelegramBot: offset ustawiony na {self.last_update_id}")
        except Exception as e:
            logger.warning(f"TelegramBot init offset error: {e}")

    def start(self):
        """Uruchamia bota w osobnym wątku"""
        self.running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="TelegramBot"
        )
        self._thread.start()
        logger.info("TelegramBot uruchomiony")

    def stop(self):
        """Zatrzymuje bota"""
        self.running = False
        logger.info("TelegramBot zatrzymany")

    def _poll_loop(self):
        """Główna pętla nasłuchiwania — long polling"""
        while self.running:
            try:
                self._check_updates()
                time.sleep(POLL_INTERVAL)
            except Exception as e:
                logger.error(f"TelegramBot poll error: {e}")
                time.sleep(5)

    def _check_updates(self):
        """Sprawdza nowe wiadomości i przetwarza komendy"""
        if not CONFIG['telegram_enabled']:
            return

        try:
            response = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={
                    'offset':  self.last_update_id + 1,
                    'timeout': 1,
                    'limit':   10,
                },
                timeout=10,
            )
            data = response.json()

            if not data.get('ok'):
                return

            for update in data.get('result', []):
                self.last_update_id = update['update_id']
                self._process_update(update)

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            logger.warning(f"TelegramBot check updates error: {e}")

    def _process_update(self, update):
        """Przetwarza jeden update (wiadomość) od użytkownika"""
        message = update.get('message', {})
        if not message:
            return

        # Sprawdź czy wiadomość pochodzi od autoryzowanego użytkownika
        chat_id      = str(message.get('chat', {}).get('id', ''))
        allowed_ids  = [str(cid) for cid in TELEGRAM_CHAT_IDS]
        if chat_id not in allowed_ids:
            logger.warning(f"TelegramBot: nieautoryzowany chat_id: {chat_id}")
            return

        text = message.get('text', '').strip()
        if not text:
            return

        logger.info(f"TelegramBot: otrzymano komendę: {text}")

        # Parsuj komendę
        parts = text.split()
        cmd   = parts[0].lower()

        if cmd in ('/analyze', '/a'):
            if len(parts) < 2:
                send("❌ Podaj ticker: /analyze SOUN")
            else:
                handle_analyze(parts[1], self.scanner)

        elif cmd == '/status':
            handle_status(self.scanner)

        elif cmd == '/top':
            handle_top(self.scanner)

        elif cmd in ('/help', '/start'):
            handle_help()

        else:
            send(f"❓ Nieznana komenda: <b>{text}</b>\n"
                 f"Wpisz /help aby zobaczyć dostępne komendy.")


# ==================== TEST ====================

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  TEST: Telegram Bot")
    print("="*55)

    if not CONFIG['telegram_enabled']:
        print("\n⚠️  Telegram nie skonfigurowany w .env")
        print("Dodaj TELEGRAM_BOT_TOKEN i TELEGRAM_CHAT_ID")
        exit(0)

    # Test wysyłania
    print("\n✅ Test wysyłania wiadomości:")
    send("🤖 <b>Stock Scanner Bot</b> — test połączenia ✅")
    print("  Wiadomość wysłana — sprawdź Telegram")

    # Test getUpdates
    print("\n✅ Test getUpdates:")
    try:
        response = requests.get(
            f"{TELEGRAM_API}/getUpdates",
            params={'limit': 3},
            timeout=10,
        )
        data = response.json()
        if data.get('ok'):
            print(f"  OK — {len(data.get('result', []))} wiadomości")
        else:
            print(f"  ERROR: {data}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print("\n" + "="*55)
    print("  Plik 13 gotowy ✅")
    print("="*55)
