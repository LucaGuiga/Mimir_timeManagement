-- Run 13 migration: both stress equations are calculated; ratio is the primary one (stress.equation), alt_ratio the other.
-- ADD COLUMN IF NOT EXISTS is MariaDB syntax; on MySQL 8 the installer drops the clause.
ALTER TABLE stress_scores ADD COLUMN IF NOT EXISTS equation VARCHAR(8) NULL;
ALTER TABLE stress_scores ADD COLUMN IF NOT EXISTS alt_ratio FLOAT NULL;
