#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 6: TELEGRAM ALERTY
Zapisz jako telegram_alerts.py w folderze stock-scanner

Zadanie:
- Wysyła alerty dla sygnałów BUY/WATCH/AVOID
- Wysyła alerty re-analizy gdy trigger się odpali
- Co godzinę wysyła podsumowanie (dashboard)
- W trybie DEMO drukuje do konsoli zamiast wysyłać
"""

import requests
from config import (logger, CONFIG, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                    TELEGRAM_CHAT_IDS, now_chicago)


# ==================== WYSYŁANIE WIADOMOŚCI ====================

def send_message(text, parse_mode='HTML'):
    """
    Wysyła wiadomość na wszystkie skonfigurowane Chat ID.
    W trybie DEMO (brak tokena) drukuje do konsoli.
    """
    if not CONFIG['telegram_enabled']:
        # DEMO — drukuj do konsoli
        print("\n" + "─"*50)
        print("📱 TELEGRAM (DEMO — konsola):")
        print(text)
        print("─"*50)
        return True

    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    success = False

    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            response = requests.post(
                url,
                data={
                    'chat_id':    chat_id,
                    'text':       text[:4096],
                    'parse_mode': parse_mode,
                },
                timeout=10
            )
            if response.status_code == 200:
                success = True
            else:
                logger.error(f"Telegram błąd {chat_id}: "
                             f"{response.status_code}")
        except Exception as e:
            logger.error(f"Telegram wyjątek {chat_id}: {e}")

    if success:
        logger.info(f"Telegram: wysłano na {len(TELEGRAM_CHAT_IDS)} chat(y)")
    return success


# ==================== ALERTY SYGNAŁÓW ====================

def alert_signal(result, ticker_data):
    """
    Wysyła alert dla nowego sygnału Claude'a.
    Filtruje duplikaty przez cooldown.
    """
    verdict    = result.get('verdict', 'WATCH')
    ticker     = result.get('ticker', 'UNKNOWN')
    confidence = result.get('confidence', '')
    just       = result.get('justification', '')
    risk       = result.get('risk', '')

    price        = ticker_data.get('price', 0)
    change_pct   = ticker_data.get('change_pct', 0)
    volume       = ticker_data.get('volume', 0)
    volume_ratio = ticker_data.get('volume_ratio', 1.0)
    reasons      = ticker_data.get('reasons', [])

    # Ikony
    if verdict == 'BUY':
        icon        = '🟢'
        verdict_str = '🟢 BUY'
    elif verdict == 'WATCH':
        icon        = '🟡'
        verdict_str = '🟡 WATCH'
    else:
        icon        = '🔴'
        verdict_str = '🔴 AVOID'

    confidence_str = {
        'WYSOKA': '⭐⭐⭐',
        'ŚREDNIA': '⭐⭐',
        'NISKA': '⭐',
    }.get(confidence, '⭐')

    # Powody pre-filtra (skrócone)
    reasons_str = ''
    if reasons:
        reasons_str = '\n'.join(f'  • {r}' for r in reasons[:3])

    time_str = now_chicago().strftime('%H:%M CST')

    # Stop-loss i take-profit (z bazy lub z result)
    stop_loss   = result.get('stop_loss')
    take_profit = result.get('take_profit')
    rr_ratio    = result.get('rr_ratio')
    risk_pct    = result.get('risk_pct')
    reward_pct  = result.get('reward_pct')
    sl_basis    = result.get('sl_basis', '')

    # Sekcja SL/TP dla BUY
    sl_tp_str = ''
    if verdict == 'BUY' and stop_loss and take_profit:
        basis_label = {
            'vwap':  'VWAP',
            'atr':   'ATR',
            'pct_4': '4% stały',
        }.get(sl_basis, sl_basis)

        sl_tp_str = f"""
🎯 <b>Plan pozycji:</b>
  Stop loss:   <b>${stop_loss:.2f}</b> (-{risk_pct:.1f}%) [{basis_label}]
  Take profit: <b>${take_profit:.2f}</b> (+{reward_pct:.1f}%)
  R/R ratio:   {rr_ratio:.1f}:1"""

    message = f"""{icon} <b>{ticker} — {verdict_str}</b>
Pewność: {confidence_str} {confidence}
Czas: {time_str}

💰 Cena: <b>${price:.2f}</b> ({change_pct:+.1f}%)
📊 Wolumen: {volume:,} ({volume_ratio:.1f}x średniej)
{sl_tp_str}
📋 Sygnały:
{reasons_str}

🤖 Claude:
{just}

