#!/usr/bin/env python3
"""
STOCK SCANNER - NEWS RSS MODULE
Zapisz jako news_rss.py w folderze stock-scanner

Zadanie:
  Real-time news z GlobeNewswire RSS + SEC EDGAR RSS
  Uzupelnia Polygon news ktory ma 30-90 min opoznienie

  GlobeNewswire: wiekszosc nano/micro-cap PR
  SEC EDGAR:     8-K filings (M&A, FDA, material events)

  Odpytuje co 2 minuty (zamiast co 5 min jak Polygon)
  Cache ostatnich 200 artykulow zeby unikac duplikatow

Historia:
  v1.0 - GlobeNewswire + SEC EDGAR RSS
"""

import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from collections import deque
import re

from config import logger, now_chicago
from pre_filter import CATALYST_HIGH, CATALYST_MEDIUM, CATALYST_NEGATIVE

# ==================== RSS FEEDS ====================

GNW_FEEDS = [
    # Public Companies - wszystkie PR spolki publicznych
    'https://www.globenewswire.com/RssFeed/orgclass/1',
    # Mergers & Acquisitions
    'https://www.globenewswire.com/RssFeed/subjectcode/27-Mergers%20and%20Acquisitions',
    # Clinical Trials
    'https://www.globenewswire.com/RssFeed/subjectcode/8-Clinical%20Trials%20and%20Medical%20Discoveries',
    # Earnings
    'https://www.globenewswire.com/RssFeed/subjectcode/13-Earnings%20Releases%20and%20Operating%20Results',
]

SEC_FEEDS = [
    # 8-K filings (material events: M&A, FDA, contracts)
    'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom',
    # S-1 (IPO/offerings)
    'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-1&dateb=&owner=include&count=20&search_text=&output=atom',
]

# Ticker pattern w tekscie
TICKER_PATTERN = re.compile(r'\b(?:NASDAQ|NYSE|OTC):\s*([A-Z]{1,6})\b')
TICKER_PARENS  = re.compile(r'\((?:NASDAQ|NYSE|OTC)[:\s]*([A-Z]{1,6})\)')

# Jak stary news jest ignorowany (godziny)
MAX_AGE_HOURS = 4


