#!/usr/bin/env python3
"""Analiza wynikow backtest_multi.json"""
import json, glob

files = sorted(glob.glob('backtest_multi_*.json'))
fname = files[-1]
print(f"Plik: {fname}\n")

with open(fname) as f:
    data = json.load(f)

print(f"Okres: {data['start']} -> {data['end']}")

def stats(vals):
    if not vals: return None
    wins = sum(1 for v in vals if v > 0)
    return {'n': len(vals), 'wr': wins/len(vals)*100,
            'avg': sum(vals)/len(vals), 'best': max(vals), 'worst': min(vals),
            'median': sorted(vals)[len(vals)//2]}

for strat, block in sorted(data['strategies'].items()):
    sigs = block['signals']
    print(f"\n{'='*70}")
    print(f"  {strat}  (n={block['n']})")
    print('='*70)

    for window in ['pnl_1h', 'pnl_4h', 'pnl_24h']:
        vals = [s[window] for s in sigs if s.get(window) is not None]
        st = stats(vals)
        if st:
            print(f"  {window:8s}: n={st['n']:4d} WR {st['wr']:5.1f}% | "
                  f"avg {st['avg']:+6.2f}% med {st['median']:+5.1f}% | "
                  f"best {st['best']:+.0f}% worst {st['worst']:+.0f}%")

    sl = sigs[0]['sl_pct']; tp = sigs[0]['tp_pct']
    total, wins, trades, stopped = 0, 0, 0, 0
    for s in sigs:
        pnl4 = s.get('pnl_4h')
        if pnl4 is None: continue
        trades += 1
        if s.get('stopped'):
            total += sl; stopped += 1
        else:
            capped = min(pnl4, tp) if pnl4 > 0 else pnl4
            total += capped
            if capped > 0: wins += 1
    if trades:
        print(f"\n  SL/TP ({sl}%/{tp}%): trades {trades} wins {wins} ({wins/trades*100:.0f}%) "
              f"stopped {stopped} ({stopped/trades*100:.0f}%)")
        print(f"    Total: {total:+.0f}% | Avg/trade: {total/trades:+.2f}%")

    print(f"  Alt SL/TP:")
    for asl, atp in [(-10,10),(-15,20),(-20,30),(-8,12)]:
        t2,w2,tr2 = 0,0,0
        for s in sigs:
            pnl4 = s.get('pnl_4h'); mae = s.get('mae_4h',0)
            if pnl4 is None: continue
            tr2 += 1
            if mae >= abs(asl):
                t2 += asl
            else:
                capped = min(pnl4, atp) if pnl4>0 else pnl4
                t2 += capped
                if capped>0: w2+=1
        if tr2:
            print(f"    SL{asl}/TP{atp}: WR {w2/tr2*100:4.0f}% avg {t2/tr2:+5.2f}% total {t2:+.0f}%")

    days = set(s['day'] for s in sigs)
    print(f"  Freq: {len(sigs)/len(days):.1f}/dzien")
    top = sorted([s for s in sigs if s.get('pnl_4h') is not None], key=lambda x: x['pnl_4h'], reverse=True)[:5]
    print(f"  TOP5: " + ", ".join(f"{s['ticker']}{s['pnl_4h']:+.0f}%" for s in top))
    wr5 = sorted([s for s in sigs if s.get('pnl_4h') is not None], key=lambda x: x['pnl_4h'])[:5]
    print(f"  WORST5: " + ", ".join(f"{s['ticker']}{s['pnl_4h']:+.0f}%" for s in wr5))
