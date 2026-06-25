#!/usr/bin/env python3
"""
STOCK SCANNER - PLIK 6: TELEGRAM ALERTY
"""

import requests
from config import (logger, CONFIG, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                    TELEGRAM_CHAT_IDS, now_chicago)


def send_message(text, parse_mode='HTML'):
    if not CONFIG['telegram_enabled']:
        print("\n" + "-"*50)
        print("TELEGRAM (DEMO):")
        print(text)
        print("-"*50)
        return True

    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    success = False

    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            response = requests.post(
                url,
                data={'chat_id': chat_id, 'text': text[:4096], 'parse_mode': parse_mode},
                timeout=10
            )
            if response.status_code == 200:
                success = True
            else:
                logger.error(f"Telegram blad {chat_id}: {response.status_code}")
        except Exception as e:
            logger.error(f"Telegram wyjatek {chat_id}: {e}")

    if success:
        logger.info(f"Telegram: wyslano na {len(TELEGRAM_CHAT_IDS)} chat(y)")
    return success


def _build_buy_message(ticker, confidence_str, time_str, price, change_pct,
                        volume, volume_ratio, stop_loss, take_profit, rr_ratio,
                        risk_pct, reward_pct, sl_basis, reasons, just, risk):
    basis_label = {'vwap': 'VWAP', 'atr': 'ATR', 'pct_4': '4%'}.get(sl_basis or '', '4%')

    if stop_loss and take_profit and risk_pct and reward_pct:
        sl_line = chr(128721) + ' STOP LOSS:   <b>$' + '{:.2f}'.format(stop_loss) + '</b> (-' + '{:.1f}'.format(risk_pct) + '%) [' + basis_label + ']'
        tp_line = chr(127919) + ' TAKE PROFIT: <b>$' + '{:.2f}'.format(take_profit) + '</b> (+' + '{:.1f}'.format(reward_pct) + '%)'
        rr_line = chr(9878) + ' R/R:         ' + '{:.1f}'.format(rr_ratio or 2.0) + ':1'
    else:
        sl_line = chr(128721) + ' STOP LOSS:   <b>$' + '{:.2f}'.format(price * 0.96) + '</b> (-4%) [4%]'
        tp_line = chr(127919) + ' TAKE PROFIT: <b>$' + '{:.2f}'.format(price * 1.08) + '</b> (+8%)'
        rr_line = chr(9878) + ' R/R:         2.0:1'

    just_short = '. '.join(just.split('. ')[:2]) + '.' if just else ''
    reasons_line = ' | '.join((reasons or [])[:2]) or '-'

    lines = [
        chr(128994) + ' <b>' + ticker + '</b> BUY ' + confidence_str,
        chr(9200) + ' ' + time_str,
        '',
        chr(128176) + ' ENTRY: <b>$' + '{:.2f}'.format(price) + '</b> (' + '{:+.1f}'.format(change_pct) + '%)',
        chr(128202) + ' Vol: ' + '{:,}'.format(volume) + ' (' + '{:.1f}'.format(volume_ratio) + 'x)',
        sl_line,
        tp_line,
        rr_line,
        '',
        chr(128203) + ' ' + reasons_line,
        '',
        chr(129300) + ' ' + just_short,
        '',
        chr(9888) + ' ' + (risk or ''),
    ]
    return chr(10).join(lines)


