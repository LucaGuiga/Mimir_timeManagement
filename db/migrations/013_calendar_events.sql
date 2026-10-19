-- Run 15 migration: Canvas calendar events (office hours, lecture sessions) are kept apart from assignments.
-- They come in on the same iCal feed; is_event keeps them out of reminders, stress, study blocks, emails and the dashboard.
-- ADD COLUMN IF NOT EXISTS is MariaDB syntax; on MySQL 8 the installer drops the clause.
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS is_event BOOLEAN NOT NULL DEFAULT FALSE;
-- iCal calendar events got negative ids (-1 .. about -4.3e9); assignments added through Telegram use -1e12 and below
UPDATE assignments SET is_event = TRUE WHERE canvas_assignment_id < 0 AND canvas_assignment_id > -1000000000000 AND source IS NULL;
