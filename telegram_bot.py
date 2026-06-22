#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 13: TELEGRAM BOT HANDLER (v2.0)
Zapisz jako telegram_bot.py w folderze stock-scanner

Komendy:
    /analyze TICKER  — ręczna analiza tickera
    /status          — stan systemu
    /top             — TOP 5 ostatnich sygnałów
    /help            — lista komend
    /stats           — win rate z bazy, ostatnie 24h
    /performance     — trafność BUY/WATCH/AVOID
    /cost            — koszt Claude API dziś/w tygodniu
    /pause           — zatrzymaj skanowanie
    /resume          — wznów skanowanie
    /blacklist TICKER — dodaj ticker do czarnej listy (do końca sesji)
    /remind Xd TEKST — ustaw reminder za X dni (zapisany w bazie)
    /backtest        — analiza sygnałów z bazy bez SSH
    /report          — raport tygodniowy jako tekst na Telegram
"""

import time
import threading
import requests
import sqlite3
from datetime import datetime, timedelta

from config import (
    CLAUDE_CONFIG,
    logger, CONFIG, now_chicago,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS
)


# ==================== GLOBALNE STANY ====================

# Kolejka manualnych analiz (współdzielona z main.py)
manual_queue = []
manual_queue_lock = threading.Lock()

# Blacklista na czas sesji (reset po restarcie)
session_blacklist = set()
session_blacklist_lock = threading.Lock()

# Flaga pauzy (współdzielona z main.py)
system_paused = False
system_paused_lock = threading.Lock()

# Referencja do stanu systemu (ustawiana przez main.py)
system_state = {
    'scan_count': 0,
    'last_scan': None,
    'active_signals': [],
    'start_time': time.time(),
    'daily_cost': 0.0,
    'weekly_cost': 0.0,
}


# ==================== POMOCNICZE ====================

def send_message(text, chat_id=None):
    """Wysyła wiadomość na Telegram — na wszystkie skonfigurowane chaty."""
    targets = [chat_id] if chat_id else TELEGRAM_CHAT_IDS
    for cid in targets:
        if not cid:
            continue
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={
                    'chat_id': cid,
                    'text': text[:4000],
                    'parse_mode': 'HTML'
                },
                timeout=5
            )
        except Exception as e:
            logger.error(f"Telegram send error (chat {cid}): {e}")


def is_authorized(chat_id):
    """Sprawdza czy nadawca jest na liście autoryzowanych chatów."""
    return str(chat_id) in [str(cid) for cid in TELEGRAM_CHAT_IDS if cid]


def get_db_connection():
    """Zwraca połączenie z bazą danych."""
    try:
        conn = sqlite3.connect(CONFIG.get('db_path', 'scanner.db'))
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.error(f"DB connection error: {e}")
        return None


# ==================== KOMENDY ====================

def cmd_help():
    return (
        "📋 <b>STOCK SCANNER — KOMENDY</b>\n\n"
        "📊 <b>Analiza:</b>\n"
        "  /analyze TICKER — ręczna analiza\n"
        "  /top            — TOP 5 sygnałów\n"
        "  /backtest       — analiza wyników z bazy\n\n"
        "📈 <b>Statystyki:</b>\n"
        "  /stats          — win rate ostatnie 24h\n"
        "  /performance    — trafność BUY/WATCH/AVOID\n"
        "  /cost           — koszt Claude API\n"
        "  /report         — raport tygodniowy\n\n"
        "⚙️ <b>Kontrola:</b>\n"
        "  /status         — stan systemu\n"
        "  /pause          — zatrzymaj skanowanie\n"
        "  /resume         — wznów skanowanie\n"
        "  /blacklist TICK — pomiń ticker (do restartu)\n\n"
        "⏰ <b>Reminder:</b>\n"
        "  /remind 7d Tekst przypomnienia\n"
        "  /reminders      — lista aktywnych reminderów\n"
    )


def cmd_status():
    global system_paused, system_state
    uptime_sec = int(time.time() - system_state.get('start_time', time.time()))
    uptime_min = uptime_sec // 60
    uptime_h   = uptime_min // 60
    uptime_str = f"{uptime_h}h {uptime_min % 60}m"

    paused_str = "⏸ ZATRZYMANY" if system_paused else "✅ DZIAŁA"
    last_scan  = system_state.get('last_scan')
    last_str   = last_scan if last_scan else "—"

    with session_blacklist_lock:
        bl_count = len(session_blacklist)
        bl_str   = ", ".join(session_blacklist) if session_blacklist else "brak"

    return (
        f"⚙️ <b>STATUS SYSTEMU</b>\n\n"
        f"Stan:       {paused_str}\n"
        f"Uptime:     {uptime_str}\n"
        f"Skanów:     {system_state.get('scan_count', 0)}\n"
        f"Ostatni:    {last_str}\n"
        f"Blacklist:  {bl_str} ({bl_count})\n"
        f"Koszt dziś: ${system_state.get('daily_cost', 0.0):.4f}\n"
    )


def cmd_top():
    conn = get_db_connection()
    if not conn:
        return "❌ Błąd połączenia z bazą danych."
    try:
        c = conn.cursor()
        c.execute('''
            SELECT ticker, verdict, price, confidence, timestamp
            FROM signals
            ORDER BY timestamp DESC
            LIMIT 5
        ''')
        rows = c.fetchall()
        if not rows:
            return "📭 Brak sygnałów w bazie."

        lines = ["📈 <b>TOP 5 OSTATNICH SYGNAŁÓW</b>\n"]
        for row in rows:
            icon = '🟢' if row['verdict'] == 'BUY' else (
                   '🟡' if row['verdict'] == 'WATCH' else '🔴')
            ts   = row['timestamp'][:16] if row['timestamp'] else '—'
            lines.append(
                f"{icon} <b>{row['ticker']}</b> @ ${row['price']:.2f} "
                f"| {row['verdict']} | {ts}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Błąd bazy: {e}"
    finally:
        conn.close()


def cmd_stats():
    """Win rate z ostatnich 24h."""
    conn = get_db_connection()
    if not conn:
        return "❌ Błąd połączenia z bazą danych."
    try:
        c   = conn.cursor()
        ago = (datetime.now() - timedelta(hours=24)).isoformat()
        c.execute('''
            SELECT verdict,
                   COUNT(*) as cnt,
                   AVG(outcome_1h)  as avg_1h,
                   AVG(outcome_4h)  as avg_4h,
                   AVG(outcome_24h) as avg_24h,
                   SUM(CASE WHEN outcome_1h > 0 THEN 1 ELSE 0 END) as wins_1h
            FROM signals
            WHERE timestamp > ? AND outcome_1h IS NOT NULL
            GROUP BY verdict
        ''', (ago,))
        rows = c.fetchall()

        if not rows:
            return "📭 Brak sygnałów z wynikami z ostatnich 24h."

        lines = ["📊 <b>STATYSTYKI — ostatnie 24h</b>\n"]
        for row in rows:
            icon    = '🟢' if row['verdict'] == 'BUY' else (
                      '🟡' if row['verdict'] == 'WATCH' else '🔴')
            wr      = round(row['wins_1h'] / row['cnt'] * 100, 1) if row['cnt'] else 0
            avg_1h  = row['avg_1h']  or 0
            avg_24h = row['avg_24h'] or 0
            lines.append(
                f"{icon} <b>{row['verdict']}</b> ({row['cnt']} sygn.)\n"
                f"   Win rate 1h: {wr}%\n"
                f"   Avg 1h: {avg_1h:+.2f}% | Avg 24h: {avg_24h:+.2f}%"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Błąd bazy: {e}"
    finally:
        conn.close()


def cmd_performance():
    """Trafność BUY/WATCH/AVOID — całkowita historia."""
    conn = get_db_connection()
    if not conn:
        return "❌ Błąd połączenia z bazą danych."
    try:
        c = conn.cursor()
        c.execute('''
            SELECT verdict,
                   COUNT(*) as cnt,
                   AVG(outcome_1h)  as avg_1h,
                   AVG(outcome_4h)  as avg_4h,
                   AVG(outcome_24h) as avg_24h,
                   SUM(CASE WHEN outcome_1h > 0 THEN 1 ELSE 0 END) as wins_1h,
                   MAX(outcome_1h)  as best,
                   MIN(outcome_1h)  as worst
            FROM signals
            WHERE outcome_1h IS NOT NULL
            GROUP BY verdict
        ''')
        rows = c.fetchall()

        if not rows:
            return (
                "📭 Brak sygnałów z wynikami.\n"
                "Wyniki pojawiają się automatycznie po 1h/4h/24h od sygnału."
            )

        lines = ["📈 <b>PERFORMANCE — cała historia</b>\n"]
        for row in rows:
            icon = '🟢' if row['verdict'] == 'BUY' else (
                   '🟡' if row['verdict'] == 'WATCH' else '🔴')
            wr   = round(row['wins_1h'] / row['cnt'] * 100, 1) if row['cnt'] else 0
            lines.append(
                f"{icon} <b>{row['verdict']}</b> — {row['cnt']} sygnałów\n"
                f"   Win rate 1h:  {wr}%\n"
                f"   Avg 1h:  {(row['avg_1h'] or 0):+.2f}%\n"
                f"   Avg 4h:  {(row['avg_4h'] or 0):+.2f}%\n"
                f"   Avg 24h: {(row['avg_24h'] or 0):+.2f}%\n"
                f"   Best/Worst 1h: {(row['best'] or 0):+.2f}% / "
                f"{(row['worst'] or 0):+.2f}%"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Błąd bazy: {e}"
    finally:
        conn.close()


def cmd_cost():
    """Koszt Claude API dziś i w tym tygodniu."""
    daily  = system_state.get('daily_cost',  0.0)
    weekly = system_state.get('weekly_cost', 0.0)
    budget_daily = CLAUDE_CONFIG.get('daily_budget_usd', 1.14)

    used_pct = round(daily / budget_daily * 100, 1) if budget_daily else 0
    remaining = max(0.0, budget_daily - daily)

    return (
        f"💰 <b>KOSZT CLAUDE API</b>\n\n"
        f"Dziś:       ${daily:.4f} / ${budget_daily:.2f} ({used_pct}%)\n"
        f"Pozostało:  ${remaining:.4f}\n"
        f"W tygodniu: ${weekly:.4f}\n"
        f"Limit mies: ${CONFIG.get('claude_monthly_budget', 25.0):.2f}"
    )


def cmd_pause():
    global system_paused
    with system_paused_lock:
        system_paused = True
    return "⏸ <b>Skanowanie zatrzymane.</b>\nUżyj /resume aby wznowić."


def cmd_resume():
    global system_paused
    with system_paused_lock:
        system_paused = False
    return "▶️ <b>Skanowanie wznowione.</b>"


def cmd_blacklist(ticker):
    if not ticker:
        return "❌ Podaj ticker: /blacklist SOUN"
    ticker = ticker.upper().strip()
    with session_blacklist_lock:
        session_blacklist.add(ticker)
    return (
        f"🚫 <b>{ticker}</b> dodany do blacklisty.\n"
        f"Zostanie pominięty do końca sesji (do restartu systemu)."
    )


def cmd_remind(args_str):
    """
    /remind 7d Uruchom backtest opcja 2
    /remind 21d Sprawdź wyniki systemu
    """
    if not args_str:
        return "❌ Użycie: /remind 7d Treść przypomnienia"

    parts = args_str.strip().split(' ', 1)
    if len(parts) < 2:
        return "❌ Użycie: /remind 7d Treść przypomnienia"

    duration_str = parts[0].lower()
    text         = parts[1].strip()

    # Parsuj czas: 1d, 7d, 21d
    try:
        if duration_str.endswith('d'):
            days = int(duration_str[:-1])
        elif duration_str.endswith('h'):
            days = int(duration_str[:-1]) / 24
        else:
            return "❌ Format czasu: 7d (dni) lub 12h (godziny)"
    except ValueError:
        return "❌ Nieprawidłowy format czasu. Przykład: 7d lub 12h"

    fire_at = (datetime.now() + timedelta(days=days)).isoformat()

    conn = get_db_connection()
    if not conn:
        return "❌ Błąd bazy danych."
    try:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                text      TEXT NOT NULL,
                fire_at   TEXT NOT NULL,
                created   TEXT NOT NULL,
                sent      INTEGER DEFAULT 0
            )
        ''')
        c.execute(
            'INSERT INTO reminders (text, fire_at, created) VALUES (?, ?, ?)',
            (text, fire_at, datetime.now().isoformat())
        )
        conn.commit()
        fire_dt = datetime.fromisoformat(fire_at)
        return (
            f"⏰ <b>Reminder ustawiony!</b>\n\n"
            f"Treść: {text}\n"
            f"Kiedy: {fire_dt.strftime('%d.%m.%Y %H:%M')}"
        )
    except Exception as e:
        return f"❌ Błąd zapisu: {e}"
    finally:
        conn.close()


def cmd_reminders():
    """Lista aktywnych reminderów."""
    conn = get_db_connection()
    if not conn:
        return "❌ Błąd bazy danych."
    try:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                text    TEXT NOT NULL,
                fire_at TEXT NOT NULL,
                created TEXT NOT NULL,
                sent    INTEGER DEFAULT 0
            )
        ''')
        c.execute(
            'SELECT id, text, fire_at FROM reminders WHERE sent = 0 ORDER BY fire_at',
        )
        rows = c.fetchall()
        if not rows:
            return "📭 Brak aktywnych reminderów."

        lines = ["⏰ <b>AKTYWNE REMINDERY</b>\n"]
        for row in rows:
            fire_dt = datetime.fromisoformat(row['fire_at'])
            lines.append(
                f"#{row['id']} — {fire_dt.strftime('%d.%m.%Y %H:%M')}\n"
                f"   {row['text']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Błąd bazy: {e}"
    finally:
        conn.close()


def cmd_backtest():
    """Analiza sygnałów z bazy — odpowiednik backtest.py opcja 2."""
    conn = get_db_connection()
    if not conn:
        return "❌ Błąd połączenia z bazą danych."
    try:
        c = conn.cursor()
        c.execute('''
            SELECT verdict,
                   COUNT(*) as cnt,
                   AVG(outcome_1h)  as avg_1h,
                   AVG(outcome_4h)  as avg_4h,
                   AVG(outcome_24h) as avg_24h,
                   SUM(CASE WHEN outcome_1h > 0 THEN 1 ELSE 0 END) as wins_1h
            FROM signals
            WHERE outcome_1h IS NOT NULL
            GROUP BY verdict
        ''')
        rows = c.fetchall()

        if not rows:
            return (
                "📭 Brak sygnałów z wynikami.\n"
                "Uruchom system i poczekaj min. 1h na pierwsze wyniki."
            )

        lines = ["🔬 <b>BACKTEST — analiza bazy</b>\n"]
        for row in rows:
            icon = '🟢' if row['verdict'] == 'BUY' else (
                   '🟡' if row['verdict'] == 'WATCH' else '🔴')
            wr   = round(row['wins_1h'] / row['cnt'] * 100, 1) if row['cnt'] else 0
            lines.append(
                f"{icon} <b>{row['verdict']}</b> ({row['cnt']} sygn.) "
                f"— win rate 1h: {wr}%\n"
                f"   Avg: 1h {(row['avg_1h'] or 0):+.2f}% | "
                f"4h {(row['avg_4h'] or 0):+.2f}% | "
                f"24h {(row['avg_24h'] or 0):+.2f}%"
            )

        # TOP 3 najlepsze BUY
        c.execute('''
            SELECT ticker, price, outcome_1h, timestamp
            FROM signals
            WHERE verdict = 'BUY' AND outcome_1h IS NOT NULL
            ORDER BY outcome_1h DESC
            LIMIT 3
        ''')
        best = c.fetchall()
        if best:
            lines.append("\n🏆 <b>Top 3 BUY (wynik 1h):</b>")
            for row in best:
                ts = row['timestamp'][:10] if row['timestamp'] else '—'
                lines.append(
                    f"  {row['ticker']} @ ${row['price']:.2f} "
                    f"→ {row['outcome_1h']:+.2f}% ({ts})"
                )

        return "\n".join(lines)
    except Exception as e:
        return f"❌ Błąd bazy: {e}"
    finally:
        conn.close()


def cmd_report():
    """Raport tygodniowy jako tekst na Telegram."""
    conn = get_db_connection()
    if not conn:
        return "❌ Błąd połączenia z bazą danych."
    try:
        c   = conn.cursor()
        ago = (datetime.now() - timedelta(days=7)).isoformat()

        # Ogólne statystyki tygodnia
        c.execute('''
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN verdict = 'BUY'   THEN 1 ELSE 0 END) as buys,
                   SUM(CASE WHEN verdict = 'WATCH' THEN 1 ELSE 0 END) as watches,
                   SUM(CASE WHEN verdict = 'AVOID' THEN 1 ELSE 0 END) as avoids
            FROM signals
            WHERE timestamp > ?
        ''', (ago,))
        totals = c.fetchone()

        # Win rate BUY z tego tygodnia
        c.execute('''
            SELECT AVG(outcome_1h)  as avg_1h,
                   AVG(outcome_24h) as avg_24h,
                   SUM(CASE WHEN outcome_1h > 0 THEN 1 ELSE 0 END) as wins,
                   COUNT(*) as cnt
            FROM signals
            WHERE verdict = 'BUY' AND outcome_1h IS NOT NULL AND timestamp > ?
        ''', (ago,))
        buy_stats = c.fetchone()

        # Top 5 tickerów tygodnia
        c.execute('''
            SELECT ticker, COUNT(*) as cnt
            FROM signals
            WHERE timestamp > ?
            GROUP BY ticker
            ORDER BY cnt DESC
            LIMIT 5
        ''', (ago,))
        top_tickers = c.fetchall()

        now_str = now_chicago().strftime('%d.%m.%Y')
        lines   = [f"📋 <b>RAPORT TYGODNIOWY — {now_str}</b>\n"]

        if totals and totals['total']:
            lines.append(
                f"📊 Łącznie sygnałów: {totals['total']}\n"
                f"   🟢 BUY:   {totals['buys']}\n"
                f"   🟡 WATCH: {totals['watches']}\n"
                f"   🔴 AVOID: {totals['avoids']}"
            )
        else:
            lines.append("📭 Brak sygnałów z ostatnich 7 dni.")

        if buy_stats and buy_stats['cnt']:
            wr = round(buy_stats['wins'] / buy_stats['cnt'] * 100, 1)
            lines.append(
                f"\n📈 <b>BUY performance:</b>\n"
                f"   Win rate 1h:  {wr}%\n"
                f"   Avg 1h:  {(buy_stats['avg_1h'] or 0):+.2f}%\n"
                f"   Avg 24h: {(buy_stats['avg_24h'] or 0):+.2f}%"
            )

        if top_tickers:
            lines.append("\n🔥 <b>Najaktywniejsze tickery:</b>")
            for row in top_tickers:
                lines.append(f"   {row['ticker']}: {row['cnt']} sygn.")

        daily  = system_state.get('daily_cost',  0.0)
        weekly = system_state.get('weekly_cost', 0.0)
        lines.append(f"\n💰 Koszt API w tygodniu: ${weekly:.4f}")

        return "\n".join(lines)
    except Exception as e:
        return f"❌ Błąd bazy: {e}"
    finally:
        conn.close()


def cmd_analyze(ticker):
    if not ticker:
        return "❌ Podaj ticker: /analyze SOUN"
    ticker = ticker.upper().strip()
    with session_blacklist_lock:
        if ticker in session_blacklist:
            return f"🚫 {ticker} jest na blackliście tej sesji."
    with manual_queue_lock:
        if ticker not in manual_queue:
            manual_queue.append(ticker)
            return (
                f"✅ <b>{ticker}</b> dodany do kolejki analizy.\n"
                f"Wynik pojawi się za chwilę."
            )
        else:
            return f"⏳ <b>{ticker}</b> już jest w kolejce."


# ==================== REMINDER CHECKER ====================

def check_reminders():
    """Sprawdza i wysyła zaległe remindery. Uruchamiany co minutę."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        c   = conn.cursor()
        now = datetime.now().isoformat()
        c.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                text    TEXT NOT NULL,
                fire_at TEXT NOT NULL,
                created TEXT NOT NULL,
                sent    INTEGER DEFAULT 0
            )
        ''')
        c.execute(
            'SELECT id, text FROM reminders WHERE sent = 0 AND fire_at <= ?',
            (now,)
        )
        due = c.fetchall()
        for row in due:
            send_message(f"⏰ <b>REMINDER</b>\n\n{row['text']}")
            c.execute('UPDATE reminders SET sent = 1 WHERE id = ?', (row['id'],))
        conn.commit()
    except Exception as e:
        logger.error(f"Reminder check error: {e}")
    finally:
        conn.close()


# ==================== GŁÓWNA PĘTLA BOTA ====================

def run_bot():
    """Główna pętla bota — polling Telegram API."""
    logger.info("Telegram bot uruchomiony.")
    offset    = 0
    last_remind_check = 0

    while True:
        try:
            # Sprawdź remindery co minutę
            if time.time() - last_remind_check > 60:
                check_reminders()
                last_remind_check = time.time()

            # Pobierz nowe wiadomości
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={'offset': offset, 'timeout': 30},
                timeout=35
            )
            if not resp.ok:
                time.sleep(5)
                continue

            updates = resp.json().get('result', [])

            for update in updates:
                offset = update['update_id'] + 1
                msg    = update.get('message', {})
                if not msg:
                    continue

                chat_id = msg.get('chat', {}).get('id')
                text    = msg.get('text', '').strip()

                if not chat_id or not text or not text.startswith('/'):
                    continue

                if not is_authorized(chat_id):
                    send_message(
                        "🔒 Nieautoryzowany dostęp.",
                        chat_id=chat_id
                    )
                    continue

                # Parsuj komendę
                parts   = text.split(' ', 1)
                cmd     = parts[0].lower().split('@')[0]
                args    = parts[1].strip() if len(parts) > 1 else ''

                if cmd == '/help':
                    reply = cmd_help()
                elif cmd == '/status':
                    reply = cmd_status()
                elif cmd == '/top':
                    reply = cmd_top()
                elif cmd == '/stats':
                    reply = cmd_stats()
                elif cmd == '/performance':
                    reply = cmd_performance()
                elif cmd == '/cost':
                    reply = cmd_cost()
                elif cmd == '/pause':
                    reply = cmd_pause()
                elif cmd == '/resume':
                    reply = cmd_resume()
                elif cmd == '/blacklist':
                    reply = cmd_blacklist(args)
                elif cmd == '/remind':
                    reply = cmd_remind(args)
                elif cmd == '/reminders':
                    reply = cmd_reminders()
                elif cmd == '/backtest':
                    reply = cmd_backtest()
                elif cmd == '/report':
                    reply = cmd_report()
                elif cmd == '/analyze':
                    reply = cmd_analyze(args)
                elif cmd == '/start':
                    reply = (
                        "👋 <b>Stock Scanner Bot aktywny!</b>\n"
                        "Wpisz /help aby zobaczyć komendy."
                    )
                else:
                    reply = f"❓ Nieznana komenda: {cmd}\nWpisz /help."

                send_message(reply, chat_id=chat_id)

        except requests.exceptions.Timeout:
            pass  # normalny long-polling timeout
        except Exception as e:
            logger.error(f"Bot loop error: {e}")
            time.sleep(5)


def start_bot_thread():
    """Uruchamia bota w osobnym wątku (wywoływane z main.py)."""
    t = threading.Thread(target=run_bot, daemon=True, name="TelegramBot")
    t.start()
    logger.info("Telegram bot thread started.")
    return t


# ==================== STANDALONE TEST ====================

if __name__ == "__main__":
    print("="*55)
    print("  TELEGRAM BOT v2.0 — test standalone")
    print("="*55)
    print("Bot nasłuchuje... (Ctrl+C aby zatrzymać)")
    print("\nDostępne komendy:")
    print("  /help /status /top /stats /performance")
    print("  /cost /pause /resume /blacklist /remind")
    print("  /reminders /backtest /report /analyze\n")

    # W trybie standalone cost/state są zerowe — to normalne
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\n👋 Bot zatrzymany.")
