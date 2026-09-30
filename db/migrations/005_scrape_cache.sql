-- Run 8 migration: remembers a hash of each scraped page so the model is only called when the page changed.
CREATE TABLE IF NOT EXISTS scrape_cache (
  page_key VARCHAR(100) NOT NULL PRIMARY KEY,
  content_hash CHAR(64) NOT NULL,
  updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
