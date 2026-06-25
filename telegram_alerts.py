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

    confidence_str = {
        'WYSOKA': '⭐⭐⭐',
        'ŚREDNIA': '⭐⭐',
        'NISKA': '⭐',
    }.get(confidence, '⭐')

    time_str = now_chicago().strftime('%H:%M CST')

    # Stop-loss i take-profit
    stop_loss   = result.get('stop_loss')
    take_profit = result.get('take_profit')
    rr_ratio    = result.get('rr_ratio', 2.0)
    risk_pct    = result.get('risk_pct')
    reward_pct  = result.get('reward_pct')
    sl_basis    = result.get('sl_basis', '')

    basis_label = {
        'vwap':  'VWAP',
        'atr':   'ATR',
        'pct_4': '4%',
    }.get(sl_basis, sl_basis or '4%')

    if verdict == 'BUY':
        # SL/TP block
        if stop_loss and take_profit:
            sl_tp_block = (
                f"\n🛑 STOP LOSS:   <b>${stop_loss:.2f}</b> "
                f"(-{risk_pct:.1f}%) [{basis_label}]"
                f"\n🎯 TAKE PROFIT: <b>${take_profit:.2f}</b> "
                f"(+{reward_pct:.1f}%)"
                f"\n⚖️ R/R:          {rr_ratio:.1f}:1"
            )
        else:
            sl_tp_block = (
                f"\n🛑 STOP LOSS:   <b>${price*0.96:.2f}</b> (-4%) [4%]"
                f"\n🎯 TAKE PROFIT: <b>${price*1.08:.2f}</b> (+8%)"
                f"\n⚖️ R/R:          2.0:1"
            )

        just_short = '. '.join(just.split('. ')[:2]) + '.' if just else ''
        reasons_line = ' | '.join(reasons[:2]) if reasons else '-'

        parts = [
            '🟢 <b>' + ticker + '</b> BUY ' + confidence_str,
            '⏰ ' + time_str,
            '',
            '💰 ENTRY: <b>$' + '{:.2f}'.format(price) + '</b> (' + '{:+.1f}'.format(change_pct) + '%)',
            '📊 Vol: ' + '{:,}'.format(volume) + ' (' + '{:.1f}'.format(volume_ratio) + 'x)',
            sl_tp_block,
            '',
            '📋 ' + reasons_line,
            '',
            '🤖 ' + just_short,
            '',
            '⚠️ ' + risk,
        ]
        message = chr(10).join(parts)
        return send_message(message)

    elif verdict == 'WATCH' and confidence == 'WYSOKA':
        just_short = '. '.join(just.split('. ')[:2]) + '.' if just else ''
        reasons_line = ' | '.join(reasons[:2]) if reasons else '-'
        parts = [
            '🟡 <b>' + ticker + '</b> WATCH ' + confidence_str,
            '⏰ ' + time_str,
            '',
            '$' + '{:.2f}'.format(price) + ' (' + '{:+.1f}'.format(change_pct) + '%) | Vol ' + '{:.1f}'.format(volume_ratio) + 'x',
            '',
            '📋 ' + reasons_line,
            '',
            '🤖 ' + just_short,
        ]
        message = chr(10).join(parts)
        return send_message(message)

    else:
        logger.info(f"Telegram: pominięto {verdict} {confidence} dla {ticker}")
        return False


def alert_retrigger(ticker, trigger, details, old_verdict, new_verdict,
                    current_price, entry_price):
    """
    Wysyła alert gdy trigger re-analizy się odpali.
    STOP_LOSS lub BUY → AVOID = SELL SIGNAL.
    """
    price_change = ((current_price - entry_price) / entry_price) * 100

    trigger_labels = {
        'STOP_LOSS':        'Stop-loss osiągnięty',
        'VOLUME_DROP':      'Wolumen słabnie',
        'PRICE_REVERSAL':   'Cena się cofa',
        'TAKE_PROFIT':      'Take profit osiągnięty',
        'DARKPOOL_SELL':    'Dark pool SELL',
        'OPTIONS_BEARISH':  'Options flow bearish',
        'UW_ACTIVITY_GONE': 'Aktywność UW zniknęła',
    }
    label = trigger_labels.get(trigger, trigger)

    verdict_icon = {'BUY': '🟢', 'WATCH': '🟡', 'AVOID': '🔴'}
    old_icon = verdict_icon.get(old_verdict, '⚪')
    new_icon = verdict_icon.get(new_verdict, '⚪') if new_verdict else '🔄'

    time_str = now_chicago().strftime('%H:%M CST')

    # SELL SIGNAL
    if trigger == 'STOP_LOSS' or (old_verdict == 'BUY' and new_verdict == 'AVOID'):
        result_str = 'ZYSK +{:.1f}%'.format(price_change) if price_change > 0 \
                     else 'STRATA {:.1f}%'.format(price_change)
        trigger_emoji = '🛑' if trigger == 'STOP_LOSS' else '🔴'
        dash = chr(8212)

        parts = [
            trigger_emoji + ' <b>SELL ' + dash + ' ' + ticker + '</b>',
            '⏰ ' + time_str,
            '',
            '💰 ENTRY: $' + '{:.2f}'.format(entry_price),
            '💰 EXIT:  $' + '{:.2f}'.format(current_price) + ' (' + '{:+.1f}%'.format(price_change) + ')',
            '📊 ' + result_str,
            '',
            '📋 Trigger: ' + label,
            '⚠️ ' + details,
        ]
        message = chr(10).join(parts)

    elif trigger == 'TAKE_PROFIT':
        gain_pct = price_change
        parts = [
            '💰 <b>TAKE PROFIT ' + chr(8212) + ' ' + ticker + '</b>',
            '⏰ ' + time_str,
            '',
            'ENTRY: $' + '{:.2f}'.format(entry_price),
            'EXIT:  $' + '{:.2f}'.format(current_price),
            'ZYSK:  +' + '{:.1f}%'.format(gain_pct) + ' 🎯',
            '',
            'Rozważ realizację zysku.',
        ]
        message = chr(10).join(parts)

    else:
        trigger_icons = {
            'VOLUME_DROP':      '📉',
            'PRICE_REVERSAL':   '🔻',
            'DARKPOOL_SELL':    '🐋',
            'OPTIONS_BEARISH':  '📊',
            'UW_ACTIVITY_GONE': '👻',
        }
        icon = trigger_icons.get(trigger, '⚡')

        message = (
            f"{icon} <b>{ticker} — RE-ANALIZA</b>\n"
            f"Trigger: {label}\n"
            f"Czas: {time_str}\n\n"
            f"💰 Cena: ${current_price:.2f} ({price_change:+.1f}% od sygnału)\n"
            f"📋 Szczegóły: {details}\n\n"
            f"Werdykt: {old_icon} {old_verdict}"
        )
        if new_verdict:
            message += f" → {new_icon} {new_verdict}"

    return send_message(message)


