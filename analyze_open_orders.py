#Bez kosztów — prosta analiza na podstawie danych które już masz. Sprawdź każdą pozycję pod kątem:


python3 -c "
from alpaca_trader import AlpacaPaperTrader
from polygon_api import PolygonAPI

t = AlpacaPaperTrader()
p = PolygonAPI()
positions = t.get_positions()

print('ANALIZA POZYCJI:')
print('='*70)

for pos in sorted(positions, key=lambda x: float(x.get('unrealized_plpc',0)), reverse=True):
    sym     = pos.get('symbol')
    entry   = float(pos.get('avg_entry_price', 0))
    current = float(pos.get('current_price', 0))
    pnl_pct = float(pos.get('unrealized_plpc', 0)) * 100
    pnl     = float(pos.get('unrealized_pl', 0))

    # Pobierz dane z Polygon
    data     = p.get_ticker_details(sym)
    price    = data.get('price', current)
    change   = data.get('change_pct', 0)
    vol_ratio = data.get('volume_ratio', 0)
    vwap     = data.get('vwap', 0)

    # Prosta ocena
    above_vwap = price > vwap if vwap else None
    momentum   = change > 0

    if pnl_pct > 5 and not momentum:
        rec = 'ZAMKNIJ - zysk ale momentum slabnie'
    elif pnl_pct > 10:
        rec = 'ZAMKNIJ - duzy zysk, realizuj'
    elif pnl_pct < -5:
        rec = 'ZAMKNIJ - stop loss'
    elif pnl_pct > 0 and momentum and above_vwap:
        rec = 'TRZYMAJ - trend nadal bullish'
    elif pnl_pct > 0:
        rec = 'WATCH - zysk ale slaby momentum'
    else:
        rec = 'TRZYMAJ lub ZAMKNIJ - ocen recznie'

    print(f'{sym:6s} | {pnl_pct:+.1f}% | zmiana {change:+.1f}% | vol {vol_ratio:.1f}x | VWAP {\"powyzej\" if above_vwap else \"ponizej\" if above_vwap==False else \"?\"} | {rec}')
"