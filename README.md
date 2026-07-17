# FreeWebNovel Scraper

Scrapes FreeWebNovel chapters into a MySQL database, serves an RSS feed and progress dashboard.

## Setup

### Docker

```bash
docker compose up --build -d
```

### Environment variables

Edit `.env` or pass your own:

| Variable | Default | Description |
|---|---|---|
| `MYSQL_ROOT_PASSWORD` | `changeme` | Root password for MariaDB |
| `MYSQL_DATABASE` | `freewebnovel` | Database name |
| `MYSQL_USER` | `fwnuser` | App user |
| `MYSQL_PASSWORD` | `fwnpass` | App password |
| `SERVER_PORT` | `9310` | RSS/dashboard port |
| `RUN_INTERVAL_HOURS` | `6` | Hours between scrape cycles |
| `DELAY_BETWEEN_REQUESTS` | `10` | Seconds between chapter batches |

### Add novels

Edit `sources.txt` — one novel URL per line:
```
https://freewebnovel.com/novel/slime-evolution
https://freewebnovel.com/novel/another-novel
```

## Endpoints

- **RSS** — `http://localhost:9310/rss/{novel-url}`
  - Example: `http://localhost:9310/rss/https://freewebnovel.com/novel/slime-evolution`
- **All RSS** — `http://localhost:9310/rss/all.xml`
- **Progress** — `http://localhost:9310/progress`
- **Status** — `http://localhost:9310/status`

## Database schema

Two tables in MySQL/MariaDB:

- `novels` — metadata (url, title, author, status, chapter_count)
- `chapters` — individual chapters (url, title, content, scraped_at)

Content is stored as plain text in a `LONGTEXT` column.

## Manual run (no Docker)

```bash
pip install -r requirements.txt
python scraper.py run
```

Set `MYSQL_HOST`, `MYSQL_PASSWORD`, etc. as environment variables.