def alert_take_profit(ticker, entry_price, current_price, gain_pct):
    """Specjalny alert dla take profit."""
    time_str = now_chicago().strftime('%H:%M CST')
    parts = [
        '💰 <b>TAKE PROFIT ' + chr(8212) + ' ' + ticker + '</b>',
        '⏰ ' + time_str,
        '',
        'ENTRY: $' + '{:.2f}'.format(entry_price),
        'EXIT:  $' + '{:.2f}'.format(current_price),
        'ZYSK:  +' + '{:.1f}%'.format(gain_pct) + ' 🎯',
        '',
        'Rozważ realizację zysku.',
    ]
    return send_message(chr(10).join(parts))


# ==================== DASHBOARD ====================

def send_hourly_dashboard(stats, active_signals, top_today):
    """Wysyła godzinne podsumowanie systemu."""
    time_str = now_chicago().strftime('%H:%M CST')

    avg = stats.get('avg_outcomes_buy', {})
    acc_str = ''
    if avg.get('1h') is not None:
        acc_str += f"\n  1h:  {avg['1h']:+.1f}%"
    if avg.get('4h') is not None:
        acc_str += f"\n  4h:  {avg['4h']:+.1f}%"
    if avg.get('24h') is not None:
        acc_str += f"\n  24h: {avg['24h']:+.1f}%"

    by_verdict = stats.get('by_verdict', {})

    message = (
        f"📊 <b>DASHBOARD — {time_str}</b>\n\n"
        f"📈 Sygnały dziś:\n"
        f"  🟢 BUY:   {by_verdict.get('BUY', 0)}\n"
        f"  🟡 WATCH: {by_verdict.get('WATCH', 0)}\n"
        f"  🔴 AVOID: {by_verdict.get('AVOID', 0)}\n"
        f"  Łącznie:  {stats.get('total_signals', 0)}"
    )

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

    message = (
        f"✅ <b>STOCK SCANNER uruchomiony</b>\n"
        f"Tryb: {mode_str}\n"
        f"Czas: {time_str}\n\n"
        f"Parametry:\n"
        f"  • Cena: $0.01 — $15.00\n"
        f"  • Min wolumen: 100,000\n"
        f"  • Cykl: co 5 min (UW: co 1 min)\n"
        f"  • TOP 5 tickerów → Claude AI"
    )
    return send_message(message)


def send_shutdown_message(stats):
    """Wysyła wiadomość przy zatrzymaniu systemu"""
    time_str = now_chicago().strftime('%H:%M CST')
    by_verdict = stats.get('by_verdict', {})

    message = (
        f"🛑 <b>STOCK SCANNER zatrzymany</b>\n"
        f"Czas: {time_str}\n\n"
        f"Sesja:\n"
        f"  🟢 BUY:   {by_verdict.get('BUY', 0)}\n"
        f"  🟡 WATCH: {by_verdict.get('WATCH', 0)}\n"
        f"  🔴 AVOID: {by_verdict.get('AVOID', 0)}\n"
        f"  Łącznie:  {stats.get('total_signals', 0)}"
    )
    return send_message(message)


# ==================== TEST ====================

if __name__ == "__main__":
    print("\n" + "="*50)
    print("  TEST: Telegram Alerty")
    print("="*50)
    send_startup_message(demo_mode=True)
    print("\n" + "="*50)
    print("  Plik 6 gotowy ✅")
    print("="*50)
