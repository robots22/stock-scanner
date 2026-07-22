from alpaca_trader import AlpacaPaperTrader

t = AlpacaPaperTrader()

positions = t.get_positions()
orders    = t._get('/v2/orders?status=open&limit=100') or []

# Tickery z trailing stop
has_trailing = {o.get('symbol') for o in orders if o.get('type') == 'trailing_stop'}

print(f'Pozycji: {len(positions)} | Trailing stops: {len(has_trailing)}')
print()

no_protection = []
for p in sorted(positions, key=lambda x: float(x.get('unrealized_plpc', 0)), reverse=True):
    sym       = p.get('symbol')
    qty       = p.get('qty')
    available = p.get('qty_available', qty)
    pnl_pct   = float(p.get('unrealized_plpc', 0)) * 100
    entry     = float(p.get('avg_entry_price', 0))
    current   = float(p.get('current_price', 0))
    protected = sym in has_trailing

    status = 'OK  ' if protected else 'BRAK'
    print(f'{status} | {sym:6s} | {pnl_pct:+.1f}% | entry ${entry:.2f} -> ${current:.2f} | qty {qty} avail {available}')

    if not protected and int(float(available or 0)) > 0:
        no_protection.append((sym, str(int(float(available)))))

print()
if no_protection:
    print(f'Brak trailing stop ({len(no_protection)} pozycji) - skladam orders...')
    for sym, qty in no_protection:
        print(f'  -> {sym} qty={qty}')
        t._submit_trailing_stop(sym, qty, '4.0')
else:
    print('Wszystkie pozycje maja trailing stop!')
