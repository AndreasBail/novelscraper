#!/usr/bin/env python3
"""Core scraping logic: fetch, parse, and write to SQLite."""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import threading
from pathlib import Path

import httpx
from lxml import etree

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fwn.scrape")

DB_PATH = os.environ.get("DB_PATH", "/app/data/novels.db")
DELAY_BETWEEN_REQUESTS = float(os.environ.get("DELAY_BETWEEN_REQUESTS", "5"))
DELAY_PAGE_LIST = float(os.environ.get("DELAY_PAGE_LIST", "0.5"))
BASE = "https://freewebnovel.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

SCRAPE_BATCH_SIZE = int(os.environ.get("SCRAPE_BATCH_SIZE", "10"))
_scraping_lock = threading.Lock()
_scraping_novel = None
_scraping_progress = {"message": "", "percent": 0, "total": 0, "scraped": 0}

_local = threading.local()


def _get_db():
    if not getattr(_local, "db", None):
        _local.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.db.execute("PRAGMA journal_mode=WAL")
        _local.db.execute("PRAGMA busy_timeout=5000")
    return _local.db


def init_db():
    db = _get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS sources (
        url TEXT PRIMARY KEY, title TEXT DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS novels (
        url TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT DEFAULT '',
        cover TEXT, genres TEXT DEFAULT '', status TEXT DEFAULT '',
        last_scraped INTEGER, chapter_count INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS chapters (
        novel_url TEXT NOT NULL, chapter_url TEXT PRIMARY KEY,
        chapter_title TEXT NOT NULL, content TEXT DEFAULT '',
        scraped_at INTEGER NOT NULL, content_hash TEXT DEFAULT '',
        FOREIGN KEY(novel_url) REFERENCES novels(url))""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_cn ON chapters(novel_url,scraped_at)")
    db.commit()


def norm_url(url):
    return url.replace("www.freewebnovel.com", "freewebnovel.com", 1)


async def fetch(url, ref=None, retries=3):
    url = norm_url(url)
    if ref:
        ref = norm_url(ref)
    h = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
         "Accept-Language": "en-US,en;q=0.5"}
    if ref:
        h["Referer"] = ref
    for attempt in range(retries):
        try:
            r = httpx.get(url, headers=h, timeout=30.0, follow_redirects=True)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 500, 502, 503):
                wait = 2.0 * (2 ** attempt)
                log.warning("HTTP %d for %s, retry %.1fs", r.status_code, url, wait)
                await asyncio.sleep(wait)
        except Exception as e:
            log.warning("Error: %s", e)
            await asyncio.sleep(2.0 * (2 ** attempt))
    return None


# ---- Parsing ----

def parse_novel(html):
    t = etree.HTML(html)
    h1 = t.xpath("//h1//text()")
    title = "".join(h1).strip() if h1 else "Unknown"
    cover = t.xpath("//meta[@property='og:image']/@content")
    genres = t.xpath("//meta[@property='og:novel:genre']/@content")
    author = t.xpath("//meta[@property='og:novel:author']/@content")
    status = t.xpath("//meta[@property='og:novel:status']/@content")
    data_div = t.xpath("//div[@id='indexListPage']")
    total_chapters = int(data_div[0].get("data-total-chapters", 0)) if data_div else 0
    total_pages = int(data_div[0].get("data-total-page", 1)) if data_div else 1
    page_size = int(data_div[0].get("data-page-size", 40)) if data_div else 40
    return {
        "title": title, "cover": cover[0] if cover else None,
        "genres": genres[0] if genres else "", "author": author[0] if author else "",
        "status": status[0] if status else "",
        "total_chapters": total_chapters, "total_pages": total_pages,
        "page_size": page_size,
    }


def parse_chap_list(html):
    t = etree.HTML(html)
    chs = []
    for a in t.xpath("//ul[contains(@class,'ul-list5')]/li/a"):
        h = a.get("href", "")
        txt = "".join(a.itertext()).strip()
        if h and txt:
            chs.append({"url": h if h.startswith("http") else BASE + h, "title": txt})
    if not chs:
        for a in t.xpath("//a[contains(@href,'chapter')]"):
            h = a.get("href", "")
            txt = "".join(a.itertext()).strip()
            if h and txt:
                chs.append({"url": h if h.startswith("http") else BASE + h, "title": txt})
    return chs