def alert_signal(result, ticker_data):
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

    confidence_str = {'WYSOKA': chr(11088)*3, 'SREDNIA': chr(11088)*2, 'NISKA': chr(11088)}.get(confidence, chr(11088))
    time_str = now_chicago().strftime('%H:%M CST')

    stop_loss   = result.get('stop_loss')
    take_profit = result.get('take_profit')
    rr_ratio    = result.get('rr_ratio', 2.0)
    risk_pct    = result.get('risk_pct')
    reward_pct  = result.get('reward_pct')
    sl_basis    = result.get('sl_basis', '')

    if verdict == 'BUY':
        message = _build_buy_message(
            ticker, confidence_str, time_str, price, change_pct,
            volume, volume_ratio, stop_loss, take_profit, rr_ratio,
            risk_pct, reward_pct, sl_basis, reasons, just, risk
        )
        return send_message(message)

    elif verdict == 'WATCH' and confidence == 'WYSOKA':
        just_short   = '. '.join(just.split('. ')[:2]) + '.' if just else ''
        reasons_line = ' | '.join((reasons or [])[:2]) or '-'
        lines = [
            chr(128993) + ' <b>' + ticker + '</b> WATCH ' + confidence_str,
            chr(9200) + ' ' + time_str,
            '',
            '$' + '{:.2f}'.format(price) + ' (' + '{:+.1f}'.format(change_pct) + '%) | Vol ' + '{:.1f}'.format(volume_ratio) + 'x',
            '',
            chr(128203) + ' ' + reasons_line,
            '',
            chr(129300) + ' ' + just_short,
        ]
        return send_message(chr(10).join(lines))

    else:
        logger.info(f"Telegram: pominieto {verdict} {confidence} dla {ticker}")
        return False


def alert_manual(result, ticker_data):
    """Alert dla /analyze - wysyla zawsze niezaleznie od werdyktu."""
    verdict    = result.get('verdict', 'WATCH')
    ticker     = result.get('ticker', 'UNKNOWN')
    confidence = result.get('confidence', '')
    just       = result.get('justification', '')
    risk       = result.get('risk', '')

    price        = ticker_data.get('price', 0)
    change_pct   = ticker_data.get('change_pct', 0)
    volume_ratio = ticker_data.get('volume_ratio', 1.0)
    reasons      = ticker_data.get('reasons', [])

    icon = {'BUY': chr(128994), 'WATCH': chr(128993), 'AVOID': chr(128308)}.get(verdict, chr(9898))
    confidence_str = {'WYSOKA': chr(11088)*3, 'SREDNIA': chr(11088)*2, 'NISKA': chr(11088)}.get(confidence, chr(11088))
    time_str   = now_chicago().strftime('%H:%M CST')
    just_short = '. '.join(just.split('. ')[:2]) + '.' if just else '-'
    reasons_line = ' | '.join((reasons or [])[:2]) or '-'

    stop_loss   = result.get('stop_loss')
    take_profit = result.get('take_profit')
    sl_basis    = result.get('sl_basis', '')
    basis_label = {'vwap': 'VWAP', 'atr': 'ATR', 'pct_4': '4%'}.get(sl_basis or '', '4%')

    lines = [
        icon + ' <b>' + ticker + '</b> ' + verdict + ' ' + confidence_str + ' [/analyze]',
        chr(9200) + ' ' + time_str,
        '',
        chr(128176) + ' $' + '{:.2f}'.format(price) + ' (' + '{:+.1f}'.format(change_pct) + '%) | Vol ' + '{:.1f}'.format(volume_ratio) + 'x',
    ]

    if verdict == 'BUY' and stop_loss and take_profit:
        risk_pct   = result.get('risk_pct', 0)
        reward_pct = result.get('reward_pct', 0)
        lines += [
            chr(128721) + ' SL: $' + '{:.2f}'.format(stop_loss) + ' (-' + '{:.1f}'.format(risk_pct) + '%) [' + basis_label + ']',
            chr(127919) + ' TP: $' + '{:.2f}'.format(take_profit) + ' (+' + '{:.1f}'.format(reward_pct) + '%)',
        ]

    lines += [
        '',
        chr(128203) + ' ' + reasons_line,
        '',
        chr(129300) + ' ' + just_short,
    ]

    if risk:
        lines += ['', chr(9888) + ' ' + risk]

    return send_message(chr(10).join(lines))


