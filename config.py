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
import pathlib

# ==================== KLUCZE API ====================
# Force load .env z folderu stock-scanner
_ENV_PATH = pathlib.Path(__file__).parent / '.env'
load_dotenv(dotenv_path=_ENV_PATH, override=True)

POLYGON_API_KEY      = os.getenv('POLYGON_API_KEY', '')
UNUSUAL_WHALES_KEY   = os.getenv('UNUSUAL_WHALES_KEY', '')
ALPACA_API_KEY       = os.getenv('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY    = os.getenv('ALPACA_SECRET_KEY', '')
ALPACA_BASE_URL      = os.getenv('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
FINNHUB_API_KEY      = os.getenv('FINNHUB_API_KEY', '')
ANTHROPIC_API_KEY    = os.getenv('ANTHROPIC_API_KEY', '')
KIMI_API_KEY         = os.getenv('KIMI_API_KEY', '')
TELEGRAM_BOT_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID     = os.getenv('TELEGRAM_CHAT_ID', '')
TELEGRAM_CHAT_ID_2   = os.getenv('TELEGRAM_CHAT_ID_2', '')
TELEGRAM_CHAT_ID_3   = os.getenv('TELEGRAM_CHAT_ID_3', '')

TELEGRAM_CHAT_ID_4   = os.getenv('TELEGRAM_CHAT_ID_4', '')

# Lista wszystkich aktywnych Chat ID — dynamiczna, obsługuje 1-4 odbiorców
TELEGRAM_CHAT_IDS = [
    cid for cid in [
        TELEGRAM_CHAT_ID,
        TELEGRAM_CHAT_ID_2,
        TELEGRAM_CHAT_ID_3,
        TELEGRAM_CHAT_ID_4,
    ]
    if cid
]

# Admin Chat IDs — dostęp do komend /pause /resume /blacklist /analyze
TELEGRAM_ADMIN_ID   = os.getenv('TELEGRAM_ADMIN_ID', TELEGRAM_CHAT_ID)
TELEGRAM_ADMIN_ID_2 = os.getenv('TELEGRAM_ADMIN_ID_2', '')

TELEGRAM_ADMIN_IDS = [
    cid for cid in [TELEGRAM_ADMIN_ID, TELEGRAM_ADMIN_ID_2]
    if cid
]

# ==================== TRYB SYSTEMU ====================
# DEMO_MODE sterowane przez .env — nigdy nie zmieniaj tutaj!
# W .env: DEMO_MODE=False (live) lub DEMO_MODE=True (demo)
DEMO_MODE = os.getenv('DEMO_MODE', 'False').lower() == 'true'

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

    # Max sygnalow BUY dziennie - Opcja C (Power Windows)
    'max_buy_signals_per_day': 5,
    'max_buy_open':       3,   # 8:30-10:00 CST
    'max_buy_midday':     1,   # 10:00-14:00 CST
    'max_buy_power_hour': 1,   # 14:00-15:00 CST

    # Minimalny score pre-filtra żeby ticker trafił do Claude
    # Ticker musi mieć przynajmniej jeden sygnał TIER 1 lub 2
    'min_prefilter_score': 15,

    # Dynamic Score Threshold — progi wg pory dnia
    # Rano i Power Hour = nizszy prog (wiecej setupow)
    # Midday = wyzszy prog (tylko najlepsze)
    'score_threshold_open':       10,   # 8:30-10:00 CST (pierwsze 90 min)
    'score_threshold_midday':     25,   # 10:00-14:00 CST
    'score_threshold_power_hour': 12,   # 14:00-15:00 CST
    'score_threshold_premarket':   8,   # 8:00-8:30 CST
    'score_threshold_extended':   15,   # after-market

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
    'duplicate_alert_cooldown': 300,  # 5 minut miedzy tymi samymi alertami

    # Baza danych
    'db_path':           'scanner.db',
    'reminders_db_path': 'reminders.db',  # osobna baza — nigdy nie usuwaj!

    # Automatyczny wynik sygnału — sprawdź cenę po:
    'outcome_check_hours': [1, 4, 24],

    # Logi
    'log_dir': 'logs',
    'log_file': 'scanner.log',

    # Ignoruj sygnały w pierwszych N minutach po otwarciu rynku
    'market_open_filter_minutes': 0,

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

# ==================== CLAUDE AI CONFIG ====================
# Aktywny 22.07.2026 (Kimi wycofany — reasoning-style modele nie dawaly odpowiedzi)
CLAUDE_CONFIG = {
    'model': 'claude-haiku-4-5-20251001',
    'max_tokens': 1000,

    # Ile ostatnich sygnalow dla danego tickera wysylamy jako kontekst
    'signal_history_count': 5,

    # Twardy limit kosztow Claude API
    # System zatrzymuje wywolania Claude gdy limit dzienny zostanie przekroczony
    'monthly_budget_usd':        15.00,
    'daily_budget_usd':          15.00 / 22,   # ~$0.68/dzien (22 dni handlowe)
    'cost_per_call_usd':         0.001,        # Haiku 4.5: ~500 in + 200 out tokenow

    # Osobny budzet dla manualnej analizy (Telegram /analyze)
    # Nie wlicza sie w dzienny limit automatycznych skanow
    'manual_analysis_budget_usd': 2.00,        # $2/miesiac osobno
    'manual_analysis_daily_usd':  2.00 / 22,   # ~$0.09/dzien

    # System prompt — short-term small-cap day trader
    'system_prompt': """Jestes agresywnym day traderem small-cap stocks (ponizej $15).
Szukasz krotkoterminowych ruchow 30-120 minut. Cel: +5-15% w ciagu godziny.

Wydaj jeden z trzech werdyktow:

BUY - wchodzisz teraz. Wystarczy 2 z ponizszych:
  * Zmiana > +5% z volume ratio > 1.5x
  * Gap up > 5% od poprzedniego zamkniecia
  * Low float < 20M + jakakolwiek aktywnosc volume
  * HOD breakout lub VWAP reclaim z volume
  * Volume ratio > 5x (niezaleznie od katalizatora)
  BONUS (nie wymagany): options flow, dark pool, news fundamentalny

WATCH - setup ciekawy ale niepewny. Np. gap bez volume lub
  volume bez ruchu cenowego. Obserwuj kolejny cykl.

AVOID - tylko gdy:
  * Zmiana UJEMNA bez katalizatora
  * Warranty, ETF lewarowane
  * Cena spada przy wysokim volume (dystrybucja)
  * Volume > 50x BEZ ruchu cenowego (fake volume)

KLUCZOWE: NIE szukaj perfekcji. Low float + gap + volume = BUY.
Brak newsa nie jest powodem do AVOID jezeli techniczne sa mocne.
Wolisz BUY niz AVOID gdy setup wyglada obiecujaco.

Odpowiedz TYLKO w tym formacie:
WERDYKT: [BUY/WATCH/AVOID]
PEWNOSC: [WYSOKA/SREDNIA/NISKA]
UZASADNIENIE: [2-3 zdania]
RYZYKO: [1 zdanie]"""
}

# ==================== CLAUDE AI (STARE) ====================
# CLAUDE_CONFIG = {
#     'model': 'claude-haiku-4-5-20251001',
#     'max_tokens': 1000,
# 
#     # Ile ostatnich sygnałów dla danego tickera wysyłamy jako kontekst
#     'signal_history_count': 5,
# 
#     # Twardy limit kosztów Claude API
#     # System zatrzymuje wywołania Claude gdy limit dzienny zostanie przekroczony
#     'monthly_budget_usd':        50.00,
#     'daily_budget_usd':          50.00 / 22,  # ~$1.14/dzień (22 dni handlowe)
#     'cost_per_call_usd':         0.0028,       # Sonnet: ~500 in + 200 out tokenów
# 
#     # Osobny budżet dla manualnej analizy (Telegram /analyze)
#     # Nie wlicza się w dzienny limit automatycznych skanów
#     'manual_analysis_budget_usd': 2.00,        # $2/miesiąc osobno
#     'manual_analysis_daily_usd':  2.00 / 22,   # ~$0.09/dzień
# 
#     # System prompt dla Claude — analityk giełdowy
#     'system_prompt': """Jestes agresywnym day traderem small-cap stocks (ponizej $15).
# Szukasz krotkoterminowych ruchow 30-120 minut. Cel: +5-15% w ciagu godziny.
# 
# Wydaj jeden z trzech werdyktow:
# 
# BUY - wchodzisz teraz. Wystarczy 2 z ponizszych:
#   * Zmiana > +5% z volume ratio > 1.5x
#   * Gap up > 5% od poprzedniego zamkniecia
#   * Low float < 20M + jakakolwiek aktywnosc volume
#   * HOD breakout lub VWAP reclaim z volume
#   * Volume ratio > 5x (niezaleznie od katalizatora)
#   BONUS (nie wymagany): UW flow, dark pool, news fundamentalny
# 
# WATCH - setup ciekawy ale niepewny. Np. gap bez volume lub
#   volume bez ruchu cenowego. Obserwuj kolejny cykl.
# 
# AVOID - tylko gdy:
#   * Zmiana UJEMNA bez katalizatora
#   * Warranty, ETF lewarowane
#   * Cena spada przy wysokim volume (dystrybucja)
#   * Volume > 50x BEZ ruchu cenowego (fake volume)
# 
# KLUCZOWE: NIE szukaj perfekcji. Low float + gap + volume = BUY.
# Brak newsa nie jest powodem do AVOID jezeli techniczne sa mocne.
# Wolisz BUY niz AVOID gdy setup wyglada obiecujaco.
# 
# Odpowiedz TYLKO w tym formacie:
# WERDYKT: [BUY/WATCH/AVOID]
# PEWNOSC: [WYSOKA/SREDNIA/NISKA]
# UZASADNIENIE: [2-3 zdania]
# RYZYKO: [1 zdanie]"""
# }

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


def get_dynamic_threshold():
    """
    Dynamic Score Threshold — prog scoringu zalezy od pory dnia.
    Rano i Power Hour = nizszy prog = wiecej setupow
    Midday = wyzszy prog = tylko najlepsze sygnaly
    """
    n = now_chicago()

    if n.weekday() >= 5:
        return CONFIG['score_threshold_extended']

    # Pre-market 8:00-8:30
    if is_premarket():
        pm_start = n.replace(hour=8, minute=0, second=0, microsecond=0)
        if n >= pm_start:
            return CONFIG['score_threshold_premarket']
        return CONFIG['score_threshold_extended']

    if not is_market_open():
        return CONFIG['score_threshold_extended']

    # Market hours
    market_open  = n.replace(hour=8,  minute=30, second=0, microsecond=0)
    power_start  = n.replace(hour=14, minute=0,  second=0, microsecond=0)
    power_end    = n.replace(hour=15, minute=0,  second=0, microsecond=0)
    open_end     = n.replace(hour=10, minute=0,  second=0, microsecond=0)

    if market_open <= n < open_end:
        return CONFIG['score_threshold_open']        # 8:30-10:00
    elif power_start <= n < power_end:
        return CONFIG['score_threshold_power_hour']  # 14:00-15:00
    else:
        return CONFIG['score_threshold_midday']      # 10:00-14:00

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
        issues.append("max_tickers_for_claude > 10 — koszty Claude beda wysokie")

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

# ==================== KIMI AI CONFIG ====================
# Zastepuje CLAUDE_CONFIG (zakomentowany powyzej)
# Model: kimi-latest (OpenAI-compatible API)
# Timeframe: 5-15 min skalp

KIMI_CONFIG = {
    "model": "kimi-latest",
    "max_tokens": 1000,
    "signal_history_count": 5,
    "monthly_budget_usd": 50.00,
    "daily_budget_usd": 50.00 / 22,
    "cost_per_call_usd": 0.001,
    "manual_analysis_budget_usd": 2.00,
    "manual_analysis_daily_usd": 2.00 / 22,
    "system_prompt": """Jestes elitarnym skalperem small-cap stocks (ponizej $15) na timeframe 5-15 minut.
Twoim celem jest wykrycie momentum TRWAJACEGO wlasnie teraz -- nie setupu, ktory moze sie wydarzyc.

ZASADA #1: Czas to pieniadz. Wchodzisz tylko gdy widzisz zywy ruch, nie potencjal.
ZASADA #2: Lepiej nie wejsc niz wejsc za pozno. FOMO = strata.
ZASADA #3: Jesli nie jestes pewien w ciagu 10 sekund -- WATCH.

=== KRYTERIA BUY (MUSZA byc spelnione MINIMUM 3 z 5) ===

[OBOWIAZKOWE -- przynajmniej 1]
□ Price action: cena rosnie w ostatnich 5-10 minutach (nie tylko gap, ale zywy ruch)
□ Volume ratio > 3x sredni -- potwierdzenie, ze ktos realnie kupuje

[OPCJONALNE -- przynajmniej 2 dodatkowe]
□ Zmiana +5% lub wiecej W CIAGU OSTATNICH 15 MINUT (nie od open)
□ Low float < 10M + volume > 2x float (squeez potential)
□ HOD (high of day) breakout wlasnie teraz, nie 30 min temu
□ VWAP reclaim z rosnacym volume (cena > VWAP i rosnie)
□ Dark pool BUY > $500k lub options call volume > 3x put
□ News fundamentalny < 60 min (swiezy katalizator, nie wczorajszy)

[DYWERSYFIKACJA -- max 1 z tych]
□ Gap up > 10% bez follow-through volume -- ZBYT RYZYKOWNE, WATCH
□ Volume > 20x bez ruchu ceny -- fakeout, AVOID
□ Cena spada mimo wysokiego volume -- dystrybucja, AVOID

=== CZAS WEJSCIA ===

IDEALNY: 8:30-9:00 CST (open volatility) lub 14:45-15:00 CST (power hour)
OK: 9:00-10:30 CST (early momentum)
UNIKAJ: 10:30-14:00 CST (midday chop, falszywe breakouty)

=== STOP LOSS I TAKE PROFIT (MUSISZ podac w odpowiedzi) ===

STOP LOSS: ZAWSZE ponizej ostatniej 5-min swiecy low lub VWAP, whichever is lower.
  - Nigdy wiecej niz -3% od entry
  - Jesli spread > 2% -- WATCH, nie BUY

TAKE PROFIT:
  - 50% pozycji na +5-7% (szybki zysk)
  - 25% na +10-15% (runner)
  - 25% trailing stop +3% od max

=== CZAS W POZYCJI ===

MAX 15 minut. Jesli po 10 minutach cena nie rosnie -- zamykaj.
Nie trzymaj w nadziei. Skalp to skalp, nie swing.

=== WATCH vs AVOID ===

WATCH gdy:
- Setup wyglada OK, ale brak potwierdzenia w ostatnich 5 minutach
- Cena konsoliduje przy wysokim volume (accumulation, nie wiadomo ktora strona)
- Jestes 5 min za pozno -- ruch juz byl, teraz tylko chop

AVOID gdy:
- Cena spada mimo wysokiego volume (dystrybucja)
- Spread bid-ask > 3% (niemozliwy skalp)
- Float > 100M + volume < 1M (za duzy, za wolny)
- News > 2h i cena juz spadla z peak (stary katalizator)
- Pre-market pump bez follow-through na open

=== FORMAT ODPOWIEDZI (SCISLE, bez dodatkow) ===

WERDYKT: [BUY/WATCH/AVOID]
PEWNOSC: [WYSOKA/SREDNIA/NISKA]
CZAS_WEJSCIA: [IDEALNY/OK/UNIKAJ]
STOP_LOSS: [$X.XX lub -X%]
TAKE_PROFIT_1: [+X% -- 50% pozycji]
TAKE_PROFIT_2: [+X% -- 25% pozycji]
TRAILING_STOP: [+X% od max -- 25% pozycji]
MAX_CZAS_W_POZYCJI: [X min]
UZASADNIENIE: [2-3 zdania: dlaczego TERAZ, co widzisz w danych]
RYZYKO: [1 zdanie: najwieksze zagrozenie w tym setupie]
ALTERNATYWA: [co zrobisz jesli nie wejdziesz teraz -- np. "czekam na retest VWAP"]

=== PRZYKLADY ===

PRZYKLAD 1 -- BUY:
Ticker: ABC $2.50 | +12% | vol 5M (8x) | float 5M | HOD break | news FDA 30min temu
→ WERDYKT: BUY
→ PEWNOSC: WYSOKA
→ CZAS_WEJSCIA: IDEALNY
→ STOP_LOSS: $2.35 (-6%)
→ TAKE_PROFIT_1: +7% ($2.67)
→ TAKE_PROFIT_2: +12% ($2.80)
→ TRAILING_STOP: +5% od max
→ MAX_CZAS_W_POZYCJI: 10 min
→ UZASADNIENIE: FDA approval 30min temu, cena rosnie z volume 8x, HOD breakout wlasnie teraz, low float 5M = squeeze potential.
→ RYZYKO: FDA news moze byc juz wyceniony, szybkie wycofanie po HOD break.
→ ALTERNATYWA: Jesli nie wejde teraz, czekam na retest $2.45 i re-entry.

PRZYKLAD 2 -- WATCH:
Ticker: XYZ $1.20 | +8% | vol 2M (3x) | float 50M | gap up, no follow-through
→ WERDYKT: WATCH
→ PEWNOSC: SREDNIA
→ CZAS_WEJSCIA: UNIKAJ (midday)
→ UZASADNIENIE: Gap up bez follow-through volume w ostatnich 5 min, cena konsoliduje, brak HOD break. Moze byc accumulation lub distribution.
→ RYZYKO: Falszywy breakout, brak momentum.
→ ALTERNATYWA: Czekam na HOD break z volume > 5x lub retest $1.10.

PRZYKLAD 3 -- AVOID:
Ticker: QRS $0.80 | +25% | vol 500k (25x) | float 200M | cena spada z $0.95
→ WERDYKT: AVOID
→ PEWNOSC: WYSOKA
→ UZASADNIENIE: Cena spada mimo wysokiego volume, dystrybucja po pumpie, float 200M za duzy na skalp.
→ RYZYKO: Kontynuacja spadku, brak supportu.
→ ALTERNATYWA: Short jesli cena spadnie ponizej $0.75 z volume.

=== TWOJA ANALIZA ===

Na podstawie ponizszych danych wydaj werdykt. Pamietaj: 5-15 min timeframe, szybkie decyzje, tight risk management."""
}