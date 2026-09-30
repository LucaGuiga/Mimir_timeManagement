-- Run 6 migration: apply to a database created before run 6.
-- Fresh installs get all of this from db/schema.sql via `python -m db.db --init`.
-- ADD COLUMN IF NOT EXISTS, ADD INDEX IF NOT EXISTS, and ADD FOREIGN KEY IF NOT EXISTS are MariaDB syntax (10.0+).
-- On MySQL 8 drop the IF NOT EXISTS clauses and run each statement once.
-- Apply with: mysql mimir < db/migrations/002_run6_schema.sql

ALTER TABLE courses ADD COLUMN IF NOT EXISTS monitor BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS telegram_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS email_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS midterm_assignment_id INT NULL;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS final_assignment_id INT NULL;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS ical_course_code VARCHAR(50) NULL;
ALTER TABLE courses ADD INDEX IF NOT EXISTS ix_courses_ical_code (ical_course_code);
ALTER TABLE courses ADD FOREIGN KEY IF NOT EXISTS fk_courses_midterm (midterm_assignment_id) REFERENCES assignments (id) ON DELETE SET NULL;
ALTER TABLE courses ADD FOREIGN KEY IF NOT EXISTS fk_courses_final (final_assignment_id) REFERENCES assignments (id) ON DELETE SET NULL;

ALTER TABLE assignments ADD COLUMN IF NOT EXISTS canvas_assignment_url VARCHAR(500) NULL;
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS ical_uid VARCHAR(200) NULL;
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS is_midterm BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS is_final BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS grade_percent FLOAT NULL;
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS grade_detected_at DATETIME NULL;
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS reminder_24h_sent BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE assignments ADD UNIQUE INDEX IF NOT EXISTS uq_ical_uid (ical_uid);
ALTER TABLE assignments ADD INDEX IF NOT EXISTS ix_assignments_grade_detected (grade_detected_at);

CREATE TABLE IF NOT EXISTS oura_intraday (
  id INT AUTO_INCREMENT PRIMARY KEY,
  date DATE NOT NULL,
  poll_time TIME NOT NULL,
  readiness_score SMALLINT NULL,
  hrv_avg FLOAT NULL,
  stress_high BOOLEAN NOT NULL DEFAULT FALSE,
  stress_threshold_used FLOAT NULL,
  notified BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_oura_intraday_date (date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ENUM columns cannot take IF NOT EXISTS; MODIFY is idempotent because the new list is a superset.
ALTER TABLE poller_status MODIFY COLUMN api_name ENUM('canvas','github','oura','ical','scraper') NOT NULL;
ALTER TABLE poll_metrics MODIFY COLUMN api_name ENUM('canvas','github','oura','ical','scraper') NOT NULL;
