-- Run 14 migration: requests sent to the bot (parsed, confirmed, applied), class cancellations, manual due dates.
-- ADD COLUMN IF NOT EXISTS is MariaDB syntax; on MySQL 8 the installer drops the clause.

ALTER TABLE telegram_inbox ADD COLUMN IF NOT EXISTS processed BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE telegram_inbox SET processed = TRUE;

-- One request per message: parsed by Mimir and DeepSeek, then waiting for your yes or no.
-- status: needs_info, awaiting_confirm, applied, rejected, failed, expired
CREATE TABLE IF NOT EXISTS telegram_requests (
  id INT AUTO_INCREMENT PRIMARY KEY,
  inbox_id INT NULL,
  text TEXT NOT NULL,
  status VARCHAR(20) NOT NULL,
  draft_json JSON NULL,
  proposal_json JSON NULL,
  summary TEXT NULL,
  result TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  decided_at DATETIME NULL,
  KEY ix_telegram_requests_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- "No class Thursday": the planner leaves that day's class blocks out.
CREATE TABLE IF NOT EXISTS class_cancellations (
  course_id INT NOT NULL,
  date DATE NOT NULL,
  reason VARCHAR(255) NULL,
  PRIMARY KEY (course_id, date),
  CONSTRAINT fk_class_cancellations_course FOREIGN KEY (course_id) REFERENCES courses (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- A due date changed through Telegram wins over the iCal feed. source: NULL (iCal) or 'telegram' (added by you).
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS manual_due BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS source VARCHAR(16) NULL;