⚠️ Ryzyko:
{risk}"""

    # Wysyłaj tylko BUY i WATCH z wysoką pewnością
    if verdict == 'BUY':
        return send_message(message)
    elif verdict == 'WATCH' and confidence == 'WYSOKA':
        return send_message(message)
    else:
        logger.info(f"Telegram: pominięto {verdict} {confidence} dla {ticker}")
        return False


def alert_retrigger(ticker, trigger, details, old_verdict, new_verdict,
                    current_price, entry_price):
    """
    Wysyła alert gdy trigger re-analizy się odpali.
    Jeśli nowy werdykt to AVOID — wysyła SELL SIGNAL zamiast RE-ANALIZA.
    """
    price_change = ((current_price - entry_price) / entry_price) * 100

    trigger_icons = {
        'VOLUME_DROP':      '📉',
        'PRICE_REVERSAL':   '🔻',
        'TAKE_PROFIT':      '💰',
        'DARKPOOL_SELL':    '🐋',
        'OPTIONS_BEARISH':  '📊',
        'UW_ACTIVITY_GONE': '👻',
    }
    icon = trigger_icons.get(trigger, '⚡')

    trigger_labels = {
        'VOLUME_DROP':      'Wolumen słabnie',
        'PRICE_REVERSAL':   'Cena się cofa',
        'TAKE_PROFIT':      'Take profit osiągnięty',
        'DARKPOOL_SELL':    'Dark pool SELL',
        'OPTIONS_BEARISH':  'Options flow bearish',
        'UW_ACTIVITY_GONE': 'Aktywność UW zniknęła',
    }
    label = trigger_labels.get(trigger, trigger)

    verdict_icon = {
        'BUY':   '🟢',
        'WATCH': '🟡',
        'AVOID': '🔴',
    }

    old_icon = verdict_icon.get(old_verdict, '⚪')
    new_icon = verdict_icon.get(new_verdict, '⚪') if new_verdict else '🔄'

    time_str = now_chicago().strftime('%H:%M CST')

    # SELL SIGNAL — gdy Claude zmienia BUY → AVOID
    if old_verdict == 'BUY' and new_verdict == 'AVOID':
        profit_str = f"+{price_change:.1f}% zysku" if price_change > 0                      else f"{price_change:.1f}% straty"
        message = f"""🔴 <b>SELL SIGNAL — {ticker}</b>
Trigger: {label}
Czas: {time_str}

💰 Wejście:  ${entry_price:.2f}
💰 Teraz:    ${current_price:.2f} ({price_change:+.1f}%)
📋 Powód: {details}

⚠️ Rozważ zamknięcie pozycji ({profit_str})"""

    else:
        message = f"""{icon} <b>{ticker} — RE-ANALIZA</b>
Trigger: {label}
Czas: {time_str}

💰 Cena: ${current_price:.2f} ({price_change:+.1f}% od sygnału)
📋 Szczegóły: {details}

Werdykt: {old_icon} {old_verdict}"""

        if new_verdict:
            message += f" → {new_icon} {new_verdict}"

    return send_message(message)


def alert_take_profit(ticker, entry_price, current_price, gain_pct):
    """
    Specjalny alert dla take profit (+10%).
    """
    time_str = now_chicago().strftime('%H:%M CST')

    message = f"""💰 <b>{ticker} — TAKE PROFIT</b>
Czas: {time_str}

Wejście: ${entry_price:.2f}
Teraz:   ${current_price:.2f}
Zysk:    <b>+{gain_pct:.1f}%</b> 🎯

Rozważ realizację części zysku."""

    return send_message(message)


# ==================== DASHBOARD ====================

def send_hourly_dashboard(stats, active_signals, top_today):
    """
    Wysyła godzinne podsumowanie systemu.

    stats          — dict z database.get_stats()
    active_signals — lista aktywnych BUY
    top_today      — lista najlepszych sygnałów z dzisiaj
    """
    time_str = now_chicago().strftime('%H:%M CST')

    # Statystyki trafności
    avg = stats.get('avg_outcomes_buy', {})
    acc_str = ''
    if avg.get('1h') is not None:
        acc_str += f"\n  1h:  {avg['1h']:+.1f}%"
    if avg.get('4h') is not None:
        acc_str += f"\n  4h:  {avg['4h']:+.1f}%"
    if avg.get('24h') is not None:
        acc_str += f"\n  24h: {avg['24h']:+.1f}%"

    by_verdict = stats.get('by_verdict', {})

    message = f"""📊 <b>DASHBOARD — {time_str}</b>

📈 Sygnały dziś:
  🟢 BUY:   {by_verdict.get('BUY', 0)}
  🟡 WATCH: {by_verdict.get('WATCH', 0)}
  🔴 AVOID: {by_verdict.get('AVOID', 0)}
  Łącznie:  {stats.get('total_signals', 0)}"""

    if acc_str:
        message += f"\n\n🎯 Średni wynik BUY:{acc_str}"

    if active_signals:
        message += f"\n\n👁 Monitorowane ({len(active_signals)}):"
        for sig in active_signals[:5]:
            message += f"\n  • {sig['ticker']} @ ${sig['price']:.2f}"

    if top_today:
        message += "\n\n🏆 Top sygnały dziś:"
        for s in top_today[:3]:
            icon = ('🟢' if s.get('verdict') == 'BUY'
                    else '🟡' if s.get('verdict') == 'WATCH'
                    else '🔴')
            message += (f"\n  {icon} {s.get('ticker')} — "
                        f"{s.get('verdict')} @ ${s.get('price', 0):.2f}")

    return send_message(message)


def send_startup_message(demo_mode=True):
    """Wysyła wiadomość przy starcie systemu"""
    time_str = now_chicago().strftime('%Y-%m-%d %H:%M CST')
    mode_str = '🔬 DEMO' if demo_mode else '🚀 LIVE'

    message = f"""✅ <b>STOCK SCANNER uruchomiony</b>