def alert_retrigger(ticker, trigger, details, old_verdict, new_verdict,
                    current_price, entry_price):
    price_change = ((current_price - entry_price) / entry_price) * 100

    trigger_labels = {
        'STOP_LOSS':        'Stop-loss osiagniety',
        'VOLUME_DROP':      'Wolumen slabnie',
        'PRICE_REVERSAL':   'Cena sie cofa',
        'TAKE_PROFIT':      'Take profit osiagniety',
        'DARKPOOL_SELL':    'Dark pool SELL',
        'OPTIONS_BEARISH':  'Options flow bearish',
        'UW_ACTIVITY_GONE': 'Aktywnosc UW zniknela',
    }
    label    = trigger_labels.get(trigger, trigger)
    time_str = now_chicago().strftime('%H:%M CST')
    dash     = chr(8212)

    if trigger == 'STOP_LOSS' or (old_verdict == 'BUY' and new_verdict == 'AVOID'):
        result_str     = 'ZYSK +' + '{:.1f}'.format(price_change) + '%' if price_change > 0 else 'STRATA ' + '{:.1f}'.format(price_change) + '%'
        trigger_emoji  = chr(128721) if trigger == 'STOP_LOSS' else chr(128308)
        lines = [
            trigger_emoji + ' <b>SELL ' + dash + ' ' + ticker + '</b>',
            chr(9200) + ' ' + time_str,
            '',
            chr(128176) + ' ENTRY: $' + '{:.2f}'.format(entry_price),
            chr(128176) + ' EXIT:  $' + '{:.2f}'.format(current_price) + ' (' + '{:+.1f}'.format(price_change) + '%)',
            chr(128202) + ' ' + result_str,
            '',
            chr(128203) + ' Trigger: ' + label,
            chr(9888) + ' ' + details,
        ]
        return send_message(chr(10).join(lines))

    elif trigger == 'TAKE_PROFIT':
        lines = [
            chr(128176) + ' <b>TAKE PROFIT ' + dash + ' ' + ticker + '</b>',
            chr(9200) + ' ' + time_str,
            '',
            'ENTRY: $' + '{:.2f}'.format(entry_price),
            'EXIT:  $' + '{:.2f}'.format(current_price),
            'ZYSK:  +' + '{:.1f}'.format(price_change) + '% ' + chr(127919),
            '',
            'Rozwaz realizacje zysku.',
        ]
        return send_message(chr(10).join(lines))

    else:
        trigger_icons = {
            'VOLUME_DROP':      chr(128201),
            'PRICE_REVERSAL':   chr(128315),
            'DARKPOOL_SELL':    chr(128011),
            'OPTIONS_BEARISH':  chr(128202),
            'UW_ACTIVITY_GONE': chr(128123),
        }
        icon    = trigger_icons.get(trigger, chr(9889))
        verdict_icon = {'BUY': chr(128994), 'WATCH': chr(128993), 'AVOID': chr(128308)}
        old_icon = verdict_icon.get(old_verdict, chr(9898))
        new_icon = verdict_icon.get(new_verdict, chr(9898)) if new_verdict else chr(128260)

        lines = [
            icon + ' <b>' + ticker + ' ' + dash + ' RE-ANALIZA</b>',
            'Trigger: ' + label,
            chr(9200) + ' ' + time_str,
            '',
            chr(128176) + ' $' + '{:.2f}'.format(current_price) + ' (' + '{:+.1f}'.format(price_change) + '% od sygnalu)',
            chr(128203) + ' ' + details,
            '',
            'Werdykt: ' + old_icon + ' ' + old_verdict + (' -> ' + new_icon + ' ' + new_verdict if new_verdict else ''),
        ]
        return send_message(chr(10).join(lines))


def alert_take_profit(ticker, entry_price, current_price, gain_pct):
    time_str = now_chicago().strftime('%H:%M CST')
    dash = chr(8212)
    lines = [
        chr(128176) + ' <b>TAKE PROFIT ' + dash + ' ' + ticker + '</b>',
        chr(9200) + ' ' + time_str,
        '',
        'ENTRY: $' + '{:.2f}'.format(entry_price),
        'EXIT:  $' + '{:.2f}'.format(current_price),
        'ZYSK:  +' + '{:.1f}'.format(gain_pct) + '% ' + chr(127919),
        '',
        'Rozwaz realizacje zysku.',
    ]
    return send_message(chr(10).join(lines))


