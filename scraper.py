#!/usr/bin/env python3
"""FreeWebNovel Scraper -> SQLite + RSS. Usage: python scraper.py scrape|serve|run"""
import asyncio, hashlib, html, json, logging, os, re, sqlite3, sys, time, threading
from datetime import datetime, timezone
from pathlib import Path
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse
from lxml import etree

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("fwn")

DB_PATH = os.environ.get("DB_PATH", "novels.db")
RUN_INTERVAL_HOURS = int(os.environ.get("RUN_INTERVAL_HOURS", "6"))
DELAY_BETWEEN_REQUESTS = float(os.environ.get("DELAY_BETWEEN_REQUESTS", "10"))
SERVER_PORT = int(os.environ.get("SERVER_PORT", "9310"))
BASE = "https://freewebnovel.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

_local = threading.local()

def _get_db():
    if not getattr(_local, "db", None):
        _local.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.db.execute("PRAGMA journal_mode=WAL")
        _local.db.execute("PRAGMA busy_timeout=5000")
    return _local.db

def norm_url(url):
    return url.replace("www.freewebnovel.com", "freewebnovel.com", 1)

# ---- HTTP ----
async def fetch(url, ref=None, retries=3):
    url = norm_url(url)
    if ref: ref = norm_url(ref)
    h = {"User-Agent":UA,"Accept":"text/html,application/xhtml+xml","Accept-Language":"en-US,en;q=0.5"}
    if ref: h["Referer"] = ref
    for a in range(retries):
        try:
            r = httpx.get(url, headers=h, timeout=30.0, follow_redirects=True)
            if r.status_code==200: return r.text
            if r.status_code in (429,500,502,503):
                w=2.0*(2**a)
                log.warning("HTTP %d for %s, retry %.1fs",r.status_code,url,w)
                await asyncio.sleep(w)
        except Exception as e:
            log.warning("Error: %s",e); await asyncio.sleep(2.0*(2**a))
    return None

# ---- Parsing ----
def parse_novel(html):
    t=etree.HTML(html)
    h1=t.xpath("//h1//text()")
    title="".join(h1).strip() if h1 else "Unknown"
    cover=t.xpath("//meta[@property='og:image']/@content")
    genres=t.xpath("//meta[@property='og:novel:genre']/@content")
    author=t.xpath("//meta[@property='og:novel:author']/@content")
    status=t.xpath("//meta[@property='og:novel:status']/@content")
    data_div=t.xpath("//div[@id='indexListPage']")
    total_chapters=int(data_div[0].get("data-total-chapters",0)) if data_div else 0
    total_pages=int(data_div[0].get("data-total-page",1)) if data_div else 1
    page_size=int(data_div[0].get("data-page-size",40)) if data_div else 40
    return {
        "title": title, "cover": cover[0] if cover else None,
        "genres": genres[0] if genres else "", "author": author[0] if author else "",
        "status": status[0] if status else "",
        "total_chapters": total_chapters, "total_pages": total_pages, "page_size": page_size,
    }

def parse_chap_list(html):
    t=etree.HTML(html); chs=[]
    for a in t.xpath("//ul[contains(@class,'ul-list5')]/li/a"):
        h=a.get("href",""); txt="".join(a.itertext()).strip()
        if h and txt: chs.append({"url":h if h.startswith("http") else BASE+h,"title":txt})
    if not chs:
        for a in t.xpath("//a[contains(@href,'chapter')]"):
            h=a.get("href",""); txt="".join(a.itertext()).strip()
            if h and txt: chs.append({"url":h if h.startswith("http") else BASE+h,"title":txt})
    return chs

def parse_chap_body(html):
    if not html: return None
    t=etree.HTML(html)
    ti=t.xpath("//span[@class='chapter']/text()")
    if not ti: ti=t.xpath("//div[contains(@class,'reader-page-title')]//text()")
    raw=ti[0].strip() if ti else "Untitled"
    title=re.sub(r'\s+',' ',raw).strip()
    # Try multiple selectors for content paragraphs
    paras=t.xpath("//div[contains(@class,'m-read')]//p/text()")
    if not paras: paras=t.xpath("//div[contains(@class,'m-read')]//div[@class='chapter']//p/text()")
    content="\n".join(p.strip() for p in paras if p.strip())
    return {"title":title,"content":content} if content else None

# ---- DB ----
def _c(): return _get_db()

