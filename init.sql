CREATE TABLE IF NOT EXISTS novels (
    url VARCHAR(512) PRIMARY KEY,
    title TEXT NOT NULL, author TEXT DEFAULT '',
    cover TEXT, genres TEXT DEFAULT '', status TEXT DEFAULT '',
    last_scraped INT, chapter_count INT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chapters (
    id INT AUTO_INCREMENT PRIMARY KEY,
    novel_url VARCHAR(512) NOT NULL, chapter_url VARCHAR(512) NOT NULL,
    chapter_title TEXT NOT NULL, content LONGTEXT,
    scraped_at INT NOT NULL, content_hash VARCHAR(16) NOT NULL,
    INDEX idx_novel (novel_url(255), scraped_at),
    UNIQUE KEY uk_chapter_url (chapter_url(255))
);