def parse_chap_body(html):
    if not html:
        return None
    t = etree.HTML(html)
    ti = t.xpath("//span[@class='chapter']/text()")
    if not ti:
        ti = t.xpath("//div[contains(@class,'reader-page-title')]//text()")
    raw = ti[0].strip() if ti else "Untitled"
    title = re.sub(r'\s+', ' ', raw).strip()
    paras = t.xpath("//div[contains(@class,'m-read')]//p/text()")
    if not paras:
        paras = t.xpath("//div[contains(@class,'m-read')]//div[@class='chapter']//p/text()")
    content = "\n".join(p.strip() for p in paras if p.strip())
    return {"title": title, "content": content} if content else None


# ---- DB helpers ----

def upsert_novel(novel_url, meta):
    db = _get_db()
    db.execute("""INSERT INTO novels (url,title,author,cover,genres,status,last_scraped)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
        title=excluded.title,author=excluded.author,cover=excluded.cover,
        genres=excluded.genres,status=excluded.status,last_scraped=excluded.last_scraped""",
        (novel_url, meta["title"], meta["author"], meta["cover"],
         meta["genres"], meta["status"], int(time.time())))
    db.commit()


def _content_hash(content):
    return hashlib.md5(content.encode()).hexdigest()[:16]


def _chapter_title_hash(title):
    """Hash of chapter title only — used for chapter dedup on upsert (lightweight)."""
    return hashlib.md5(title.encode()).hexdigest()[:16]


def upsert_chapters(novel_url, chs):
    """Insert chapter links; skip if hash unchanged."""
    inserted = 0
    db = _get_db()
    for ci in chs:
        url = ci["url"]
        title = ci["title"]
        if not title or title.lower() == "read first":
            continue
        content_hash = _chapter_title_hash(title)
        existing = db.execute(
            "SELECT content_hash FROM chapters WHERE chapter_url=?", (url,)).fetchone()
        if existing and existing[0] == content_hash:
            continue
        scraped = int(time.time())
        db.execute("""INSERT OR IGNORE INTO chapters
            (novel_url,chapter_url,chapter_title,content,scraped_at,content_hash)
            VALUES (?,?,?,?,?,?)""",
            (novel_url, url, title, "", scraped, content_hash))
        inserted += 1
    db.commit()
    db.execute(
        "UPDATE novels SET chapter_count=(SELECT COUNT(*) FROM chapters "
        "WHERE chapters.novel_url=novels.url) WHERE url=?", (novel_url,))
    db.commit()
    return inserted


def get_new_chapters(novel_url, since_epoch):
    db = _get_db()
    rows = db.execute(
        "SELECT chapter_url, chapter_title, content, scraped_at "
        "FROM chapters WHERE novel_url=? AND scraped_at>? ORDER BY scraped_at DESC",
        (novel_url, since_epoch)).fetchall()
    return [{"url": r[0], "title": r[1], "content": r[2], "scraped_at": r[3]} for r in rows]


def extract_chapter_num(title):
    m = re.match(r'Chapter\s+(\d+)', title)
    if m:
        return int(m.group(1))
    return 0


def add_source(url):
    """Add a novel URL to the sources table."""
    db = _get_db()
    db.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    db.commit()

def remove_source(url):
    """Remove a novel URL from the sources table."""
    db = _get_db()
    db.execute("DELETE FROM sources WHERE url=?", (url,))
    db.commit()

def list_sources():
    """Return all source URLs."""
    db = _get_db()
    return [r[0] for r in db.execute("SELECT url FROM sources").fetchall()]


def _update_novel_chapter_count(novel_url):
    """Recount chapters for a novel."""
    db = _get_db()
    db.execute(
        "UPDATE novels SET chapter_count=(SELECT COUNT(*) FROM chapters "
        "WHERE chapters.novel_url=novels.url) WHERE url=?", (novel_url,))
    db.commit()


# ---- Scrape ----

