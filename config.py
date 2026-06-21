#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 1: KONFIGURACJA
Zapisz jako config.py w folderze stock-scanner

Historia zmian:
    v1.0 — pierwsza wersja
    v1.1 — fix logowania UTF-8 dla Windows (polskie znaki, strzalki)
           - FileHandler z encoding=utf-8
           - StreamHandler z stdout przepisanym na UTF-8
    v1.2 — twardy limit kosztów Claude API
           - monthly_budget_usd = $25.00
           - daily_budget_usd = $25/22 = ~$1.14/dzien
           - cost_per_call_usd = $0.0028
    v1.3 — dodano parametry extended hours i manualnej analizy
           - pre-market: 4:00-8:30 CST, co 15 min, vol > 10k
           - after-market: 15:00-20:00 CST, co 15 min, vol > 10k
           - Claude tylko dla earnings/FDA w extended hours
           - osobny budzet dla manualnej analizy $2/mies
"""

import os
import logging
import sys
from datetime import datetime
import pytz
from dotenv import load_dotenv

# ==================== KLUCZE API ====================
# Wczytaj z pliku .env (nigdy nie wpisuj kluczy bezpośrednio tutaj)
load_dotenv()

POLYGON_API_KEY      = os.getenv('POLYGON_API_KEY', '')
UNUSUAL_WHALES_KEY   = os.getenv('UNUSUAL_WHALES_KEY', '')
ALPACA_API_KEY       = os.getenv('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY    = os.getenv('ALPACA_SECRET_KEY', '')
FINNHUB_API_KEY      = os.getenv('FINNHUB_API_KEY', '')
ANTHROPIC_API_KEY    = os.getenv('ANTHROPIC_API_KEY', '')
TELEGRAM_BOT_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID     = os.getenv('TELEGRAM_CHAT_ID', '')
TELEGRAM_CHAT_ID_2   = os.getenv('TELEGRAM_CHAT_ID_2', '')
TELEGRAM_CHAT_ID_3   = os.getenv('TELEGRAM_CHAT_ID_3', '')

# Lista wszystkich aktywnych Chat ID — dynamiczna, obsługuje 1-3 odbiorców
TELEGRAM_CHAT_IDS = [
    cid for cid in [
        TELEGRAM_CHAT_ID,
        TELEGRAM_CHAT_ID_2,
        TELEGRAM_CHAT_ID_3,
    ]
    if cid
]

# ==================== TRYB SYSTEMU ====================
# DEMO = True  → MockPolygon (bez kluczy API)
# DEMO = False → Prawdziwe dane (wymagane klucze API)
DEMO_MODE = True

SYSTEM_NAME    = "STOCK SCANNER"
SYSTEM_VERSION = "1.0"

# ==================== PARAMETRY SKANOWANIA ====================
CONFIG = {
    # Filtr cenowy
    'min_price': 0.01,
    'max_price': 15.00,

    # Filtr wolumenu
    'min_volume': 100_000,

    # Ile tickerów trafia do Claude AI na jeden cykl
    'max_tickers_for_claude': 5,

    # Cykl główny (Polygon + Alpaca + Finnhub) — co ile sekund
    'main_scan_interval': 300,       # 5 minut

    # Cykl UW (Unusual Whales) — co ile sekund
    'uw_scan_interval': 60,          # 1 minuta

    # Volume ratio — ile razy większy od średniej 30-dniowej
    # żeby ticker był "interesujący"
    'min_volume_ratio': 1.5,

    # Zmiana ceny — minimalny % żeby ticker był "interesujący"
    'min_price_change_pct': 3.0,

    # API timeouts
    'api_timeout': 15,
    'max_retries': 2,

    # Telegram
    'telegram_enabled': bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    'duplicate_alert_cooldown': 600,  # 10 minut między tymi samymi alertami

    # Baza danych
    'db_path': 'scanner.db',

    # Automatyczny wynik sygnału — sprawdź cenę po:
    'outcome_check_hours': [1, 4, 24],

    # Logi
    'log_dir': 'logs',
    'log_file': 'scanner.log',

    # ==================== EXTENDED HOURS ====================
    # Pre-market: 4:00-8:30 CST (5:00-9:30 ET)
    # After-market: 15:00-20:00 CST (16:00-21:00 ET)
    'premarket_enabled':         True,
    'aftermarket_enabled':       True,
    'premarket_scan_interval':   900,    # co 15 minut
    'aftermarket_scan_interval': 900,    # co 15 minut
    'min_volume_extended':       10_000, # niższy próg poza godzinami

    # Claude w extended hours — tylko dla tickerów z katalizatorem
    # (earnings, FDA, insider) — oszczędność kosztów API
    'claude_extended_hours_all':       False,  # False = tylko z katalizatorem
    'claude_extended_hours_catalyst':  True,   # True = analizuj z katalizatorem
}

# ==================== CLAUDE AI ====================
CLAUDE_CONFIG = {
    'model': 'claude-sonnet-4-6',
    'max_tokens': 1000,

    # Ile ostatnich sygnałów dla danego tickera wysyłamy jako kontekst
    'signal_history_count': 5,

    # Twardy limit kosztów Claude API
    # System zatrzymuje wywołania Claude gdy limit dzienny zostanie przekroczony
    'monthly_budget_usd':        25.00,
    'daily_budget_usd':          25.00 / 22,  # ~$1.14/dzień (22 dni handlowe)
    'cost_per_call_usd':         0.0028,       # Sonnet: ~500 in + 200 out tokenów

    # Osobny budżet dla manualnej analizy (Telegram /analyze)
    # Nie wlicza się w dzienny limit automatycznych skanów
    'manual_analysis_budget_usd': 2.00,        # $2/miesiąc osobno

    # System prompt dla Claude — analityk giełdowy
    'system_prompt': """Jesteś doświadczonym analitykiem giełdowym specjalizującym się 
