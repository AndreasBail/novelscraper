#!/usr/bin/env python3
"""FreeWebNovel Scraper -> SQLite + RSS + Dashboard."""
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

DB_PATH = os.environ.get("DB_PATH", "/app/data/novels.db")
RUN_INTERVAL_HOURS = int(os.environ.get("RUN_INTERVAL_HOURS", "6"))
DELAY_BETWEEN_REQUESTS = float(os.environ.get("DELAY_BETWEEN_REQUESTS", "10"))
SERVER_PORT = int(os.environ.get("SERVER_PORT", "9310"))
LOG_PATH = os.environ.get("LOG_PATH", "/app/logs/scraper.log")
BASE = "https://freewebnovel.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

_scraping = threading.Lock()
_scraping_novel = None
_scraping_progress = {"message": "", "percent": 0, "total": 0, "scraped": 0}
_scraping_abort = threading.Event()

_local = threading.local()

def _get_db():
    if not getattr(_local, "db", None):
        _local.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.db.execute("PRAGMA journal_mode=WAL")
        _local.db.execute("PRAGMA busy_timeout=5000")
    return _local.db

def norm_url(url):
    return url.replace("www.freewebnovel.com", "freewebnovel.com", 1)

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
            log.warning("Error: %s",e)
            await asyncio.sleep(2.0*(2**a))
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
    title=re.sub(r'\\s+',' ',raw).strip()
    paras=t.xpath("//div[contains(@class,'m-read')]//p/text()")
    if not paras: paras=t.xpath("//div[contains(@class,'m-read')]//div[@class='chapter']//p/text()")
    content="\n".join(p.strip() for p in paras if p.strip())
    return {"title":title,"content":content} if content else None

