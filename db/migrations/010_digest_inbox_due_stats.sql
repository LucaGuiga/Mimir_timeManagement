-- Run 12 migration: pooled Telegram digest, Telegram replies, and due date statistics on professor profiles.
-- ADD COLUMN IF NOT EXISTS is MariaDB syntax; on MySQL 8 the installer drops the clause.

-- Messages waiting for the next digest. The digest goes out 30 minutes after the oldest waiting message or error.
CREATE TABLE IF NOT EXISTS notification_queue (
  id INT AUTO_INCREMENT PRIMARY KEY,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  level VARCHAR(16) NOT NULL DEFAULT 'info',
  text TEXT NOT NULL,
  sent_at DATETIME NULL,
  KEY ix_notification_queue_sent (sent_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Small named values the notifier keeps between runs (last error id reported, Telegram update offset).
CREATE TABLE IF NOT EXISTS notifier_state (
  name VARCHAR(64) NOT NULL PRIMARY KEY,
  value VARCHAR(255) NOT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Messages you send back to the bot, from telegram.chat_id only. Shown on the GUI Messages page.
CREATE TABLE IF NOT EXISTS telegram_inbox (
  id INT AUTO_INCREMENT PRIMARY KEY,
  update_id BIGINT NOT NULL,
  received_at DATETIME NOT NULL,
  text TEXT NOT NULL,
  seen BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE KEY uq_telegram_inbox_update (update_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Due dates against the syllabus: average shift in days (+ later than the syllabus), share within a day, matched assignments.
ALTER TABLE professor_profiles ADD COLUMN IF NOT EXISTS avg_due_shift_days FLOAT NULL;
ALTER TABLE professor_profiles ADD COLUMN IF NOT EXISTS due_on_time_share FLOAT NULL;
ALTER TABLE professor_profiles ADD COLUMN IF NOT EXISTS syllabus_matched_count INT NOT NULL DEFAULT 0;