w small-cap stocks (poniżej $15). Twoim zadaniem jest analiza danych rynkowych 
i wydanie jednego z trzech werdyktów:

BUY   - wyraźny sygnał do zakupu, wysoka pewność
WATCH - interesujący ticker, obserwuj ale nie kupuj jeszcze  
AVOID - brak sygnału lub sygnał niedźwiedzi, omijaj

Twoja analiza powinna uwzględniać:
- Momentum cenowe i wolumenowe
- Dark pool i opcje (smart money)
- Newsy i eventy (earnings, FDA, insider transactions)
- Historię poprzednich sygnałów dla tego tickera
- Ryzyko manipulacji (pump & dump, false breakout)

Odpowiedź zawsze w formacie:
WERDYKT: [BUY/WATCH/AVOID]
PEWNOŚĆ: [WYSOKA/ŚREDNIA/NISKA]
UZASADNIENIE: [2-3 zdania konkretnego uzasadnienia]
RYZYKO: [główne ryzyko w jednym zdaniu]"""
}

# ==================== STREFA CZASOWA ====================
CHICAGO_TZ = pytz.timezone('America/Chicago')

def now_chicago():
    """Aktualny czas w Chicago (CST/CDT)"""
    return datetime.now(CHICAGO_TZ)

def is_market_open():
    """Czy rynek jest otwarty (9:30-16:00 ET = 8:30-15:00 CST)"""
    n = now_chicago()
    if n.weekday() >= 5:  # sobota, niedziela
        return False
    market_open  = n.replace(hour=8,  minute=30, second=0, microsecond=0)
    market_close = n.replace(hour=15, minute=0,  second=0, microsecond=0)
    return market_open <= n <= market_close

def is_premarket():
    """Czy trwa pre-market (4:00-8:30 CST)"""
    n = now_chicago()
    if n.weekday() >= 5:
        return False
    premarket_open  = n.replace(hour=4,  minute=0,  second=0, microsecond=0)
    premarket_close = n.replace(hour=8,  minute=30, second=0, microsecond=0)
    return premarket_open <= n < premarket_close

def is_aftermarket():
    """Czy trwa after-market (15:00-20:00 CST)"""
    n = now_chicago()
    if n.weekday() >= 5:
        return False
    aftermarket_open  = n.replace(hour=15, minute=0,  second=0, microsecond=0)
    aftermarket_close = n.replace(hour=20, minute=0,  second=0, microsecond=0)
    return aftermarket_open <= n < aftermarket_close

def get_market_status():
    """Zwraca status rynku jako string"""
    n = now_chicago()
    if n.weekday() >= 5:
        return "WEEKEND"
    if is_premarket():
        return "PRE-MARKET"
    if is_market_open():
        return "OPEN"
    if is_aftermarket():
        return "AFTER-MARKET"
    return "CLOSED"

def get_min_volume():
    """Zwraca minimalny wolumen zależnie od sesji"""
    if is_market_open():
        return CONFIG["min_volume"]
    return CONFIG["min_volume_extended"]

# ==================== LOGOWANIE ====================
os.makedirs(CONFIG['log_dir'], exist_ok=True)

# UTF-8 dla logów — fix dla Windows cp1252 (polskie znaki, strzałki)
_stdout_utf8 = open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(
            f"{CONFIG['log_dir']}/{CONFIG['log_file']}",
            encoding='utf-8'
        ),
        logging.StreamHandler(_stdout_utf8)
    ]
)
logger = logging.getLogger(__name__)

# ==================== WALIDACJA ====================
def validate_config():
    """Sprawdź czy konfiguracja jest poprawna"""
    issues = []

    if not DEMO_MODE:
        if not POLYGON_API_KEY:
            issues.append("Brak POLYGON_API_KEY w pliku .env")
        if not ANTHROPIC_API_KEY:
            issues.append("Brak ANTHROPIC_API_KEY w pliku .env")

    if CONFIG['max_tickers_for_claude'] > 10:
        issues.append("max_tickers_for_claude > 10 — koszty Claude będą wysokie")

    if issues:
        for issue in issues:
            logger.warning(f"⚠️  {issue}")
        return False

    return True

# ==================== STARTUP ====================
if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  {SYSTEM_NAME} v{SYSTEM_VERSION}")
    print(f"  Tryb: {'DEMO (MockPolygon)' if DEMO_MODE else 'LIVE'}")
    print(f"  Rynek: {get_market_status()}")
    print(f"  Czas: {now_chicago().strftime('%Y-%m-%d %H:%M:%S')} CST")
    print(f"{'='*50}\n")

    if validate_config():
        print("✅ Konfiguracja OK")
    else:
        print("⚠️  Konfiguracja ma ostrzeżenia (sprawdź wyżej)")

    print("\nParametry skanowania:")
    print(f"  Cena:          ${CONFIG['min_price']} - ${CONFIG['max_price']}")
    print(f"  Min wolumen:   {CONFIG['min_volume']:,}")
    print(f"  Tickerów/cykl: {CONFIG['max_tickers_for_claude']} (do Claude AI)")
    print(f"  Cykl główny:   co {CONFIG['main_scan_interval']//60} minuty")
    print(f"  Cykl UW:       co {CONFIG['uw_scan_interval']} sekundy")
    print(f"  Pre-market:    {'✅ włączony' if CONFIG['premarket_enabled'] else '❌ wyłączony'} (co {CONFIG['premarket_scan_interval']//60} min)")
    print(f"  After-market:  {'✅ włączony' if CONFIG['aftermarket_enabled'] else '❌ wyłączony'} (co {CONFIG['aftermarket_scan_interval']//60} min)")
    print(f"  Min vol ext:   {CONFIG['min_volume_extended']:,}")
    print(f"  Telegram:      {'✅ włączony' if CONFIG['telegram_enabled'] else '❌ wyłączony'}")
