#!/usr/bin/env python3
"""
DEDUP HISTORY — jednorazowe czyszczenie bazy scanner.db

Reguly:
  MOMENTUM: pierwszy alert per ticker/dzien
  BUY:      pierwszy BUY per ticker/dzien
  WATCH/AVOID: pierwszy per ticker/dzien PLUS zapisy gdzie
              verdict zmienil sie od poprzedniego zapisu tego tickera
              (WATCH -> AVOID = zachowaj, WATCH -> WATCH = usun)

Zabezpieczenie:
  Backup scanner.db -> scanner.db.pre_dedup_YYYYMMDD_HHMMSS.bak
  Rollback: cp scanner.db.pre_dedup_... scanner.db

Uruchomienie:
  python3 dedup_history.py               # dry-run (pokazuje statystyki)
  python3 dedup_history.py --execute     # faktycznie usuwa
"""

import sqlite3
import shutil
import sys
from datetime import datetime

DB = 'scanner.db'


def analyze():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    print("=" * 65)
    print("DEDUP ANALIZA (dry-run)")
    print("=" * 65)

    # Baseline
    c.execute("SELECT verdict, COUNT(*) FROM signals GROUP BY verdict")
    print("\nStan przed:")
    for r in c.fetchall():
        print(f"  {r[0]:9s}: {r[1]:5d}")

    total_to_delete = 0

    # === MOMENTUM: zostaw tylko pierwszy per ticker/dzien ===
    c.execute('''
        SELECT COUNT(*) FROM signals s1
        WHERE verdict='MOMENTUM'
        AND EXISTS (
            SELECT 1 FROM signals s2
            WHERE s2.verdict='MOMENTUM'
            AND s2.ticker=s1.ticker
            AND DATE(s2.timestamp)=DATE(s1.timestamp)
            AND s2.timestamp < s1.timestamp
        )
    ''')
    mom_del = c.fetchone()[0]
    total_to_delete += mom_del
    print(f"\nMOMENTUM do usuniecia: {mom_del}")

    # === BUY: zostaw tylko pierwszy per ticker/dzien ===
    c.execute('''
        SELECT COUNT(*) FROM signals s1
        WHERE verdict='BUY'
        AND EXISTS (
            SELECT 1 FROM signals s2
            WHERE s2.verdict='BUY'
            AND s2.ticker=s1.ticker
            AND DATE(s2.timestamp)=DATE(s1.timestamp)
            AND s2.timestamp < s1.timestamp
        )
    ''')
    buy_del = c.fetchone()[0]
    total_to_delete += buy_del
    print(f"BUY do usuniecia: {buy_del}")

    # === WATCH/AVOID: usun jesli nie ma zmiany verdict wzgledem POPRZEDNIEGO
    #                  zapisu tego tickera w tym dniu ===
    #
    # Implementacja: dla kazdego WATCH/AVOID zostaw wpis TYLKO jesli
    #   - jest pierwszy tego dnia dla tego tickera, LUB
    #   - poprzedni zapis (dowolnego verdict) tego tickera tego dnia
    #     mial INNY verdict niz obecny
    c.execute('''
        WITH prev AS (
            SELECT s.id, s.ticker, s.verdict, s.timestamp,
                   (SELECT verdict FROM signals p
                    WHERE p.ticker=s.ticker
                    AND DATE(p.timestamp)=DATE(s.timestamp)
                    AND p.timestamp < s.timestamp
                    ORDER BY p.timestamp DESC LIMIT 1) as prev_verdict
            FROM signals s
            WHERE s.verdict IN ('WATCH','AVOID')
        )
        SELECT COUNT(*) FROM prev
        WHERE prev_verdict IS NOT NULL
        AND prev_verdict = verdict
    ''')
    wa_del = c.fetchone()[0]
    total_to_delete += wa_del
    print(f"WATCH/AVOID do usuniecia (bez zmiany verdict): {wa_del}")

    c.execute("SELECT COUNT(*) FROM signals")
    total = c.fetchone()[0]
    print(f"\nRAZEM do usuniecia: {total_to_delete} / {total} ({total_to_delete/total*100:.1f}%)")
    print(f"Zostanie: {total - total_to_delete} wpisow")

    conn.close()
    return total_to_delete


def execute():
    # Backup
    stamp   = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup  = f'{DB}.pre_dedup_{stamp}.bak'
    print(f"\nBackup: {DB} -> {backup}")
    shutil.copy2(DB, backup)

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    total_deleted = 0

    # MOMENTUM
    c.execute('''
        DELETE FROM signals
        WHERE verdict='MOMENTUM'
        AND id IN (
            SELECT s1.id FROM signals s1
            WHERE s1.verdict='MOMENTUM'
            AND EXISTS (
                SELECT 1 FROM signals s2
                WHERE s2.verdict='MOMENTUM'
                AND s2.ticker=s1.ticker
                AND DATE(s2.timestamp)=DATE(s1.timestamp)
                AND s2.timestamp < s1.timestamp
            )
        )
    ''')
    n = c.rowcount
    total_deleted += n
    print(f"MOMENTUM usunieto: {n}")

    # BUY
    c.execute('''
        DELETE FROM signals
        WHERE verdict='BUY'
        AND id IN (
            SELECT s1.id FROM signals s1
            WHERE s1.verdict='BUY'
            AND EXISTS (
                SELECT 1 FROM signals s2
                WHERE s2.verdict='BUY'
                AND s2.ticker=s1.ticker
                AND DATE(s2.timestamp)=DATE(s1.timestamp)
                AND s2.timestamp < s1.timestamp
            )
        )
    ''')
    n = c.rowcount
    total_deleted += n
    print(f"BUY usunieto: {n}")

    # WATCH/AVOID - bez zmiany verdict
    c.execute('''
        DELETE FROM signals
        WHERE id IN (
            SELECT s.id FROM signals s
            WHERE s.verdict IN ('WATCH','AVOID')
            AND (SELECT verdict FROM signals p
                 WHERE p.ticker=s.ticker
                 AND DATE(p.timestamp)=DATE(s.timestamp)
                 AND p.timestamp < s.timestamp
                 ORDER BY p.timestamp DESC LIMIT 1) = s.verdict
        )
    ''')
    n = c.rowcount
    total_deleted += n
    print(f"WATCH/AVOID usunieto: {n}")

    conn.commit()

    c.execute("SELECT COUNT(*) FROM signals")
    remaining = c.fetchone()[0]

    print(f"\n[OK] Usunieto lacznie: {total_deleted}")
    print(f"     Zostalo w bazie:   {remaining}")
    print(f"     Backup:            {backup}")
    print(f"\nROLLBACK: cp {backup} {DB}")

    # VACUUM zeby fizycznie odzyskac miejsce
    print("\nVACUUM (kompresja bazy)...")
    conn.execute("VACUUM")
    conn.close()
    print("[OK] Gotowe")


if __name__ == '__main__':
    if '--execute' in sys.argv:
        analyze()
        print("\n" + "!" * 65)
        confirm = input("Czy usunac? (wpisz TAK aby potwierdzic): ")
        if confirm.strip() == 'TAK':
            execute()
        else:
            print("Anulowano")
    else:
        analyze()
        print("\nAby wykonac usuniecie:")
        print("  python3 dedup_history.py --execute")