async def scrape_novel(novel_url):
    """Scrape a single novel. Returns (ok: bool, message: str)."""
    global _scraping_novel, _scraping_progress
    _scraping_novel = os.path.basename(novel_url.rstrip("/"))
    _scraping_progress = {
        "message": "Fetching novel metadata...", "percent": 0, "total": 0, "scraped": 0}
    # Note: no lock held during scrape — multiple concurrent scrapes may
    # clobber _scraping_novel/_scraping_progress, but the dashboard only
    # shows a count, so that's fine.
    ref_url = novel_url

    # Fetch novel metadata page
    meta_html = await fetch(novel_url)
    if not meta_html:
        _scraping_novel = None
        _scraping_progress = {"message": "", "percent": 0, "total": 0, "scraped": 0}
        return False, "Failed to fetch novel page"

    meta = parse_novel(meta_html)
    upsert_novel(novel_url, meta)
    _scraping_progress["message"] = f"Found {meta['total_chapters']} chapters total"
    _scraping_progress["percent"] = 10

    # Collect chapter links
    chs = parse_chap_list(meta_html)
    log.info("  Found %d chapters on page 1 (total: %d)", len(chs), meta["total_chapters"])
    extra_chs = []
    if meta["total_pages"] > 1:
        for page in range(2, meta["total_pages"] + 1):
            api_url = novel_url + "?ajax=chapters&page=" + str(page) + \
                "&pageSize=" + str(meta["page_size"])
            log.info("  Fetching chapter page %d/%d...", page, meta["total_pages"])
            api_html = await fetch(api_url)
            if page < meta["total_pages"]:
                await asyncio.sleep(DELAY_PAGE_LIST)
            if api_html:
                try:
                    data = json.loads(api_html)
                    api_html = data.get("html", "")
                except Exception:
                    pass
            if api_html:
                page_chs = parse_chap_list(api_html)
                extra_chs.extend(page_chs)
                log.info("  Page %d: +%d chapters", page, len(page_chs))

    all_chapters = chs + extra_chs
    log.info("  Total chapter links: %d", len(all_chapters))
    upsert_chapters(novel_url, all_chapters)
    add_source(novel_url)

    # Only scrape chapters that don't already have content
    db = _get_db()
    pending = db.execute(
        "SELECT chapter_url, chapter_title FROM chapters "
        "WHERE novel_url=? AND content='' ORDER BY scraped_at DESC",
        (novel_url,)).fetchall()
    new_chaps = [{"url": r[0], "title": r[1]} for r in pending]
    _scraping_progress["total"] = len(new_chaps)

    if new_chaps:
        log.info("  %d new chapters to scrape content for", len(new_chaps))
        for i in range(0, len(new_chaps), SCRAPE_BATCH_SIZE):
            batch = new_chaps[i:i + SCRAPE_BATCH_SIZE]
            tasks = [_scrape_chapter(c, ref_url) for c in batch]
            results = await asyncio.gather(*tasks)
            scraped = sum(results)
            _scraping_progress["scraped"] += scraped
            _scraping_progress["message"] = f"Batch {i//SCRAPE_BATCH_SIZE + 1}: {scraped}/{len(batch)} saved"
            _scraping_progress["percent"] = 10 + int(80 * (i + len(batch)) / len(new_chaps))
            log.info("  %d-%d/%d: scraped %d", i, i + len(batch), len(new_chaps), scraped)
            if i + SCRAPE_BATCH_SIZE < len(new_chaps):
                await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

    # Mark complete
    db = _get_db()
    db.execute("""UPDATE novels SET last_scraped=?,
        chapter_count=(SELECT COUNT(*) FROM chapters WHERE chapters.novel_url=novels.url)
        WHERE url=?""", (int(time.time()), novel_url))
    db.commit()

    _scraping_progress["percent"] = 100
    _scraping_progress["message"] = f"Complete — {len(new_chaps)} new chapters"
    _scraping_novel = None
    _scraping_progress = {"message": "", "percent": 0, "total": 0, "scraped": 0}
    return True, f"Scraped {len(new_chaps)} new chapters"


async def _scrape_chapter(ch_info, ref_url):
    """Scrape content for a single chapter. Returns 1 if saved, 0 otherwise."""
    chap_html = await fetch(ch_info["url"], ref=ref_url)
    body = parse_chap_body(chap_html)
    if not body:
        return 0
    content = body["content"]
    if not content:
        return 0
    content_hash = _content_hash(content)
    db = _get_db()
    db.execute("""UPDATE chapters SET content=?, chapter_title=?,
        content_hash=?, scraped_at=? WHERE chapter_url=?""",
        (content, body["title"], content_hash, int(time.time()), ch_info["url"]))
    db.commit()
    return 1


async def scrape_novel_concurrent(novel_url):
    """Scrape a single novel without holding the lock."""
    return await scrape_novel(novel_url)


async def scrape_all_concurrent(urls, max_concurrent):
    """Scrape multiple novels concurrently with a semaphore to limit parallelism.
    Waits DELAY_BETWEEN_REQUESTS between novels to avoid rate limits."""
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _limited(url, idx):
        # Wait before each novel to prevent burst
        if idx > 0:
            await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
        async with semaphore:
            log.info("=== Scraping: %s ===", url)
            ok, msg = await scrape_novel_concurrent(url)
            if ok and msg != "Failed to fetch novel page":
                log.info("  Done: %s", msg)
            else:
                log.warning("  Failed: %s", msg)
            return (url, ok, msg)

    tasks = [_limited(url, i) for i, url in enumerate(urls)]
    results = await asyncio.gather(*tasks)
    return list(results)