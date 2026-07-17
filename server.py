#!/usr/bin/env python3
"""FastAPI server: API routes, dashboard, RSS serving."""
import html as html_mod
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, HTMLResponse

from scraper import (
    _get_db, _scraping_lock, _scraping_novel, _scraping_progress,
    init_db, norm_url, scrape_all_novels_serial, scrape_novel,
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


# ── Status ──
@app.get("/api/status")
def api_status():
    db = _get_db()
    novels = db.execute("SELECT url,title,author,status FROM novels").fetchall()
    total_chaps = db.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
    chaps_with = db.execute("SELECT COUNT(*) FROM chapters WHERE length(content) > 0").fetchone()[0]
    return {
        "total_novels": len(novels),
        "total_chapters": total_chaps,
        "chapters_with_content": chaps_with,
        "scraping": {"active": bool(_scraping_novel), "novel": _scraping_novel or "", **_scraping_progress},
        "novels": [{"url": r[0], "title": r[1], "author": r[2], "status": r[3]} for r in novels],
    }


# ── Scraping ──
@app.post("/api/scrape/{path:path}")
async def api_scrape(path):
    url = path if path.startswith("http") else norm_url(f"https://freewebnovel.com/novel/{path}")
    with _scraping_lock:
        if _scraping_novel:
            raise HTTPException(409, f"Already scraping: {_scraping_novel}")
        ok, msg = await scrape_novel(url)
    return {"ok": ok, "message": msg}

@app.get("/api/scrape/{path:path}/progress")
def api_scrape_progress(path):
    return {"novel": _scraping_novel or "", **_scraping_progress}

@app.post("/api/scrape-all")
async def api_scrape_all():
    with _scraping_lock:
        if _scraping_novel:
            raise HTTPException(409, f"Already scraping: {_scraping_novel}")
        urls = list_sources()
        if not urls:
            raise HTTPException(404, "No sources in database")
        db = _get_db()
        db.execute("UPDATE novels SET last_scraped=NULL")
        db.commit()
        results = await scrape_all_novels_serial(urls)
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
    with _scraping_lock:
        if _scraping_novel:
            raise HTTPException(409, f"Already scraping: {_scraping_novel}")
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


# ── Dashboard ──

_DASHBOARD_CSS = """\
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0e17;color:#c9d1d9;min-height:100vh}
.header{background:linear-gradient(135deg,#161b22,#0d1117);padding:24px 32px;border-bottom:1px solid #21262d}
.header h1{color:#f0f6fc;font-size:24px;font-weight:600}
.header p{color:#8b949e;margin-top:4px}
.container{max-width:1200px;margin:0 auto;padding:24px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:32px}
.stat-card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:20px}
.stat-card .label{font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:1px}
.stat-card .value{font-size:32px;font-weight:700;color:#f0f6fc;margin-top:8px}
.stat-card .value.green{color:#3fb950}
table{border-collapse:collapse;background:#161b22;border-radius:8px;overflow:hidden}
.table-wrap{overflow-x:auto}
th{background:#0d1117;padding:12px 16px;text-align:left;font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:1px}
td{padding:12px 16px;border-top:1px solid #21262d}
td.action-col{min-width:100px;max-width:140px;white-space:nowrap;padding:12px 16px}
tr:hover{background:#1c2129}
"""

_DASHBOARD_JS = """\
function deleteNovel(slug, title) {
  if (!confirm('Remove "' + title + '" and all its chapters?')) return;
  fetch('/api/sources/' + slug, {method:'DELETE'})
    .then(r=>r.json()).then(d=>{location.reload()})
    .catch(e=>alert('Error: '+e));
}
function addNovel() {
  var url = document.getElementById('addUrl').value.trim();
  if (!url) return;
  var st = document.getElementById('addStatus');
  st.textContent = 'Adding...'; st.style.color = '#8b949e';
  fetch('/api/sources/add?url=' + encodeURIComponent(url), {method:'POST'})
    .then(r=>r.json()).then(d=>{
      st.textContent = 'Added: ' + d.url + '. Scraping ' + (d.ok ? 'succeeded' : 'failed') + '.';
      st.style.color = d.ok ? '#3fb950' : '#f85149';
      document.getElementById('addUrl').value = '';
      setTimeout(()=>location.reload(), 1500);
    }).catch(e=>{st.textContent = 'Error: '+e; st.style.color='#f85149';});
}
function editNovel(slug, title, author, url) {
  var body = '<label style="color:#8b949e;font-size:13px;display:block;margin-bottom:4px">Title</label>'
    + '<input id="editTitle" value="'+title+'" style="width:100%;padding:8px;margin-bottom:12px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;font-size:14px">'
    + '<label style="color:#8b949e;font-size:13px;display:block;margin-bottom:4px">Author</label>'
    + '<input id="editAuthor" value="'+author+'" style="width:100%;padding:8px;margin-bottom:12px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;font-size:14px">'
    + '<label style="color:#8b949e;font-size:13px;display:block;margin-bottom:4px">URL</label>'
    + '<input id="editUrl" value="'+url+'" style="width:100%;padding:8px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;font-size:14px">';
  document.getElementById('modalTitle').textContent = 'Edit: ' + title;
  document.getElementById('modalBody').innerHTML = body;
  document.getElementById('modalSave').onclick = function(){
    var t = document.getElementById('editTitle').value.trim();
    var a = document.getElementById('editAuthor').value.trim();
    var u = document.getElementById('editUrl').value.trim();
    if(!t||!u) return;
    fetch('/api/novels/update', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url:u, slug:slug, title:t, author:a})})
      .then(r=>r.json()).then(d=>{closeModal();location.reload()})
      .catch(e=>alert('Error: '+e));
  };
  document.getElementById('modal').style.display = 'flex';
}
function closeModal() { document.getElementById('modal').style.display = 'none'; }
document.addEventListener('keydown', function(e){ if(e.key==='Escape') closeModal(); });
"""


def _build_dashboard_rows(novels):
    """Generate HTML table rows for all novels."""
    rows = ""
    for n_url, n_title, n_author, n_count, n_last in novels:
        bar_pct = 0
        latest = ""
        if n_count:
            wc = _get_db().execute(
                "SELECT COUNT(*) FROM chapters WHERE length(content) > 0 AND novel_url=?",
                (n_url,)).fetchone()[0]
            bar_pct = wc / n_count * 100
            lt = _get_db().execute(
                "SELECT chapter_title FROM chapters WHERE novel_url=? ORDER BY scraped_at DESC LIMIT 1",
                (n_url,)).fetchone()
            latest = lt[0] if lt else ""
        last_str = "Never"
        if n_last:
            last_str = datetime.fromtimestamp(n_last, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        slug = n_url.rstrip("/").split("/")[-1]
        safe_title = html_mod.escape(n_title)
        safe_author = html_mod.escape(n_author)
        bar_div = f'<div style="height:100%;width:{bar_pct}%;background:linear-gradient(90deg,#238636,#3fb950);border-radius:3px"></div>' if bar_pct > 0 else ""
        bar_html = (
            f'<div style="display:flex;align-items:center;gap:10px">'
            f'<div style="flex:1;height:8px;background:#21262d;border-radius:4px;overflow:hidden">{bar_div}</div>'
            f'<span style="font-size:12px;color:{"#3fb950" if bar_pct==100 else "#8b949e"};min-width:36px;text-align:right">{bar_pct:.0f}%</span></div>'
        )
        rows += f"""    <tr>
      <td><a href="/rss/{slug}" style="color:#4fc3f7">{safe_title}</a></td>
      <td style="color:#aaa">{safe_author}</td>
      <td>{n_count}</td>
      <td>{bar_html}</td>
      <td style="font-size:12px;color:#888">{latest[:40]}</td>
      <td style="font-size:12px;color:#888">{last_str}</td>
      <td style="text-align:center">
        <a href="/api/scrape/{html_mod.escape(n_url)}" title="Scrape" style="color:#3fb950;text-decoration:none;padding:2px 6px;font-size:16px;display:inline-block">▶</a>
        <button onclick="editNovel('{slug}', '{safe_title}', '{safe_author}', '{html_mod.escape(n_url)}')" title="Edit" style="color:#ffa657;text-decoration:none;padding:2px 6px;font-size:16px;cursor:pointer;background:none;border:none">✎</button>
        <button onclick="deleteNovel('{slug}', '{safe_title}')" title="Delete" style="color:#f85149;text-decoration:none;padding:2px 6px;font-size:16px;cursor:pointer;background:none;border:none">✕</button>
      </td>
    </tr>"""
    if not rows:
        rows = "    <tr><td colspan=7 style='color:#8b949e;text-align:center'>No novels scraped yet. Add URLs using the form below.</td></tr>"
    return rows


def dashboard_html():
    """Generate the full dashboard HTML page."""
    db = _get_db()
    novels = db.execute("SELECT url,title,author,chapter_count,last_scraped FROM novels").fetchall()
    total_novels = len(novels)
    total_chaps = db.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
    with_content = db.execute("SELECT COUNT(*) FROM chapters WHERE length(content) > 0").fetchone()[0]
    scraping = _scraping_novel
    pct = _scraping_progress.get("percent", 0)
    msg = _scraping_progress.get("message", "")
    active = bool(_scraping_novel)

    rows = _build_dashboard_rows(novels)
    scrape_all_btn = '<a href="/api/scrape-all" style="background:#ff9800;color:#fff;padding:8px 20px;border-radius:4px;text-decoration:none;font-size:14px;display:inline-block;margin-bottom:16px">Scrape All</a>'

    if active:
        indicator = f"""<div style="margin-bottom:24px;padding:12px 20px;background:#1a3a2a;border:1px solid #2ea043;border-radius:8px;color:#3fb950">
      <strong>Scraping:</strong> {html_mod.escape(scraping)} &mdash; {html_mod.escape(msg)}
      <div style="margin-top:8px;width:100%;height:8px;background:#21262d;border-radius:4px;overflow:hidden">
        <div style="height:100%;width:{pct}%;background:linear-gradient(90deg,#2ea043,#3fb950)"></div>
      </div>
    </div>"""
    else:
        indicator = '<div style="margin-bottom:24px;padding:12px 20px;background:#1c2333;border:1px solid #30363d;border-radius:8px;color:#8b949e">No active scrape. Click "Scrape ->" on a novel above.</div>'

    html_out = f"""<!DOCTYPE html>
<html><head><title>FreeWebNovel Scraper</title>
<style>{_DASHBOARD_CSS}</style></head><body>
<div class="header"><h1>FreeWebNovel Scraper</h1><p>Dashboard &amp; management console</p></div>
<div class="container">
<div class="stats">
  <div class="stat-card"><div class="label">Novels</div><div class="value">{total_novels}</div></div>
  <div class="stat-card"><div class="label">Chapters</div><div class="value">{total_chaps}</div></div>
  <div class="stat-card"><div class="label">With Content</div><div class="value green">{with_content}</div></div>
  <div class="stat-card"><div class="label">Status</div><div class="value" style="font-size:16px">{"<span style='color:#3fb950'>ACTIVE</span>" if active else "Idle"}</div></div>
</div>
{indicator}
<div style="margin-bottom:16px">{scrape_all_btn}</div>
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
<script>{_DASHBOARD_JS}</script>
</div></body></html>"""
    return html_out


@app.get("/progress")
def progress():
    return HTMLResponse(content=dashboard_html())


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
                    for url in RUNNING["urls"]:
                        try:
                            log.info("Background: scraping %s", url)
                            with _scraping_lock:
                                ok, msg = await scrape_novel(url)
                            log.info("Background: %s -> %s: %s", url, "OK" if ok else "FAIL", msg)
                        except Exception:
                            log.exception("Background scrape error for %s", url)
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
