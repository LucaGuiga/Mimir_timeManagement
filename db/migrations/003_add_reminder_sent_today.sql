-- Run 5 migration. ADD COLUMN IF NOT EXISTS is MariaDB syntax; on MySQL 8 drop the clause and run once.
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS reminder_sent_today BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS schema_migrations (
  id INT AUTO_INCREMENT PRIMARY KEY,
  filename VARCHAR(255) UNIQUE NOT NULL,
  applied_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
