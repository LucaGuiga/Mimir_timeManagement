-- Run 7 migration. ADD COLUMN IF NOT EXISTS is MariaDB syntax; on MySQL 8 drop the clause and run once.
ALTER TABLE courses ADD COLUMN IF NOT EXISTS lab_parent_id INT NULL;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS is_lab BOOLEAN DEFAULT false;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS setup_complete BOOLEAN DEFAULT false;