# ---- DB ----
def init_db():
    db=_get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS novels (
        url TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT DEFAULT '',
        cover TEXT, genres TEXT DEFAULT '', status TEXT DEFAULT '',
        last_scraped INTEGER, chapter_count INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS chapters (
        novel_url TEXT NOT NULL, chapter_url TEXT PRIMARY KEY,
        chapter_title TEXT NOT NULL, content TEXT NOT NULL,
        scraped_at INTEGER NOT NULL, content_hash TEXT NOT NULL,
        FOREIGN KEY(novel_url) REFERENCES novels(url))""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_cn ON chapters(novel_url,scraped_at)")
    db.commit()

def upsert_novel(novel_url, meta):
    db=_get_db()
    db.execute("""INSERT INTO novels (url,title,author,cover,genres,status,last_scraped)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
        title=excluded.title,author=excluded.author,cover=excluded.cover,
        genres=excluded.genres,status=excluded.status,last_scraped=excluded.last_scraped""",
        (novel_url, meta["title"], meta["author"], meta["cover"],
         meta["genres"], meta["status"], int(time.time())))
    db.commit()

def upsert_chapters(novel_url, chs):
    inserted=0; db=_get_db()
    for ci in chs:
        url=ci["url"]; title=ci["title"]
        if not title or title.lower()=="read first": continue
        content_hash=hashlib.md5(title.encode()).hexdigest()[:16]
        existing=db.execute("SELECT content_hash FROM chapters WHERE chapter_url=?",(url,)).fetchone()
        if existing and existing[0]==content_hash: continue
        scraped=int(time.time())
        db.execute("""INSERT OR IGNORE INTO chapters (novel_url,chapter_url,chapter_title,content,scraped_at,content_hash)
            VALUES (?,?,?,?,?,?)""", (novel_url,url,title,"",scraped,content_hash))
        inserted += 1
    db.commit()
    db.execute("UPDATE novels SET chapter_count=(SELECT COUNT(*) FROM chapters WHERE chapters.novel_url=novels.url) WHERE url=?", (novel_url,))
    db.commit()
    return inserted

def get_new_chapters(novel_url, since):
    db=_get_db()
    rows=db.execute(
        "SELECT chapter_url, chapter_title, content, scraped_at FROM chapters WHERE novel_url=? AND scraped_at>? ORDER BY scraped_at DESC",
        (novel_url, since)).fetchall()
    return [{"url":r[0],"title":r[1],"content":r[2],"scraped_at":r[3]} for r in rows]

def extract_chapter_num(title):
    m = re.match(r'Chapter\\s+(\\d+)', title)
    if m: return int(m.group(1))
    return 0

# ---- Scrape ----
async def scrape_novel(novel_url):
    global _scraping_novel, _scraping_progress
    _scraping_novel = os.path.basename(novel_url.rstrip("/"))
    _scraping_progress = {"message": "Fetching novel metadata...", "percent": 0, "total": 0, "scraped": 0}
    ref_url = novel_url
    meta_html = await fetch(novel_url)
    if not meta_html:
        return False, "Failed to fetch novel page"
    meta = parse_novel(meta_html)
    upsert_novel(novel_url, meta)
    _scraping_progress["message"] = f"Found {meta['total_chapters']} chapters total"
    _scraping_progress["percent"] = 10
    chs = parse_chap_list(meta_html)
    log.info("  Found %d chapters on page 1 (total: %d)", len(chs), meta["total_chapters"])
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
    all_chapters = chs + extra_chs
    log.info("  Total chapter links: %d", len(all_chapters))
    upsert_chapters(novel_url, all_chapters)
    since = int(time.time()) - 86400 * 30
    new_chaps = get_new_chapters(novel_url, since)
    _scraping_progress["total"] = len(new_chaps)
    if new_chaps:
        log.info("  %d new chapters to scrape content for", len(new_chaps))
        async def _one(ci):
            chap_html = await fetch(ci["url"], ref=ref_url)
            body = parse_chap_body(chap_html)
            if not body: return 0
            content = body["content"]
            if content:
                content_hash = hashlib.md5(content.encode()).hexdigest()[:16]
                db = _get_db()
                db.execute("""UPDATE chapters SET content=?, chapter_title=?,
                    content_hash=?, scraped_at=? WHERE chapter_url=?""",
                    (content, body["title"], content_hash, int(time.time()), ci["url"]))
                db.commit()
                return 1
            return 0
        batch_size = min(10, len(new_chaps))
        for i in range(0, len(new_chaps), batch_size):
            batch = new_chaps[i:i + batch_size]
            tasks = [_one(ci) for ci in batch]
            results = await asyncio.gather(*tasks)
            scraped = sum(results)
            _scraping_progress["scraped"] += scraped
            _scraping_progress["message"] = f"Batch {i}-{i+len(batch)}/{len(new_chaps)}: {scraped} done"
            _scraping_progress["percent"] = 10 + int(80 * i / len(new_chaps))
            log.info("  Batch %d-%d/%d: scraped %d", i, i + len(batch), len(new_chaps), scraped)
            if i + batch_size < len(new_chaps):
                await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
    db = _get_db()
    db.execute("""UPDATE novels SET last_scraped=?,
        chapter_count=(SELECT COUNT(*) FROM chapters WHERE chapters.novel_url=novels.url)
        WHERE url=?""", (int(time.time()), novel_url))
    db.commit()
    _scraping_progress["percent"] = 100
    _scraping_progress["message"] = f"Complete \\u2014 {len(new_chaps)} new chapters"
    return True, f"Scraped {len(new_chaps)} new chapters"

# ---- API ----
app = FastAPI()

@app.get("/api/status")
def api_status():
    db = _get_db()
    novels = db.execute("SELECT url,title,author,status FROM novels").fetchall()
    total_chaps = db.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
    chaps_with_content = db.execute("SELECT COUNT(*) FROM chapters WHERE length(content) > 0").fetchone()[0]
    return {
        "total_novels": len(novels),
        "total_chapters": total_chaps,
        "chapters_with_content": chaps_with_content,
        "scraping": {"active": bool(_scraping_novel), "novel": _scraping_novel or "", **_scraping_progress},
        "novels": [{"url": r[0], "title": r[1], "author": r[2], "status": r[3]} for r in novels],
    }

@app.post("/api/scrape/{path:path}")
async def api_scrape(path):
    url = path if path.startswith("http") else f"https://freewebnovel.com/novel/{path}"
    with _scraping:
        if _scraping_novel:
            raise HTTPException(409, f"Already scraping: {_scraping_novel}")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: asyncio.run(scrape_novel(url)))
    return {"ok": result[0], "message": result[1]}

