-- Run 11 migration: per class workload presets, class meeting times from the syllabus, and generated schedule blocks.
-- ADD COLUMN IF NOT EXISTS is MariaDB syntax; on MySQL 8 the installer drops the clause.

-- Multiplier on estimated hours for this class (0.05 = 5%). NULL: stress.hour_scale_subjects / stress.hour_scale_default.
ALTER TABLE courses ADD COLUMN IF NOT EXISTS hour_scale DECIMAL(5,3) NULL;

-- Difficulty (1 to 5) per assignment type for one class. A missing row falls back to difficulty_presets in config.yaml.
CREATE TABLE IF NOT EXISTS course_presets (
  course_id INT NOT NULL,
  assignment_type VARCHAR(32) NOT NULL,
  difficulty TINYINT NOT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (course_id, assignment_type),
  CONSTRAINT fk_course_presets_course FOREIGN KEY (course_id) REFERENCES courses (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Weekly class meetings. source 'syllabus' rows are replaced on every new parse; 'edited' rows (changed in the GUI) win and are kept.
-- days: weekday letters M T W R F S U (R = Thursday, U = Sunday), e.g. "MWF".
CREATE TABLE IF NOT EXISTS class_meetings (
  id INT AUTO_INCREMENT PRIMARY KEY,
  course_id INT NOT NULL,
  kind VARCHAR(32) NOT NULL DEFAULT 'lecture',
  days VARCHAR(7) NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  location VARCHAR(255) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'syllabus',
  syllabus_parsed_id INT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY ix_class_meetings_course (course_id),
  CONSTRAINT fk_class_meetings_course FOREIGN KEY (course_id) REFERENCES courses (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Blocks the planner generates ('class' from class_meetings, 'study' for assignments) are rebuilt each run; NULL source = added by hand.
ALTER TABLE schedule_blocks ADD COLUMN IF NOT EXISTS source VARCHAR(16) NULL;
ALTER TABLE schedule_blocks ADD COLUMN IF NOT EXISTS course_id INT NULL;
ALTER TABLE schedule_blocks ADD COLUMN IF NOT EXISTS assignment_id INT NULL;
