#!/usr/bin/env python3
"""FastAPI server: API routes, dashboard, RSS serving."""
import html as html_mod
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from scraper import (
    _get_db, _scraping_novel, _scraping_progress,
    init_db, norm_url, scrape_all_sequential, scrape_novel,
    extract_chapter_num, add_source, remove_source, list_sources,
)
from feed import rss_all, rss_single

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fwn.server")
app = FastAPI(title="FreeWebNovel Scraper")

# Serve static files (CSS/JS for dashboard)
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Status ──
@app.get("/api/status")
def api_status():
    db = _get_db()
    novel_rows = db.execute("SELECT url,title,author,chapter_count,last_scraped FROM novels").fetchall()
    total_chaps = db.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
    chaps_with = db.execute("SELECT COUNT(*) FROM chapters WHERE length(content) > 0").fetchone()[0]
    novels_out = []
    for r in novel_rows:
        n_url, n_title, n_author, n_count, n_last = r
        wc = 0
        latest = ""
        if n_count:
            wc = db.execute(
                "SELECT COUNT(*) FROM chapters WHERE length(content) > 0 AND novel_url=?",
                (n_url,)).fetchone()[0]
            lt = db.execute(
                "SELECT chapter_title FROM chapters WHERE novel_url=? ORDER BY scraped_at DESC LIMIT 1",
                (n_url,)).fetchone()
            latest = lt[0][:30] if lt else ""
        pct = round(wc / n_count * 100) if n_count else 0
        last_str = ""
        if n_last:
            last_str = datetime.fromtimestamp(n_last, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        novels_out.append({
            "url": n_url, "title": n_title, "author": n_author,
            "chapter_count": n_count, "status": pct,
            "chapters_scraped": wc,
            "latest": latest, "last_scraped": last_str,
        })
    scraping = {
        "active": bool(_scraping_novel),
        "novel": _scraping_novel or "",
        **_scraping_progress,
    }
    return {
        "total_novels": len(novel_rows),
        "total_chapters": total_chaps,
        "chapters_with_content": chaps_with,
        "scraping": scraping,
        "novels": novels_out,
    }


# ── Scraping ──
@app.post("/api/scrape/{path:path}")
async def api_scrape(path):
    url = path if path.startswith("http") else norm_url(f"https://freewebnovel.com/novel/{path}")
    ok, msg = await scrape_novel(url)
    return {"ok": ok, "message": msg}

@app.get("/api/scrape/{path:path}/progress")
def api_scrape_progress(path):
    return {"novel": _scraping_novel or "", **_scraping_progress}

@app.post("/api/scrape-all")
async def api_scrape_all():
    urls = list_sources()
    if not urls:
        raise HTTPException(404, "No sources in database")
    db = _get_db()
    db.execute("UPDATE novels SET last_scraped=NULL")
    db.commit()
    results = await scrape_all_sequential(urls)
    failed = [u for u, ok, _ in results if not ok]
    success = [u for u, ok, _ in results if ok]
    if failed:
        log.warning("Scrape all completed with %d failures: %s", len(failed), failed)
    return {"ok": True, "message": f"Scraped {len(success)}/{len(urls)} novels", "failed": failed}


# ── Source management ──
@app.delete("/api/sources/{path:path}")
async def api_remove_source(path):
    url = path if path.startswith("http") else norm_url(f"https://freewebnovel.com/novel/{path}")
    db = _get_db()
    db.execute("DELETE FROM novels WHERE url=?", (url,))
    db.execute("DELETE FROM chapters WHERE novel_url=?", (url,))
    remove_source(url)
    db.commit()
    return {"ok": True, "removed": url}

@app.post("/api/sources/add")
async def api_add_source(request: Request):
    url = request.query_params.get("url", "")
    if not url:
        try:
            ct = request.headers.get("content-type", "")
            if "application/json" in ct:
                raw = await request.json()
                if isinstance(raw, dict):
                    url = raw.get("url", "")
        except Exception:
            pass
    if not url:
        raise HTTPException(400, "URL required")
    url = url.strip()
    if "freewebnovel.com" not in url:
        url = norm_url(f"https://freewebnovel.com/novel/{url}")
    # Check if already in sources via database
    db = _get_db()
    if db.execute("SELECT 1 FROM sources WHERE url=?", (url,)).fetchone():
        raise HTTPException(409, "Already in sources")
    add_source(url)
    ok, msg = await scrape_novel(url)
    return {"ok": ok, "url": url, "message": msg}


# ── Novel details ──
@app.post("/api/novels/update")
async def api_update_novel(body: dict):
    url = body.get("slug") or body.get("url", "")
    new_title = body.get("title")
    new_author = body.get("author")
    if not url:
        raise HTTPException(400, "Novel slug or URL required")
    db = _get_db()
    found = db.execute(
        "SELECT url FROM novels WHERE url LIKE '%/novel/%s' OR url LIKE '%/novel/%s/' OR url LIKE '%s'",
        (url, url, url)).fetchone()
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
def api_chapter_content(path, limit: int = 50, offset: int = 0):
    db = _get_db()
    chapters = db.execute(
        "SELECT chapter_url, chapter_title, content, scraped_at FROM chapters WHERE novel_url=? ORDER BY scraped_at DESC LIMIT ? OFFSET ?",
        (path, limit, offset)).fetchall()
    return {
        "chapters": [{"url": c[0], "title": c[1], "content_preview": c[2][:300] if c[2] else "", "scraped_at": c[2]} for c in chapters],
    }


# ── Logs ──
LOG_PATH = "/app/logs/scraper.log"

@app.get("/api/logs")
def api_logs(lines: int = 100):
    try:
        with open(LOG_PATH) as f:
            all_lines = f.readlines()
        return {"logs": all_lines[-int(lines):]}
    except FileNotFoundError:
        return {"logs": ["No log file found"]}


# ── RSS feeds ──
@app.get("/rss/{slug:path}")
async def rss_feed(slug):
    db = _get_db()
    novel = db.execute("SELECT url FROM novels WHERE url=?", (slug,)).fetchone()
    if not novel:
        slug_name = slug.rstrip("/").split("/")[-1]
        novel = db.execute("SELECT url FROM novels WHERE url LIKE ? OR url LIKE ? OR url=?",
                           (f"%/{slug_name}%", f"%/{slug_name}/", slug_name)).fetchone()
    if not novel:
        raise HTTPException(404, f"Novel not found: {slug}")
    novel_url = novel[0]
    rss_str, err = rss_single(novel_url)
    if err:
        raise HTTPException(404, err)
    return PlainTextResponse(content=rss_str, media_type="application/rss+xml")

@app.get("/rss/all.xml")
async def rss_all_feed():
    return PlainTextResponse(content=rss_all(), media_type="application/rss+xml")


# ── Dashboard (served from static files) ──

@app.get("/")
def root_redirect():
    """Redirect root URL to the dashboard."""
    return RedirectResponse(url="/progress")


@app.get("/progress")
def dashboard_page():
    """Serve the dashboard HTML page."""
    return FileResponse(STATIC_DIR / "dashboard.html")



# ── CLI entry point ──
def main():
    if len(__import__("sys").argv) < 2:
        print("Usage: python server.py serve")
        return
    cmd = __import__("sys").argv[1]
    init_db()
    if cmd == "serve":
        urls = list_sources()
        if urls:
            RUNNING = {"urls": urls}
            import threading
            import asyncio as _asyncio

            async def _background_loop():
                run_interval = __import__("os").environ.get("RUN_INTERVAL_HOURS", "6")
                run_interval = int(run_interval)
                while True:
                    try:
                        log.info("Background: scraping %d novels...", len(RUNNING["urls"]))
                        results = await scrape_all_sequential(RUNNING["urls"])
                        for url, ok, msg in results:
                            if ok:
                                log.info("Background: %s -> %s", url, msg)
                            else:
                                log.warning("Background: %s -> %s", url, msg)
                    except Exception:
                        log.exception("Background scrape error")
                    log.info("Waiting %d hours until next run...", run_interval)
                    await _asyncio.sleep(run_interval * 3600)

            def _run_thread():
                loop = _asyncio.new_event_loop()
                _asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(_background_loop())
                finally:
                    loop.close()

            threading.Thread(target=_run_thread, daemon=True).start()
            log.info("Background scraper started for %d sources", len(urls))
        log.info("Serving dashboard on 0.0.0.0:9310")
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=9310)
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python server.py serve")

if __name__ == "__main__":
    main()
