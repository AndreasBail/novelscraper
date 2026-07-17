#!/usr/bin/env python3
"""RSS feed generation — reads from SQLite and produces Atom-feelable RSS 2.0."""
import hashlib
import html as html_mod
import re
from datetime import datetime, timezone

from scraper import _get_db, extract_chapter_num


def _rss_item(chapter_url, chapter_title, content, scraped_at,
              novel_title, novel_author):
    pub_date = datetime.fromtimestamp(scraped_at, tz=timezone.utc)\
        .strftime("%a, %d %b %Y %H:%M:%S GMT")
    content_clean = re.sub(r'<[^>]+>', '', content) if content else ""
    guid = hashlib.md5(chapter_url.encode()).hexdigest()[:32]
    return (
        "    <item>\n"
        f"      <title>{html_mod.escape(chapter_title)}</title>\n"
        f"      <link>{chapter_url}</link>\n"
        f"      <guid isPermaLink=\"false\">{guid}</guid>\n"
        f"      <pubDate>{pub_date}</pubDate>\n"
        f"      <dc:creator>{html_mod.escape(novel_author)}</dc:creator>\n"
        f"      <description>{html_mod.escape(content_clean[:5000])}</description>\n"
        "    </item>"
    )


def rss_single(novel_url):
    """Generate RSS for a single novel."""
    db = _get_db()
    rows = db.execute("""
        SELECT c.chapter_url, c.chapter_title, c.content, c.scraped_at,
               n.title, n.author
        FROM chapters c
        JOIN novels n ON c.novel_url = n.url
        WHERE c.novel_url = ?
        ORDER BY c.scraped_at DESC
    """, (novel_url,)).fetchall()

    if not rows:
        return None, "No chapters found"

    novel_title, novel_author = rows[0][4], rows[0][5]

    # Sort by chapter number for chronological ordering
    rows.sort(key=lambda r: extract_chapter_num(r[1]))

    items = "\n".join(
        _rss_item(r[0], r[1], r[2], r[3], novel_title, novel_author)
        for r in rows
    )

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    rss_str = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>{html_mod.escape(novel_title)} - RSS</title>
    <link>{novel_url}</link>
    <description>Latest chapters of {html_mod.escape(novel_title)} by {html_mod.escape(novel_author)}</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
{items}
  </channel>
</rss>"""
    return rss_str, None


def rss_all():
    """Generate RSS with latest chapters from all novels (up to 200 items)."""
    db = _get_db()
    rows = db.execute("""
        SELECT c.chapter_url, c.chapter_title, c.content, c.scraped_at,
               n.title, n.author
        FROM chapters c
        JOIN novels n ON c.novel_url = n.url
        ORDER BY c.scraped_at DESC
        LIMIT 200
    """).fetchall()

    items = "\n".join(
        _rss_item(r[0], r[1], r[2], r[3], r[4], r[5])
        for r in rows
    )

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    rss_str = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>FreeWebNovel - All Chapters RSS</title>
    <link>https://freewebnovel.com</link>
    <description>Latest chapters from all novels</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
{items}
  </channel>
</rss>"""
    return rss_str