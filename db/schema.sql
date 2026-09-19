-- Athena schema. Idempotent: every statement is CREATE TABLE IF NOT EXISTS.
-- Apply with: python -m db.db --init

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS professor_profiles (
  id INT AUTO_INCREMENT PRIMARY KEY,
  professor_name VARCHAR(255) NOT NULL,
  syllabus_adherence_score FLOAT NULL,
  avg_early_post_days FLOAT NULL,
  avg_late_post_days FLOAT NULL,
  typical_post_days JSON NULL,
  typical_post_hour_start TINYINT NULL,
  typical_post_hour_end TINYINT NULL,
  observation_count INT NOT NULL DEFAULT 0,
  last_updated DATETIME NULL,
  UNIQUE KEY uq_professor_name (professor_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS courses (
  id INT AUTO_INCREMENT PRIMARY KEY,
  canvas_course_id BIGINT NOT NULL,
  canvas_course_name VARCHAR(255) NOT NULL,
  professor_id INT NULL,
  repo_name VARCHAR(255) NULL,
  repo_path_prefix VARCHAR(255) NULL,
  branches JSON NOT NULL DEFAULT (JSON_ARRAY('main')),
  quarter VARCHAR(32) NULL,
  mapped BOOLEAN NOT NULL DEFAULT FALSE,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_canvas_course_id (canvas_course_id),
  KEY ix_courses_professor (professor_id),
  KEY ix_courses_active (active),
  KEY ix_courses_mapped (mapped),
  KEY ix_courses_quarter (quarter),
  CONSTRAINT fk_courses_professor FOREIGN KEY (professor_id)
    REFERENCES professor_profiles (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS assignments (
  id INT AUTO_INCREMENT PRIMARY KEY,
  course_id INT NOT NULL,
  canvas_assignment_id BIGINT NOT NULL,
  title VARCHAR(512) NOT NULL,
  assignment_type ENUM('hw','quiz','test','lab','project_milestone','reading','other') NOT NULL DEFAULT 'other',
  assignment_number INT NULL,
  available_from DATETIME NULL,
  due_at DATETIME NULL,
  syllabus_predicted_open DATETIME NULL,
  syllabus_predicted_due DATETIME NULL,
  status ENUM('pending','open','submitted','graded') NOT NULL DEFAULT 'pending',
  canvas_posted_at DATETIME NULL,
  profiled BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_canvas_assignment_id (canvas_assignment_id),
  KEY ix_assignments_course (course_id),
  KEY ix_assignments_due (due_at),
  KEY ix_assignments_status (status),
  KEY ix_assignments_profiled (profiled),
  KEY ix_assignments_type_number (assignment_type, assignment_number),
  CONSTRAINT fk_assignments_course FOREIGN KEY (course_id)
    REFERENCES courses (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS assignment_changes (
  id INT AUTO_INCREMENT PRIMARY KEY,
  assignment_id INT NOT NULL,
  field_changed ENUM('due_at','available_from','title') NOT NULL,
  old_value VARCHAR(512) NULL,
  new_value VARCHAR(512) NULL,
  detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  profiled BOOLEAN NOT NULL DEFAULT FALSE,
  reported BOOLEAN NOT NULL DEFAULT FALSE,
  KEY ix_changes_assignment (assignment_id),
  KEY ix_changes_detected (detected_at),
  KEY ix_changes_profiled (profiled),
  KEY ix_changes_reported (reported),
  CONSTRAINT fk_changes_assignment FOREIGN KEY (assignment_id)
    REFERENCES assignments (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS announcements (
  id INT AUTO_INCREMENT PRIMARY KEY,
  course_id INT NOT NULL,
  canvas_announcement_id BIGINT NOT NULL,
  title VARCHAR(512) NOT NULL,
  body TEXT NULL,
  posted_at DATETIME NULL,
  detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  profiled BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE KEY uq_canvas_announcement_id (canvas_announcement_id),
  KEY ix_announcements_course (course_id),
  KEY ix_announcements_posted (posted_at),
  KEY ix_announcements_profiled (profiled),
  CONSTRAINT fk_announcements_course FOREIGN KEY (course_id)
    REFERENCES courses (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hot_zones (
  id INT AUTO_INCREMENT PRIMARY KEY,
  professor_id INT NOT NULL,
  day_of_week TINYINT NOT NULL,
  hour_start TINYINT NOT NULL,
  hour_end TINYINT NOT NULL,
  source ENUM('profiler','manual') NOT NULL DEFAULT 'profiler',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_hot_zones_professor (professor_id),
  KEY ix_hot_zones_day (day_of_week),
  CONSTRAINT fk_hot_zones_professor FOREIGN KEY (professor_id)
    REFERENCES professor_profiles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS oura_daily (
  id INT AUTO_INCREMENT PRIMARY KEY,
  date DATE NOT NULL,
  sleep_score SMALLINT NULL,
  readiness_score SMALLINT NULL,
  hrv_avg FLOAT NULL,
  resting_hr FLOAT NULL,
  total_sleep_seconds INT NULL,
  sleep_efficiency FLOAT NULL,
  data_source ENUM('api','estimated') NOT NULL DEFAULT 'api',
  missing BOOLEAN NOT NULL DEFAULT FALSE,
  filled_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_oura_date (date),
  KEY ix_oura_missing (missing)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS stress_scores (
  id INT AUTO_INCREMENT PRIMARY KEY,
  date DATE NOT NULL,
  ratio FLOAT NOT NULL,
  t_awake FLOAT NULL,
  t_class FLOAT NULL,
  t_travel FLOAT NULL,
  t_clubs FLOAT NULL,
  t_chores FLOAT NULL,
  t_fixed FLOAT NULL,
  t_available FLOAT NULL,
  deadline_term FLOAT NULL,
  sleep_penalty FLOAT NULL,
  hours_slept FLOAT NULL,
  calculated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY ix_stress_date_calc (date, calculated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS schedule_blocks (
  id INT AUTO_INCREMENT PRIMARY KEY,
  date DATE NOT NULL,
  time_category ENUM('class','travel','clubs','chores','fixed','flexible') NOT NULL,
  label VARCHAR(255) NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  priority TINYINT NOT NULL DEFAULT 3,
  moveable BOOLEAN NOT NULL DEFAULT FALSE,
  skipped BOOLEAN NOT NULL DEFAULT FALSE,
  skip_reason VARCHAR(255) NULL,
  original_start_time TIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_blocks_date (date),
  KEY ix_blocks_date_category (date, time_category),
  KEY ix_blocks_skipped (skipped)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- InnoDB never treats NULL as equal inside a UNIQUE index, so the composite
-- (commit_sha, file_path) key only constrains real commits; no commit records
-- (commit_sha IS NULL) may repeat freely. file_path is capped at 700 chars so
-- the full key fits the 3072 byte InnoDB index limit under utf8mb4.
CREATE TABLE IF NOT EXISTS github_commits (
  id INT AUTO_INCREMENT PRIMARY KEY,
  course_id INT NOT NULL,
  assignment_id INT NULL,
  repo_name VARCHAR(255) NOT NULL,
  branch VARCHAR(255) NOT NULL,
  file_path VARCHAR(700) NOT NULL,
  commit_sha CHAR(40) NULL,
  commit_message TEXT NULL,
  commit_author VARCHAR(255) NULL,
  commit_timestamp DATETIME NULL,
  file_size_bytes INT NULL,
  prev_file_size_bytes INT NULL,
  size_delta INT NULL,
  no_commit BOOLEAN NOT NULL DEFAULT FALSE,
  logged_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_commit_sha_path (commit_sha, file_path),
  KEY ix_commits_course (course_id),
  KEY ix_commits_assignment (assignment_id),
  KEY ix_commits_timestamp (commit_timestamp),
  KEY ix_commits_no_commit (no_commit),
  KEY ix_commits_repo_branch (repo_name, branch),
  CONSTRAINT fk_commits_course FOREIGN KEY (course_id)
    REFERENCES courses (id) ON DELETE CASCADE,
  CONSTRAINT fk_commits_assignment FOREIGN KEY (assignment_id)
    REFERENCES assignments (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS syllabus_parsed (
  id INT AUTO_INCREMENT PRIMARY KEY,
  course_id INT NOT NULL,
  raw_text LONGTEXT NULL,
  parsed_json JSON NULL,
  parse_model VARCHAR(128) NULL,
  parse_cost_usd DECIMAL(8,4) NULL,
  validated BOOLEAN NOT NULL DEFAULT FALSE,
  human_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
  quarter VARCHAR(32) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_syllabus_course (course_id),
  KEY ix_syllabus_quarter (quarter),
  CONSTRAINT fk_syllabus_course FOREIGN KEY (course_id)
    REFERENCES courses (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS error_log (
  id INT AUTO_INCREMENT PRIMARY KEY,
  timestamp DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  script_name VARCHAR(128) NOT NULL,
  error_type VARCHAR(128) NOT NULL,
  operation VARCHAR(255) NOT NULL,
  raw_message TEXT NULL,
  severity ENUM('warning','critical') NOT NULL DEFAULT 'warning',
  acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
  KEY ix_error_timestamp (timestamp),
  KEY ix_error_severity_ack (severity, acknowledged),
  KEY ix_error_script (script_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS poll_metrics (
  id INT AUTO_INCREMENT PRIMARY KEY,
  timestamp DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  api_name ENUM('canvas','github','oura') NOT NULL,
  endpoint VARCHAR(512) NULL,
  response_time_us INT NULL,
  http_status SMALLINT NULL,
  KEY ix_metrics_api_time (api_name, timestamp),
  KEY ix_metrics_status (http_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS poller_status (
  api_name ENUM('canvas','github','oura') NOT NULL PRIMARY KEY,
  state ENUM('running','paused','auth_failed','backoff') NOT NULL DEFAULT 'running',
  last_cycle_at DATETIME(6) NULL,
  last_cycle_duration_us INT NULL,
  last_error TEXT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS process_state (
  process_name VARCHAR(32) NOT NULL PRIMARY KEY,
  pid INT NULL,
  started_at DATETIME NULL,
  last_heartbeat DATETIME NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