@app.get("/api/scrape/{path:path}/progress")
def api_scrape_progress(path):
    return {"novel": _scraping_novel or "", **_scraping_progress}

@app.post("/api/scrape-all")
async def api_scrape_all():
    with _scraping:
        if _scraping_novel:
            raise HTTPException(409, f"Already scraping: {_scraping_novel}")
        sources = Path("sources.txt")
        urls = [l.strip() for l in sources.read_text().splitlines() if l.strip()] if sources.exists() else []
        if not urls:
            raise HTTPException(404, "No sources in sources.txt")
        db = _get_db()
        db.execute("UPDATE novels SET last_scraped=NULL")
        db.commit()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: asyncio.run(asyncio.gather(*[scrape_novel(u) for u in urls])))
        db.execute("UPDATE novels SET last_scraped=? WHERE url IN (SELECT url FROM novels)", (int(time.time()),))
        db.commit()
    return {"ok": True, "message": f"Scraped {len(urls)} novels"}

@app.delete("/api/sources/{path:path}")
async def api_remove_source(path):
    url = path if path.startswith("http") else f"https://freewebnovel.com/novel/{path}"
    db = _get_db()
    db.execute("DELETE FROM novels WHERE url=?", (url,))
    db.execute("DELETE FROM chapters WHERE novel_url=?", (url,))
    db.commit()
    sources = Path("sources.txt")
    if sources.exists():
        lines = [l for l in sources.read_text().splitlines() if l.strip() != url]
        sources.write_text("\n".join(lines) + "\n")
    _scraping_abort.set()
    return {"ok": True, "removed": url}

@app.post("/api/sources/add")
async def api_add_source(body: dict = None, url: str = None):
    """Add a novel URL to sources and start scraping."""
    # Support both JSON body and query param
    from fastapi import Request
    # Try to read body if content-type is json
    try:
        url = body.get("url", "") if body else ""
    except Exception:
        pass
    if not url:
        raise HTTPException(400, "URL required")
    url = url.strip()
    # Validate it looks like a novel URL
    if "freewebnovel.com" not in url:
        url = f"https://freewebnovel.com/novel/{url}"
    sources = Path("sources.txt")
    existing = set()
    if sources.exists():
        existing = {l.strip() for l in sources.read_text().splitlines() if l.strip()}
    if url in existing:
        raise HTTPException(409, "Already in sources")
    existing.add(url)
    sources.write_text("\n".join(sorted(existing)) + "\n")
    _scraping_abort.set()
    return {"ok": True, "url": url}

@app.post("/api/novels/update")
async def api_update_novel(body: dict):
    """Update a novel's title, author, or URL."""
    url = body.get("slug") or body.get("url", "")
    new_title = body.get("title")
    new_author = body.get("author")
    if not url:
        raise HTTPException(400, "Novel slug or URL required")
    db = _get_db()
    # Find by slug
    found = db.execute("SELECT url FROM novels WHERE url LIKE '%/novel/%s' OR url LIKE '%/novel/%s/' OR url LIKE '%s'", (url, url, url)).fetchone()
    if not found:
        raise HTTPException(404, "Novel not found")
    orig_url = found[0]
    if new_title:
        db.execute("UPDATE novels SET title=? WHERE url=?", (new_title, orig_url))
    if new_author:
        db.execute("UPDATE novels SET author=? WHERE url=?", (new_author, orig_url))
    db.commit()
    return {"ok": True, "updated": orig_url}

