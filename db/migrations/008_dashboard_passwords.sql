-- Run 10 migration: dashboard passwords (salted scrypt hashes pushed to AWS) and the dashboard login log pulled back from AWS.
CREATE TABLE IF NOT EXISTS dashboard_passwords (
  label VARCHAR(64) NOT NULL PRIMARY KEY,
  salt CHAR(32) NOT NULL,
  hash CHAR(64) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ts is UTC. ts_raw is the timestamp string exactly as the API sent it, used as the next ?since= so the comparison matches the API's own format.
CREATE TABLE IF NOT EXISTS dashboard_access_log (
  id INT AUTO_INCREMENT PRIMARY KEY,
  ts DATETIME(6) NOT NULL,
  ts_raw VARCHAR(40) NOT NULL,
  ip VARCHAR(64) NOT NULL,
  outcome VARCHAR(20) NOT NULL,
  label VARCHAR(64) NULL,
  UNIQUE KEY uq_dashboard_access (ts, ip, outcome)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
