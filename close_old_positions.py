import sqlite3
from alpaca_trader import AlpacaPaperTrader
from config import now_chicago

t = AlpacaPaperTrader()
positions = t.get_positions()
print(f'Otwarte pozycje: {len(positions)}')

# Sprawdz ktore tickery maja dzisiejszy BUY w bazie
today = now_chicago().strftime('%Y-%m-%d')
conn = sqlite3.connect('scanner.db')
c = conn.cursor()
c.execute('''
    SELECT DISTINCT ticker FROM signals
    WHERE verdict='BUY' AND DATE(timestamp) = ?
''', (today,))
today_tickers = set(row[0] for row in c.fetchall())
conn.close()

print(f'Tickery z dzisiejszym BUY: {today_tickers}')
print()

closed = 0
kept   = 0

for p in positions:
    sym = p.get('symbol')
    pnl = float(p.get('unrealized_pl', 0))
    pnl_pct = float(p.get('unrealized_plpc', 0)) * 100

    if sym in today_tickers:
        print(f'ZACHOWANO: {sym} ({pnl_pct:+.1f}%) - dzisiejsza pozycja')
        kept += 1
    else:
        result = t.sell(sym, reason='cleanup_old')
        print(f'ZAMKNIETO: {sym} ({pnl_pct:+.1f}%) PnL=${pnl:+.2f}')
        closed += 1

print(f'\nZamknieto: {closed} | Zachowano: {kept}')