@app.get("/api/novels/{path:path}")
def api_novel_detail(path):
    db = _get_db()
    novel = db.execute("SELECT url,title,author,cover,genres,status,last_scraped,chapter_count FROM novels WHERE url=?", (path,)).fetchone()
    if not novel:
        raise HTTPException(404, "Novel not found")
    n_url, n_title, n_author, n_cover, n_genres, n_status, n_last, n_count = novel
    total_chaps = db.execute("SELECT COUNT(*) FROM chapters WHERE novel_url=?", (n_url,)).fetchone()[0]
    with_content = db.execute("SELECT COUNT(*) FROM chapters WHERE length(content) > 0 AND novel_url=?", (n_url,)).fetchone()[0]
    pct = (with_content / total_chaps * 100) if total_chaps else 0
    chapters = db.execute("SELECT chapter_url, chapter_title, scraped_at FROM chapters WHERE novel_url=? ORDER BY scraped_at DESC", (n_url,)).fetchall()
    return {
        "url": n_url, "title": n_title, "author": n_author, "cover": n_cover,
        "genres": n_genres, "status": n_status, "last_scraped": n_last,
        "chapter_count": n_count, "total_chapters": total_chaps,
        "with_content": with_content, "progress_pct": round(pct, 1),
        "latest_chapter": chapters[0] if chapters else None,
        "chapters": [{"url": c[0], "title": c[1], "scraped_at": c[2]} for c in chapters],
    }

@app.get("/api/novels/{path:path}/chapters")
def api_chapter_content(path, limit=50, offset=0):
    db = _get_db()
    chapters = db.execute(
        "SELECT chapter_url, chapter_title, content, scraped_at FROM chapters WHERE novel_url=? ORDER BY scraped_at DESC LIMIT ? OFFSET ?",
        (path, limit, offset)).fetchall()
    return {"chapters": [{"url": c[0], "title": c[1], "content_preview": c[2][:300] if c[2] else "", "scraped_at": c[2]} for c in chapters]}

@app.get("/api/logs")
def api_logs(lines=100):
    try:
        with open(LOG_PATH) as f:
            all_lines = f.readlines()
        return {"logs": all_lines[-int(lines):]}
    except FileNotFoundError:
        return {"logs": ["No log file found"]}

