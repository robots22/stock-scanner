from alpaca_trader import AlpacaPaperTrader

t = AlpacaPaperTrader()

# Sprawdz wszystkie otwarte orders
orders = t._get('/v2/orders?status=open&limit=100')
if orders:
    print(f'Otwarte orders: {len(orders)}')
    for o in orders:
        print(f"  {o.get('symbol'):6s} | {o.get('type'):20s} | qty: {o.get('qty')} | side: {o.get('side')}")
else:
    print('Brak otwartych orders')

print()

# Sprawdz pozycje
for sym in ['COYA', 'CANF', 'RCAX']:
    pos = t.get_position(sym)
    if pos:
        print(f"{sym}: qty={pos.get('qty')} available={pos.get('qty_available')} entry=${float(pos.get('avg_entry_price',0)):.2f}")
    else:
        print(f"{sym}: brak pozycji")