Tryb: {mode_str}
Czas: {time_str}

Parametry:
  • Cena: $0.01 — $15.00
  • Min wolumen: 100,000
  • Cykl: co 5 min (UW: co 1 min)
  • TOP 5 tickerów → Claude AI"""

    return send_message(message)


def send_shutdown_message(stats):
    """Wysyła wiadomość przy zatrzymaniu systemu"""
    time_str = now_chicago().strftime('%H:%M CST')
    by_verdict = stats.get('by_verdict', {})

    message = f"""🛑 <b>STOCK SCANNER zatrzymany</b>
Czas: {time_str}

Sesja:
  🟢 BUY:   {by_verdict.get('BUY', 0)}
  🟡 WATCH: {by_verdict.get('WATCH', 0)}
  🔴 AVOID: {by_verdict.get('AVOID', 0)}
  Łącznie:  {stats.get('total_signals', 0)}"""

    return send_message(message)


# ==================== TEST ====================

if __name__ == "__main__":
    print("\n" + "="*50)
    print("  TEST: Telegram Alerty")
    print("="*50)

    # Test startup
    print("\n✅ Test startup:")
    send_startup_message(demo_mode=True)

    # Test sygnału BUY
    print("\n✅ Test alert BUY:")
    fake_result = {
        'ticker':        'SOUN',
        'verdict':       'BUY',
        'confidence':    'WYSOKA',
        'justification': ('SoundHound pokazuje silne momentum: volume 4.2x '
                          'średniej, dark pool $1.2M BUY. Nowy kontrakt '
                          'z producentem samochodów potwierdza kierunek.'),
        'risk':          'Możliwy fałszywy breakout jeśli wolumen '
                         'nie utrzyma się w kolejnych 30 minutach.',
    }
    fake_ticker = {
        'price':        3.42,
        'change_pct':   8.7,
        'volume':       2_100_000,
        'volume_ratio': 4.2,
        'reasons':      [
            'Volume 4.2x średniej (bardzo wysoki)',
            'Zmiana +8.7% (duży ruch)',
            'Dark pool $1,200,000 (głównie kupno)',
        ],
    }
    alert_signal(fake_result, fake_ticker)

    # Test sygnału WATCH
    print("\n✅ Test alert WATCH (wysoka pewność):")
    fake_watch = {
        'ticker':        'IONQ',
        'verdict':       'WATCH',
        'confidence':    'WYSOKA',
        'justification': ('IonQ na radarze — earnings za 3 dni, '
                          'poprzedni raport bił oczekiwania. '
                          'Volume podwyższony ale brak dark pool.'),
        'risk':          'Ryzyko sell-the-news po earnings.',
    }
    fake_watch_ticker = {
        'price':        8.91,
        'change_pct':   3.2,
        'volume':       450_000,
        'volume_ratio': 1.8,
        'reasons':      [
            'Earnings za 3 dni (poprzedni: beat)',
            'Volume 1.8x średniej (podwyższony)',
        ],
    }
    alert_signal(fake_watch, fake_watch_ticker)

    # Test re-trigger
    print("\n✅ Test alert re-trigger (PRICE_REVERSAL):")
    alert_retrigger(
        ticker='SOUN',
        trigger='PRICE_REVERSAL',
        details='Cena cofnęła się o -4.2% od sygnału ($3.42 → $3.28)',
        old_verdict='BUY',
        new_verdict='WATCH',
        current_price=3.28,
        entry_price=3.42,
    )

    # Test take profit
    print("\n✅ Test alert take profit:")
    alert_take_profit(
        ticker='SOUN',
        entry_price=3.42,
        current_price=3.85,
        gain_pct=12.6,
    )

    # Test dashboard
    print("\n✅ Test dashboard:")
    fake_stats = {
        'total_signals': 12,
        'by_verdict':    {'BUY': 4, 'WATCH': 5, 'AVOID': 3},
        'avg_outcomes_buy': {'1h': 2.3, '4h': 4.1, '24h': None},
        'active_monitoring': 2,
    }
    fake_active = [
        {'ticker': 'SOUN', 'price': 3.42},
        {'ticker': 'IONQ', 'price': 8.91},
    ]
    fake_top = [
        {'ticker': 'SOUN',  'verdict': 'BUY',   'price': 3.42},
        {'ticker': 'IONQ',  'verdict': 'WATCH',  'price': 8.91},
        {'ticker': 'MARA',  'verdict': 'BUY',    'price': 12.50},
    ]
    send_hourly_dashboard(fake_stats, fake_active, fake_top)

    # Test shutdown
    print("\n✅ Test shutdown:")
    send_shutdown_message(fake_stats)

    print("\n" + "="*50)
    print("  Plik 6 gotowy ✅")
    print("="*50)