def send_hourly_dashboard(stats, active_signals, top_today):
    time_str   = now_chicago().strftime('%H:%M CST')
    by_verdict = stats.get('by_verdict', {})
    avg        = stats.get('avg_outcomes_buy', {})

    acc_str = ''
    if avg.get('1h') is not None:
        acc_str += '\n  1h:  ' + '{:+.1f}'.format(avg['1h']) + '%'
    if avg.get('4h') is not None:
        acc_str += '\n  4h:  ' + '{:+.1f}'.format(avg['4h']) + '%'
    if avg.get('24h') is not None:
        acc_str += '\n  24h: ' + '{:+.1f}'.format(avg['24h']) + '%'

    lines = [
        chr(128202) + ' <b>DASHBOARD ' + chr(8212) + ' ' + time_str + '</b>',
        '',
        chr(128200) + ' Sygnaly dzis:',
        '  ' + chr(128994) + ' BUY:   ' + str(by_verdict.get('BUY', 0)),
        '  ' + chr(128993) + ' WATCH: ' + str(by_verdict.get('WATCH', 0)),
        '  ' + chr(128308) + ' AVOID: ' + str(by_verdict.get('AVOID', 0)),
        '  Lacznie: ' + str(stats.get('total_signals', 0)),
    ]

    if acc_str:
        lines += ['', chr(127919) + ' Sredni wynik BUY:' + acc_str]

    if active_signals:
        lines += ['', chr(128065) + ' Monitorowane (' + str(len(active_signals)) + '):']
        for sig in active_signals[:5]:
            lines.append('  ' + chr(8226) + ' ' + sig['ticker'] + ' @ $' + '{:.2f}'.format(sig['price']))

    if top_today:
        lines += ['', chr(127942) + ' Top sygnaly dzis:']
        for s in top_today[:3]:
            icon = chr(128994) if s.get('verdict') == 'BUY' else chr(128993) if s.get('verdict') == 'WATCH' else chr(128308)
            lines.append('  ' + icon + ' ' + s.get('ticker', '') + ' ' + chr(8212) + ' ' + s.get('verdict', '') + ' @ $' + '{:.2f}'.format(s.get('price', 0)))

    return send_message(chr(10).join(lines))


def send_startup_message(demo_mode=True):
    time_str = now_chicago().strftime('%Y-%m-%d %H:%M CST')
    mode_str = chr(128300) + ' DEMO' if demo_mode else chr(128640) + ' LIVE'
    lines = [
        chr(9989) + ' <b>STOCK SCANNER uruchomiony</b>',
        'Tryb: ' + mode_str,
        'Czas: ' + time_str,
        '',
        'Parametry:',
        '  ' + chr(8226) + ' Cena: $0.01 ' + chr(8212) + ' $15.00',
        '  ' + chr(8226) + ' Min wolumen: 100,000',
        '  ' + chr(8226) + ' Cykl: co 5 min (UW: co 1 min)',
        '  ' + chr(8226) + ' TOP 3 tickerow ' + chr(8594) + ' Claude AI',
    ]
    return send_message(chr(10).join(lines))


def send_shutdown_message(stats):
    time_str   = now_chicago().strftime('%H:%M CST')
    by_verdict = stats.get('by_verdict', {})
    lines = [
        chr(128721) + ' <b>STOCK SCANNER zatrzymany</b>',
        'Czas: ' + time_str,
        '',
        'Sesja:',
        '  ' + chr(128994) + ' BUY:   ' + str(by_verdict.get('BUY', 0)),
        '  ' + chr(128993) + ' WATCH: ' + str(by_verdict.get('WATCH', 0)),
        '  ' + chr(128308) + ' AVOID: ' + str(by_verdict.get('AVOID', 0)),
        '  Lacznie: ' + str(stats.get('total_signals', 0)),
    ]
    return send_message(chr(10).join(lines))


if __name__ == "__main__":
    print("Telegram Alerty OK")
