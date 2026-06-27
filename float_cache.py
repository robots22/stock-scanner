#!/usr/bin/env python3
"""
STOCK SCANNER - FLOAT CACHE
Zapisz jako float_cache.py w folderze stock-scanner

Zadanie:
  Przechowuje i zarządza danymi float (shares outstanding)
  dla tickerów small-cap.

  Float zmienia się rzadko (reverse splits, offerings)
  więc cache-ujemy na 7 dni.

  Strategia pobierania:
  - Pobierz float dla tickerów z vol > 2x (priorytet)
  - Cache persystowany w pliku JSON
  - Max 50 API calls per sesja

Historia zmian:
  v1.0 - pierwsza wersja
"""

import json
import os
from datetime import datetime, timedelta
import pytz

from config import logger, CONFIG, now_chicago

CACHE_FILE    = 'float_cache.json'
CACHE_TTL_DAYS = 7
MAX_FETCH_PER_SESSION = 50

CHICAGO_TZ = pytz.timezone('America/Chicago')


class FloatCache:
    """
    Cache dla danych float tickerów.
    Persystowany na dysku — przeżywa restarty systemu.
    """

    def __init__(self, polygon_api):
        self.polygon    = polygon_api
        self._cache     = {}   # {ticker: {'float': N, 'updated': iso_str}}
        self._fetched_today = set()
        self._load()

    def _load(self):
        """Wczytaj cache z dysku."""
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, 'r') as f:
                    self._cache = json.load(f)
                logger.info(f"FloatCache: wczytano {len(self._cache)} tickerow z {CACHE_FILE}")
        except Exception as e:
            logger.warning(f"FloatCache load error: {e}")
            self._cache = {}

    def _save(self):
        """Zapisz cache na dysk."""
        try:
            with open(CACHE_FILE, 'w') as f:
                json.dump(self._cache, f)
        except Exception as e:
            logger.warning(f"FloatCache save error: {e}")

    def _is_fresh(self, ticker, change_pct=None):
        """Czy cache dla tickera jest aktualny.
        
        TTL dynamiczny:
          Float < 5M + ruch > 30%  -> 1 dzien (ryzyko offering)
          Float < 5M               -> 3 dni
          Float 5-50M              -> 7 dni
          Float > 50M              -> 30 dni
        """
        entry = self._cache.get(ticker)
        if not entry:
            return False
        try:
            updated = datetime.fromisoformat(entry['updated'])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=CHICAGO_TZ)
            age_days = (now_chicago() - updated).total_seconds() / 86400

            float_shares = entry.get('float') or 0

            if float_shares < 5_000_000:
                if change_pct and abs(change_pct) > 30:
                    ttl = 1   # nano-cap po duzym ruchu = ryzyko offering
                else:
                    ttl = 3   # nano-cap normalny
            elif float_shares < 50_000_000:
                ttl = 7       # low-cap
            else:
                ttl = 30      # mid-cap

            return age_days < ttl
        except Exception:
            return False

    def get(self, ticker, change_pct=None):
        """Zwraca float dla tickera lub None."""
        entry = self._cache.get(ticker)
        if entry and self._is_fresh(ticker, change_pct):
            return entry.get('float')
        return None

    def _fetch_float(self, ticker):
        """Pobiera float z Polygon API."""
        try:
            data = self.polygon._get(
                f'/v3/reference/tickers/{ticker}',
                cache_ttl=86400,
            )
            if not data:
                return None
            results = data.get('results', data)
            if isinstance(results, dict):
                shares = (results.get('weighted_shares_outstanding') or
                          results.get('share_class_shares_outstanding'))
                if shares:
                    return int(shares)
        except Exception as e:
            logger.debug(f"Float fetch error {ticker}: {e}")
        return None

    def enrich_universe(self, universe):
        """
        Główna funkcja — dodaje float_shares do każdego tickera w universe.

        Strategia:
        1. Tickery z cache (świeżym) → od razu
        2. Tickery bez cache → pobierz (max 50/sesja, priorytet vol > 2x)
        3. Zapisz nowe dane do cache
        """
        # Podziel na: mamy cache vs potrzebujemy fetch
        need_fetch = []
        for t in universe:
            ticker = t.get('ticker', '')
            cached = self.get(ticker)
            if cached is not None:
                t['float_shares'] = cached
            elif ticker not in self._fetched_today:
                need_fetch.append(t)

        if not need_fetch:
            return

        # Sortuj po volume_ratio — najpierw najbardziej aktywne
        need_fetch.sort(
            key=lambda x: x.get('volume_ratio', 0),
            reverse=True
        )

        # Ogranicz do max 50 per sesja
        to_fetch = need_fetch[:MAX_FETCH_PER_SESSION]
        fetched  = 0

        for t in to_fetch:
            ticker = t.get('ticker', '')
            shares = self._fetch_float(ticker)

            self._fetched_today.add(ticker)

            if shares:
                t['float_shares'] = shares
                self._cache[ticker] = {
                    'float':   shares,
                    'updated': now_chicago().isoformat(),
                }
                fetched += 1
            else:
                # Zapisz ze float=None żeby nie odpytywać wielokrotnie
                self._cache[ticker] = {
                    'float':   None,
                    'updated': now_chicago().isoformat(),
                }

        if fetched > 0:
            self._save()
            logger.info(f"FloatCache: pobrano float dla {fetched} nowych tickerow "
                        f"({len(self._cache)} w cache)")

    def get_stats(self):
        known     = sum(1 for v in self._cache.values() if v.get('float'))
        unknown   = sum(1 for v in self._cache.values() if not v.get('float'))
        fresh     = sum(1 for t in self._cache if self._is_fresh(t))
        return {
            'total':   len(self._cache),
            'known':   known,
            'unknown': unknown,
            'fresh':   fresh,
        }