@app.get("/rss/{slug:path}")
async def rss(slug):
    db = _get_db()
    # Resolve slug to full novel URL
    # Try exact match first (for full URLs passed as slug)
    novel = db.execute("SELECT url FROM novels WHERE url=?", (slug,)).fetchone()
    if not novel:
        # Try matching the last path segment as a slug
        slug_name = slug.rstrip("/").split("/")[-1]
        novel = db.execute("SELECT url FROM novels WHERE url LIKE ? OR url LIKE ? OR url=?",
                           (f"%/{slug_name}%", f"%/{slug_name}/", slug_name)).fetchone()
    if not novel:
        raise HTTPException(status_code=404, detail=f"Novel not found: {slug}")
    novel_url = novel[0]
    rows = db.execute("""SELECT c.chapter_url, c.chapter_title, c.content, c.scraped_at,
        n.title as novel_title, n.author as novel_author
        FROM chapters c JOIN novels n ON c.novel_url=n.url
        WHERE c.novel_url=? ORDER BY c.scraped_at DESC""", (novel_url,)).fetchall()
    rows.sort(key=lambda r: extract_chapter_num(r[1]))
    if not rows:
        raise HTTPException(status_code=404, detail="No chapters found")
    info = db.execute("SELECT title,author FROM novels WHERE url=?", (novel_url,)).fetchone()
    novel_title, novel_author = info or ("Unknown", "")
    items = ""
    for r in rows:
        chap_url, chap_title, content, scraped_at, _, _ = r
        pub_date = datetime.fromtimestamp(scraped_at, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        content_clean = re.sub(r'<[^>]+>','',content) if content else ""
        items += f"""    <item>
      <title>{html.escape(chap_title)}</title>
      <link>{chap_url}</link>
      <guid isPermaLink="false">{hashlib.md5(chap_url.encode()).hexdigest()[:32]}</guid>
      <pubDate>{pub_date}</pubDate>
      <dc:creator>{html.escape(novel_author)}</dc:creator>
      <description>{html.escape(content_clean[:5000])}</description>
    </item>"""
    rss_str = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>{html.escape(novel_title)} - RSS</title>
    <link>{novel_url}</link>
    <description>Latest chapters of {html.escape(novel_title)} by {html.escape(novel_author)}</description>
    <language>en</language>
    <lastBuildDate>{datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")}</lastBuildDate>
{items}  </channel>
</rss>"""
    return PlainTextResponse(content=rss_str, media_type="application/rss+xml")

@app.get("/rss/all.xml")
async def rss_all():
    db = _get_db()
    rows = db.execute("""SELECT c.chapter_url, c.chapter_title, c.content, c.scraped_at,
        n.title as novel_title, n.author as novel_author
        FROM chapters c JOIN novels n ON c.novel_url=n.url
        ORDER BY c.scraped_at DESC LIMIT 200""").fetchall()
    rows.sort(key=lambda r: extract_chapter_num(r[1]))
    if not rows:
        raise HTTPException(status_code=404, detail="No chapters found")
    items = ""
    for r in rows:
        chap_url, chap_title, content, scraped_at, novel_title, novel_author = r
        pub_date = datetime.fromtimestamp(scraped_at, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        content_clean = re.sub(r'<[^>]+>','',content) if content else ""
        items += f"""    <item>
      <title>{html.escape(novel_title)} - {html.escape(chap_title)}</title>
      <link>{chap_url}</link>
      <guid isPermaLink="false">{hashlib.md5(chap_url.encode()).hexdigest()[:32]}</guid>
      <pubDate>{pub_date}</pubDate>
      <dc:creator>{html.escape(novel_author)}</dc:creator>
      <description>{html.escape(content_clean[:3000])}</description>
    </item>"""
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

# ---- Dashboard ----
def dashboard_html():
    db = _get_db()
    novels = db.execute("SELECT url,title,author,chapter_count,last_scraped FROM novels").fetchall()
    total_novels = len(novels)
    total_chaps = db.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
    with_content = db.execute("SELECT COUNT(*) FROM chapters WHERE length(content) > 0").fetchone()[0]
    scraping = _scraping_novel
    pct = _scraping_progress.get("percent", 0)
    msg = _scraping_progress.get("message", "")
    active = bool(_scraping_novel)

    rows = ""
    for n_url, n_title, n_author, n_count, n_last in novels:
        bar_pct = 0
        latest = ""
        if n_count:
            wc = db.execute("SELECT COUNT(*) FROM chapters WHERE length(content) > 0 AND novel_url=?", (n_url,)).fetchone()[0]
            bar_pct = wc / n_count * 100
            lt = db.execute("SELECT chapter_title FROM chapters WHERE novel_url=? ORDER BY scraped_at DESC LIMIT 1", (n_url,)).fetchone()
            latest = lt[0] if lt else ""
        bar = chr(9608) * int(bar_pct / 2) + chr(9617) * (50 - int(bar_pct / 2)) if n_count else ""
        last_str = "Never"
        if n_last:
            last_str = datetime.fromtimestamp(n_last, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        safe_url = n_url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        safe_title = html.escape(n_title)
        slug = n_url.rstrip("/").split("/")[-1]
        rows += f"""    <tr>
      <td><a href="/rss/{slug}" style="color:#4fc3f7">{safe_title}</a></td>
      <td style="color:#aaa">{html.escape(n_author)}</td>
      <td>{n_count}</td>
      <td style="color:#0f0">{bar} {bar_pct:.0f}%</td>
      <td style="font-size:12px;color:#888">{latest[:40]}</td>
      <td style="font-size:12px;color:#888">{last_str}</td>
      <td class="action-col" style="white-space:nowrap">
        <a href="/api/scrape/{html.escape(n_url)}" title="Scrape" style="color:#3fb950;text-decoration:none;padding:2px 4px;font-size:16px;display:inline-block;border:1px solid transparent;border-radius:4px">▶</a>
        <button onclick="editNovel('{slug}', '{safe_title}', '{html.escape(n_author)}', '{html.escape(n_url)}')" title="Edit" style="color:#ffa657;text-decoration:none;padding:2px 4px;font-size:16px;cursor:pointer;background:none;border:1px solid transparent;border-radius:4px">✎</button>
        <button onclick="deleteNovel('{slug}', '{safe_title}')" title="Delete" style="color:#f85149;text-decoration:none;padding:2px 4px;font-size:16px;cursor:pointer;background:none;border:1px solid transparent;border-radius:4px">✕</button>
      </td>
    </tr>"""

    if not rows:
        rows = "    <tr><td colspan=7 style='color:#8b949e;text-align:center'>No novels scraped yet. Add URLs to sources.txt.</td></tr>"

    scrape_all = '<a href="/api/scrape-all" style="background:#ff9800;color:#fff;padding:8px 20px;border-radius:4px;text-decoration:none;font-size:14px;display:inline-block;margin-bottom:16px">Scrape All</a>'

    if active:
        indicator = f"""<div style="margin-bottom:24px;padding:12px 20px;background:#1a3a2a;border:1px solid #2ea043;border-radius:8px;color:#3fb950">
      <strong>Scraping:</strong> {html.escape(scraping)} &mdash; {html.escape(msg)}
      <div style="margin-top:8px;width:100%;height:8px;background:#21262d;border-radius:4px;overflow:hidden">
        <div style="height:100%;width:{pct}%;background:linear-gradient(90deg,#2ea043,#3fb950)"></div>
      </div>
    </div>"""
    else:
        indicator = '<div style="margin-bottom:24px;padding:12px 20px;background:#1c2333;border:1px solid #30363d;border-radius:8px;color:#8b949e">No active scrape. Click "Scrape ->" on a novel above.</div>'

    js_code = r"""<script>
function deleteNovel(slug, title) {
  if (!confirm("Remove \"" + title + "\" and all its chapters?")) return;
  fetch("/api/sources/" + slug, {method:"DELETE"})
    .then(r=>r.json()).then(d=>{location.reload()})
    .catch(e=>alert("Error: "+e));
}
function addNovel() {
  var url = document.getElementById("addUrl").value.trim();
  if (!url) return;
  var st = document.getElementById("addStatus");
  st.textContent = "Adding..."; st.style.color = "#8b949e";
  fetch("/api/sources/add?url=" + encodeURIComponent(url), {method:"POST"})
    .then(r=>r.json()).then(d=>{
      st.textContent = "Added: " + d.url + ". Scraping started.";
      st.style.color = "#3fb950";
      document.getElementById("addUrl").value = "";
      setTimeout(()=>location.reload(), 1500);
    }).catch(e=>{st.textContent = "Error: "+e; st.style.color="#f85149";});
}
function editNovel(slug, title, author, url) {
  var body = '<label style="color:#8b949e;font-size:13px;display:block;margin-bottom:4px">Title</label>'
    + '<input id="editTitle" value="' + title + '" style="width:100%;padding:8px;margin-bottom:12px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;font-size:14px">'
    + '<label style="color:#8b949e;font-size:13px;display:block;margin-bottom:4px">Author</label>'
    + '<input id="editAuthor" value="' + author + '" style="width:100%;padding:8px;margin-bottom:12px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;font-size:14px">'
    + '<label style="color:#8b949e;font-size:13px;display:block;margin-bottom:4px">URL</label>'
    + '<input id="editUrl" value="' + url + '" style="width:100%;padding:8px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;font-size:14px">';
  document.getElementById("modalTitle").textContent = "Edit: " + title;
  document.getElementById("modalBody").innerHTML = body;
  document.getElementById("modalSave").onclick = function(){
    var t = document.getElementById("editTitle").value.trim();
    var a = document.getElementById("editAuthor").value.trim();
    var u = document.getElementById("editUrl").value.trim();
    if(!t||!u) return;
    fetch("/api/novels/update", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({url:u, slug:slug, title:t, author:a})})
      .then(r=>r.json()).then(d=>{closeModal();location.reload()})
      .catch(e=>alert("Error: "+e));
  };
  document.getElementById("modal").style.display = "flex";
}
function closeModal() {
  document.getElementById("modal").style.display = "none";
}
document.addEventListener("keydown", function(e){ if(e.key==="Escape") closeModal(); });
</script>"""

    html_out = f"""<!DOCTYPE html>
<html><head><title>FreeWebNovel Scraper</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0e17;color:#c9d1d9;min-height:100vh}}
.header{{background:linear-gradient(135deg,#161b22,#0d1117);padding:24px 32px;border-bottom:1px solid #21262d}}
.header h1{{color:#f0f6fc;font-size:24px;font-weight:600}}
.header p{{color:#8b949e;margin-top:4px}}
.container{{max-width:1200px;margin:0 auto;padding:24px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:32px}}
.stat-card{{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:20px}}
.stat-card .label{{font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:1px}}
.stat-card .value{{font-size:32px;font-weight:700;color:#f0f6fc;margin-top:8px}}
.stat-card .value.green{{color:#3fb950}}
table{{border-collapse:collapse;background:#161b22;border-radius:8px;overflow:hidden}}
.table-wrap{{overflow-x:auto}}
th{{background:#0d1117;padding:12px 16px;text-align:left;font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:1px}}
td{{padding:12px 16px;border-top:1px solid #21262d}}
td.action-col{{min-width:100px;max-width:140px;white-space:nowrap;padding:12px 16px}}
tr:hover{{background:#1c2129}}
</style></head><body>
<div class="header"><h1>FreeWebNovel Scraper</h1><p>Dashboard &amp; management console</p></div>
<div class="container">
<div class="stats">
  <div class="stat-card"><div class="label">Novels</div><div class="value">{total_novels}</div></div>
  <div class="stat-card"><div class="label">Chapters</div><div class="value">{total_chaps}</div></div>
  <div class="stat-card"><div class="label">With Content</div><div class="value green">{with_content}</div></div>
  <div class="stat-card"><div class="label">Status</div><div class="value" style="font-size:16px">{"<span style='color:#3fb950'>ACTIVE</span>" if active else "Idle"}</div></div>
</div>
{indicator}
<div style="margin-bottom:16px">{scrape_all}</div>
<div class="table-wrap">
<table>
  <thead><tr><th>Novel</th><th>Author</th><th>Chapters</th><th>Progress</th><th>Latest</th><th>Last Scraped</th><th>Action</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>
<div style="margin-top:32px">
  <div id="addForm" style="padding:20px;background:#161b22;border:1px solid #21262d;border-radius:8px">
    <h3 style="color:#f0f6fc;margin-bottom:12px">Add Novel</h3>
    <div style="display:flex;gap:8px;align-items:center">
      <input id="addUrl" type="url" placeholder="https://freewebnovel.com/novel/..." style="flex:1;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:8px 12px;border-radius:4px;font-size:14px">
      <button onclick="addNovel()" style="background:#238636;color:#fff;border:none;padding:8px 20px;border-radius:4px;cursor:pointer;font-size:14px">Add &amp; Scrape</button>
    </div>
    <div id="addStatus" style="margin-top:8px;font-size:13px;color:#8b949e"></div>
  </div>
</div>
<div id="modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:100;justify-content:center;align-items:center">
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:24px;min-width:400px;max-width:600px">
    <h3 id="modalTitle" style="color:#f0f6fc;margin-bottom:16px"></h3>
    <div id="modalBody"></div>
    <div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end">
      <button onclick="closeModal()" style="background:#30363d;color:#c9d1d9;border:none;padding:8px 16px;border-radius:4px;cursor:pointer">Cancel</button>
      <button id="modalSave" style="background:#238636;color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer">Save</button>
    </div>
  </div>
</div>
{js_code}
</div></body></html>"""
    return html_out

@app.get("/progress")
def progress():
    return HTMLResponse(content=dashboard_html())

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
        sources = Path("sources.txt")
        urls = [l.strip() for l in sources.read_text().splitlines() if l.strip()] if sources.exists() else []
        if urls:
            async def _run():
                while True:
                    for u in urls:
                        await scrape_novel(u)
                    log.info("Waiting %d hours until next run...", RUN_INTERVAL_HOURS)
                    await asyncio.sleep(RUN_INTERVAL_HOURS * 3600)
            def _run_thread():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_run())
            threading.Thread(target=_run_thread, daemon=True).start()
            log.info("Background scraper started for %d sources", len(urls))
        log.info("Serving dashboard on 0.0.0.0:%d", SERVER_PORT)
        uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python scraper.py [scrape|serve|run]")

if __name__ == "__main__":
    main()