class NewsRSSScanner:
    """
    Skanuje GlobeNewswire i SEC EDGAR RSS co 2 minuty.
    Wykrywa ticker + catalyst quality.
    Zwraca {ticker: [articles]} jak Polygon get_recent_news_tickers().
    """

    def __init__(self):
        self._seen_ids    = deque(maxlen=500)  # cache ID juz widzianych
        self._cache       = {}                  # {ticker: [articles]}
        self._last_scan   = None
        self._scan_count  = 0
        self.session      = requests.Session()
        self.session.headers.update({
            'User-Agent': 'StockScanner/1.0 (research purposes)'
        })
        logger.info("NewsRSSScanner zainicjowany (GlobeNewswire + SEC EDGAR)")

    def _fetch_rss(self, url, timeout=10):
        """Pobierz i sparsuj RSS/Atom feed."""
        try:
            r = self.session.get(url, timeout=timeout)
            if r.status_code != 200:
                logger.debug(f"RSS {url[:50]}: status {r.status_code}")
                return []
            return self._parse_feed(r.text, url)
        except Exception as e:
            logger.debug(f"RSS fetch error {url[:50]}: {e}")
            return []

    def _parse_feed(self, xml_text, source_url):
        """Parsuj RSS lub Atom XML."""
        articles = []
        try:
            root = ET.fromstring(xml_text)
            ns   = {'atom': 'http://www.w3.org/2005/Atom'}

            # Atom format (SEC EDGAR)
            entries = root.findall('.//atom:entry', ns)
            if not entries:
                entries = root.findall('.//entry')

            # RSS format (GlobeNewswire)
            if not entries:
                entries = root.findall('.//item')

            for entry in entries:
                # ID / GUID
                art_id = (
                    self._tag(entry, 'id', ns) or
                    self._tag(entry, 'guid') or
                    self._tag(entry, 'link', ns) or
                    ''
                )

                if art_id in self._seen_ids:
                    continue

                # Tytul
                title = (
                    self._tag(entry, 'title', ns) or
                    self._tag(entry, 'title') or ''
                )

                # Opis
                desc = (
                    self._tag(entry, 'summary', ns) or
                    self._tag(entry, 'description') or
                    self._tag(entry, 'content', ns) or ''
                )

                # Data publikacji
                pub = (
                    self._tag(entry, 'published', ns) or
                    self._tag(entry, 'updated', ns) or
                    self._tag(entry, 'pubDate') or ''
                )

                # Link
                link = self._tag(entry, 'link', ns) or self._tag(entry, 'link') or ''
                if not link:
                    link_el = entry.find('atom:link', ns)
                    if link_el is not None:
                        link = link_el.get('href', '')

                pub_dt = self._parse_date(pub)
                if pub_dt is None:
                    continue

                # Sprawdz wiek
                age_h = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
                if age_h > MAX_AGE_HOURS:
                    continue

                # Wyciagnij ticker
                tickers = self._extract_tickers(title + ' ' + desc)
                if not tickers:
                    continue

                article = {
                    'title':         title,
                    'description':   desc[:500],
                    'published_utc': pub_dt.isoformat(),
                    'article_url':   link,
                    'tickers':       tickers,
                    'publisher':     {'name': self._source_name(source_url)},
                    'insights':      self._get_insights(title, desc),
                    '_age_h':        age_h,
                    '_rss_id':       art_id,
                }

                articles.append(article)
                if art_id:
                    self._seen_ids.append(art_id)

        except ET.ParseError as e:
            logger.debug(f"RSS parse error: {e}")

        return articles

    def _tag(self, el, tag, ns=None):
        """Bezpieczne pobieranie tekstu z elementu XML."""
        try:
            if ns:
                found = el.find(f'atom:{tag}', ns)
            else:
                found = el.find(tag)
            if found is not None and found.text:
                return found.text.strip()
        except Exception:
            pass
        return None

    def _parse_date(self, date_str):
        """Parsuj date w roznych formatach."""
        if not date_str:
            return None
        formats = [
            '%Y-%m-%dT%H:%M:%S%z',
            '%Y-%m-%dT%H:%M:%SZ',
            '%Y-%m-%dT%H:%M:%S.%f%z',
            '%a, %d %b %Y %H:%M:%S %z',
            '%a, %d %b %Y %H:%M:%S GMT',
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str.strip(), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
        return None

    def _extract_tickers(self, text):
        """Wyciagnij ticker symbole z tekstu."""
        tickers = set()

        # (NASDAQ: CELZ), (NYSE: MBLY) etc
        for m in TICKER_PARENS.finditer(text):
            t = m.group(1).upper()
            if 1 <= len(t) <= 6:
                tickers.add(t)

        # NASDAQ: CELZ bez nawiasow
        for m in TICKER_PATTERN.finditer(text):
            t = m.group(1).upper()
            if 1 <= len(t) <= 6:
                tickers.add(t)

        return list(tickers)

    def _get_insights(self, title, desc):
        """Prosta analiza sentimentu na podstawie catalyst keywords."""
        text = (title + ' ' + desc).lower()

        if any(w in text for w in CATALYST_NEGATIVE):
            sentiment = 'negative'
        elif any(w in text for w in CATALYST_HIGH):
            sentiment = 'positive'
        elif any(w in text for w in CATALYST_MEDIUM):
            sentiment = 'positive'
        else:
            sentiment = 'neutral'

        return [{'sentiment': sentiment, 'sentiment_reasoning': 'RSS keyword match'}]

    def _source_name(self, url):
        """Nazwa zrodla na podstawie URL."""
        if 'globenewswire' in url:
            return 'globenewswire'
        if 'sec.gov' in url:
            return 'sec.gov'
        return 'rss'

    def scan(self):
        """
        Glowna funkcja — pobierz wszystkie RSS feeds.
        Zwraca {ticker: [articles]} — ten sam format co Polygon.
        """
        news_by_ticker = {}
        total_articles = 0
        new_articles   = 0

        # GlobeNewswire feeds
        for url in GNW_FEEDS:
            articles = self._fetch_rss(url)
            for article in articles:
                for ticker in article.get('tickers', []):
                    if ticker not in news_by_ticker:
                        news_by_ticker[ticker] = []
                    # Unikaj duplikatow per ticker
                    existing_ids = [a.get('_rss_id') for a in news_by_ticker[ticker]]
                    if article.get('_rss_id') not in existing_ids:
                        news_by_ticker[ticker].append(article)
                        new_articles += 1
                total_articles += 1

        # SEC EDGAR feeds
        for url in SEC_FEEDS:
            articles = self._fetch_rss(url)
            for article in articles:
                for ticker in article.get('tickers', []):
                    if ticker not in news_by_ticker:
                        news_by_ticker[ticker] = []
                    existing_ids = [a.get('_rss_id') for a in news_by_ticker[ticker]]
                    if article.get('_rss_id') not in existing_ids:
                        news_by_ticker[ticker].append(article)
                        new_articles += 1
                total_articles += 1

        # Sortuj artykuly per ticker po dacie (najnowsze pierwsze)
        for ticker in news_by_ticker:
            news_by_ticker[ticker].sort(
                key=lambda x: x.get('published_utc', ''),
                reverse=True
            )

        self._cache      = news_by_ticker
        self._last_scan  = now_chicago()
        self._scan_count += 1

        if news_by_ticker:
            logger.info(
                f"RSS scan: {new_articles} nowych artykulow "
                f"-> {len(news_by_ticker)} tickerow "
                f"(GNW + SEC EDGAR)"
            )

        return news_by_ticker

    def get_cached(self):
        """Zwroc cache z ostatniego scanu."""
        return self._cache

    def get_stats(self):
        return {
            'scan_count':    self._scan_count,
            'last_scan':     self._last_scan.strftime('%H:%M:%S') if self._last_scan else None,
            'cached_tickers': len(self._cache),
            'seen_ids':      len(self._seen_ids),
        }


if __name__ == "__main__":
    scanner = NewsRSSScanner()
    print("Skanuje RSS feeds...")
    news = scanner.scan()
    print(f"\nZnaleziono {len(news)} tickerow z newsami:")
    for ticker, articles in sorted(news.items())[:20]:
        for a in articles[:1]:
            age = a.get('_age_h', 0)
            print(f"  {ticker:6s} ({age:.1f}h) | {a['title'][:70]}")
    print(f"\nStats: {scanner.get_stats()}")
