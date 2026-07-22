import sqlite3
from datetime import datetime
import pytz
from polygon_api import PolygonAPI

CHICAGO_TZ = pytz.timezone('America/Chicago')
now = datetime.now(CHICAGO_TZ)
p   = PolygonAPI()

conn = sqlite3.connect('scanner.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute('''
SELECT id, ticker, timestamp, price, verdict, outcome_1h, outcome_4h, outcome_24h
FROM signals
WHERE (outcome_1h IS NULL OR outcome_4h IS NULL OR outcome_24h IS NULL)
AND timestamp > datetime('now', '-7 days', '+5 hours')
''')
rows = c.fetchall()
print(f'Rekordow do aktualizacji: {len(rows)}')

updated  = 0
skipped  = 0
price_cache = {}

for row in rows:
    ticker = row['ticker'].upper()
    entry  = float(row['price'] or 0)

    if entry <= 0:
        skipped += 1
        continue

    # Pomin warranty
    if any(ticker.endswith(s) for s in ('W','WW','R','U')) or '.WS' in ticker:
        skipped += 1
        continue

    # Cache cen
    if ticker not in price_cache:
        data  = p.get_ticker_details(ticker)
        price = float(data.get('price', 0) or 0)
        if price <= 0:
            price = float(data.get('prev_close', 0) or 0)
        price_cache[ticker] = price

    current_price = price_cache[ticker]
    if current_price <= 0:
        skipped += 1
        continue

    ts = row['timestamp']
    st = datetime.fromisoformat(ts)
    if st.tzinfo is None:
        st = st.replace(tzinfo=now.tzinfo)
    else:
        st = st.astimezone(now.tzinfo)
    elapsed_h  = (now - st).total_seconds() / 3600
    change_pct = ((current_price - entry) / entry) * 100
    updates    = {}

    if elapsed_h >= 1 and row['outcome_1h'] is None:
        updates['outcome_1h']    = round(change_pct, 2)
        updates['outcome_1h_at'] = now.isoformat()
    if elapsed_h >= 4 and row['outcome_4h'] is None:
        updates['outcome_4h']    = round(change_pct, 2)
        updates['outcome_4h_at'] = now.isoformat()
    if elapsed_h >= 24 and row['outcome_24h'] is None:
        updates['outcome_24h']    = round(change_pct, 2)
        updates['outcome_24h_at'] = now.isoformat()

    if updates:
        set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
        c.execute(
            f"UPDATE signals SET {set_clause} WHERE id = ?",
            list(updates.values()) + [row['id']]
        )
        updated += 1

conn.commit()
conn.close()
print(f'Zaktualizowano: {updated} | Pominieto: {skipped}')
print(f'Unikalne tickery z cena: {len(price_cache)}')