def init_db():
    c=_c()
    c.execute("""CREATE TABLE IF NOT EXISTS novels (
        url TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT DEFAULT '',
        cover TEXT, genres TEXT DEFAULT '', status TEXT DEFAULT '',
        last_scraped INTEGER, chapter_count INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS chapters (
        novel_url TEXT NOT NULL, chapter_url TEXT PRIMARY KEY,
        chapter_title TEXT NOT NULL, content TEXT NOT NULL,
        scraped_at INTEGER NOT NULL, content_hash TEXT NOT NULL,
        FOREIGN KEY(novel_url) REFERENCES novels(url))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cn ON chapters(novel_url,scraped_at)")
    c.commit()

def add_src(url):
    c=_c(); c.execute("INSERT OR IGNORE INTO novel_sources (url) VALUES (?)",(url,)); c.commit(); c.close()

def upsert_novel(novel_url, meta):
    c=_c()
    c.execute("""INSERT INTO novels (url,title,author,cover,genres,status,last_scraped)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
        title=excluded.title,author=excluded.author,cover=excluded.cover,
        genres=excluded.genres,status=excluded.status,last_scraped=excluded.last_scraped""",
        (novel_url, meta["title"], meta["author"], meta["cover"],
         meta["genres"], meta["status"], int(time.time())))
    c.commit()

def upsert_chapters(novel_url, chs):
    inserted=0; conn=sqlite3.connect(str(DB_PATH))
    for ci in chs:
        url=ci["url"]; title=ci["title"]
        if not title or title.lower()=="read first": continue
        content_hash=hashlib.md5(title.encode()).hexdigest()[:16]
        existing=conn.execute("SELECT content_hash FROM chapters WHERE chapter_url=?",(url,)).fetchone()
        if existing and existing[0]==content_hash: continue
        scraped=int(time.time())
        conn.execute("""INSERT OR IGNORE INTO chapters (novel_url,chapter_url,chapter_title,content,scraped_at,content_hash)
            VALUES (?,?,?,?,?,?)""", (novel_url,url,title,"",scraped,content_hash))
        inserted += 1
    conn.commit()
    conn.execute("UPDATE novels SET chapter_count=(SELECT COUNT(*) FROM chapters WHERE chapters.novel_url=novels.url) WHERE url=?", (novel_url,))
    conn.commit()
    return inserted

def get_new_chapters(novel_url, since):
    conn=_c()
    rows=conn.execute(
        "SELECT chapter_url, chapter_title, content, scraped_at FROM chapters WHERE novel_url=? AND scraped_at>? ORDER BY scraped_at DESC",
        (novel_url, since)).fetchall()
    return [{"url":r[0],"title":r[1],"content":r[2],"scraped_at":r[3]} for r in rows]

def extract_chapter_num(title):
    """Extract chapter number from titles like 'Chapter 314 - Astralis Account'."""
    import re
    m = re.match(r'Chapter\s+(\d+)', title)
    if m:
        return int(m.group(1))
    return 0

def get_chapters_for_rss(novel_url, limit=9999):
    conn=_c()
    rows=conn.execute("""SELECT c.chapter_url, c.chapter_title, c.content, c.scraped_at,
        n.title as novel_title, n.author as novel_author
        FROM chapters c JOIN novels n ON c.novel_url=n.url
        WHERE c.novel_url=?""",
        (novel_url,)).fetchall()
    # Sort by actual chapter number (extracted from title)
    rows.sort(key=lambda r: extract_chapter_num(r[1]))
    return rows[:limit]

def get_all_chapters_for_rss(limit=200):
    conn=_c()
    rows=conn.execute("""SELECT c.chapter_url, c.chapter_title, c.content, c.scraped_at,
        n.title as novel_title, n.author as novel_author
        FROM chapters c JOIN novels n ON c.novel_url=n.url"""
    ).fetchall()
    rows.sort(key=lambda r: extract_chapter_num(r[1]))
    return rows[:limit]

# ---- Scrape ----
async def scrape_novel(novel_url):
    log.info("Scraping: %s", novel_url)
    ref_url = novel_url
    meta_html = await fetch(novel_url)
    if not meta_html: return False, "Failed to fetch novel page"
    meta = parse_novel(meta_html)
    upsert_novel(novel_url, meta)

    # Parse first page of chapters
    chs = parse_chap_list(meta_html)
    log.info("  Found %d chapters on page 1 (total: %d)", len(chs), meta["total_chapters"])

    # Fetch additional chapter pages via JSON API
    extra_chs = []
    if meta["total_pages"] > 1:
        for page in range(2, meta["total_pages"] + 1):
            api_url = novel_url + "?ajax=chapters&page=" + str(page) + "&pageSize=" + str(meta["page_size"])
            api_html = await fetch(api_url)
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
            else:
                log.warning("  Page %d: failed", page)

    all_chapters = chs + extra_chs
    log.info("  Total chapter links: %d", len(all_chapters))
    upsert_chapters(novel_url, all_chapters)

    # Scrape content for new chapters
    since = int(time.time()) - 86400 * 30
    new_chaps = get_new_chapters(novel_url, since)
    max_scrape = cfg["scraping"].get("max_per_run", 50)

    if new_chaps:
        log.info("  %d new chapters to scrape content for", len(new_chaps))

        async def _one(ci):
            chap_html = await fetch(ci["url"], ref=ref_url)
            body = parse_chap_body(chap_html)
            if not body: return 0
            content = body["content"]
            if content:
                content_hash = hashlib.md5(content.encode()).hexdigest()[:16]
                conn = _c()
                conn.execute("""UPDATE chapters SET content=?, chapter_title=?,
                    content_hash=?, scraped_at=? WHERE chapter_url=?""",
                    (content, body["title"], content_hash, int(time.time()), ci["url"]))
                conn.commit()
                return 1
            return 0

        # Process in batches with delays
        batch_size = min(10, len(new_chaps))
        for i in range(0, len(new_chaps), batch_size):
            batch = new_chaps[i:i + batch_size]
            tasks = [_one(ci) for ci in batch]
            results = await asyncio.gather(*tasks)
            scraped = sum(results)
            log.info("  Batch %d-%d/%d: scraped %d", i, i + len(batch), len(new_chaps), scraped)
            if i + batch_size < len(new_chaps):
                await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

    db = _c()
    db.execute("""UPDATE novels SET last_scraped=?,
        chapter_count=(SELECT COUNT(*) FROM chapters WHERE chapters.novel_url=novels.url)
        WHERE url=?""", (int(time.time()), novel_url))
    c.commit()
    return True, f"Scraped {len(new_chaps)} new chapters"

# ---- Server ----
app = FastAPI()

def progress_html():
    novels = _c().execute("SELECT url,title,author,chapter_count FROM novels").fetchall()
    if not novels:
        return '<h1>No novels scraped yet.</h1>' 
    rows = []
    for n_url, n_title, n_author, n_count in novels:
        db = _c()
        total = db.execute("SELECT COUNT(*) FROM chapters WHERE novel_url=?", (n_url,)).fetchone()[0]
        with_c = db.execute("SELECT COUNT(*) FROM chapters WHERE length(content) > 0 AND novel_url=?", (n_url,)).fetchone()[0]
        first_title = db.execute("SELECT chapter_title FROM chapters WHERE novel_url=? ORDER BY length(content) DESC LIMIT 1", (n_url,)).fetchone()
        latest = first_title[0] if first_title else "none"
    
        pct = (with_c / total * 100) if total else 0
        bar = "█" * int(pct / 2) + "░" * (50 - int(pct / 2))
        rows.append(f"""    <div style="margin:20px 0;padding:15px;border:1px solid #333;border-radius:8px;background:#1a1a1a">
      <h2 style="margin:0 0 5px">{html.escape(n_title)}</h2>
      <div style="color:#aaa;font-size:14px">{html.escape(n_author)} · {total} chapters · <span style="color:#0f0">{with_c}</span> scraped</div>
      <div style="margin:10px 0;font-size:13px;letter-spacing:1px">{bar} {pct:.1f}%</div>
      <div style="color:#888;font-size:12px">Latest with content: {html.escape(latest)}</div>
    </div>
""")
    html_out = f"""<!DOCTYPE html>
<html><head><title>Scraper Progress</title>
<style>
body {{ font-family: monospace; background: #000; color: #0f0; padding: 30px; margin: 0; }}
h1 {{ color: #fff; }}
</style>
</head><body>
<h1>FreeWebNovel Scraper</h1>
{chr(10).join(rows)}
</body></html>"""
    return html_out, "text/html"

@app.get("/progress")
def progress():
    html_out, _ = progress_html()
    return HTMLResponse(content=html_out)

@app.get("/status")
def status():
    c = _c()
    novels = c.execute("SELECT url,title,author,status FROM novels").fetchall()
    c.close()
    return {"novels": len(novels), "novel_list": [{"url":r[0],"title":r[1],"author":r[2],"status":r[3]} for r in novels]}

@app.get("/rss/{path:path}")
async def rss(path):
    c = _c()
    novel = c.execute("SELECT url FROM novels WHERE url=?", (path,)).fetchone()
    c.close()
    if not novel:
        raise HTTPException(status_code=404, detail=f"Novel not found: {path}")
    rows = get_chapters_for_rss(novel[0])
    if not rows:
        raise HTTPException(status_code=404, detail="No chapters found")
    c = _c()
    info = c.execute("SELECT title,author FROM novels WHERE url=?", (novel[0],)).fetchone()
    c.close()
    novel_title, novel_author = info or ("Unknown", "")
    items = ""
    for r in rows:
        chap_url, chap_title, content, scraped_at, _, _ = r
        pub_date = datetime.fromtimestamp(scraped_at, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        content = re.sub(r'<[^>]+>','',content)
        items += f"""    <item>
      <title>{html.escape(chap_title)}</title>
      <link>{chap_url}</link>
      <guid isPermaLink="false">{hashlib.md5(chap_url.encode()).hexdigest()[:32]}</guid>
      <pubDate>{pub_date}</pubDate>
      <dc:creator>{html.escape(novel_author)}</dc:creator>
      <description>{html.escape(content[:5000])}</description>
    </item>
"""
    rss_str = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>{html.escape(novel_title)} - RSS</title>
    <link>{path}</link>
    <description>Latest chapters of {html.escape(novel_title)} by {html.escape(novel_author)}</description>
    <language>en</language>
    <lastBuildDate>{datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")}</lastBuildDate>
{items}  </channel>
</rss>"""
    return PlainTextResponse(content=rss_str, media_type="application/rss+xml")

@app.get("/rss/all.xml")
async def rss_all():
    rows = get_all_chapters_for_rss()
    if not rows:
        raise HTTPException(status_code=404, detail="No chapters found")
    items = ""
    for r in rows:
        chap_url, chap_title, content, scraped_at, novel_title, novel_author = r
        pub_date = datetime.fromtimestamp(scraped_at, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        content = re.sub(r'<[^>]+>','',content)
        items += f"""    <item>
      <title>{html.escape(novel_title)} - {html.escape(chap_title)}</title>
      <link>{chap_url}</link>
      <guid isPermaLink="false">{hashlib.md5(chap_url.encode()).hexdigest()[:32]}</guid>
      <pubDate>{pub_date}</pubDate>
      <dc:creator>{html.escape(novel_author)}</dc:creator>
      <description>{html.escape(content[:3000])}</description>
    </item>
"""
    rss_str = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>FreeWebNovel - All Chapters RSS</title>
    <link>https://freewebnovel.com</link>
    <description>Latest chapters from all novels</description>
    <language>en</language>
    <lastBuildDate>{datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")}</lastBuildDate>
{items}  </channel>
</rss>"""
    return PlainTextResponse(content=rss_str, media_type="application/rss+xml")

# ---- CLI ----
def main():
    if len(sys.argv) < 2:
        print("Usage: python scraper.py [scrape|serve|run]")
        return
    cmd = sys.argv[1]
    init_db()

    if cmd == "scrape":
        sources = Path("sources.txt")
        if sources.exists():
            urls = [l.strip() for l in sources.read_text().splitlines() if l.strip()]
        else:
            log.error("sources.txt not found")
            return
        if not urls:
            log.error("No URLs in sources.txt")
            return
        async def scrape_all():
            for u in urls:
                await scrape_novel(u)
        asyncio.run(scrape_all())

    elif cmd == "run":
        sources = Path("sources.txt")
        urls = [l.strip() for l in sources.read_text().splitlines() if l.strip()] if sources.exists() else []
        if not urls:
            log.error("No sources configured")
            return
        async def _run():
            while True:
                for u in urls:
                    await scrape_novel(u)
                log.info("Waiting %d hours until next run...", RUN_INTERVAL_HOURS)
                await asyncio.sleep(RUN_INTERVAL_HOURS * 3600)
        asyncio.run(_run())

    elif cmd == "serve":
        log.info("Serving RSS on 0.0.0.0:%d", SERVER_PORT)
        uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python scraper.py [scrape|serve|run]")

if __name__ == "__main__":
    main()