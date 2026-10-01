-- Oura OAuth2 tokens (one row, id = 1) and pending authorization states. Tokens live only in the local database.
CREATE TABLE IF NOT EXISTS oura_auth (
  id INT NOT NULL PRIMARY KEY,
  access_token TEXT NULL,
  refresh_token TEXT NULL,
  expires_at DATETIME NULL,
  scopes VARCHAR(500) NULL,
  connected_at DATETIME NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  state ENUM('connected','revoked','error') NOT NULL DEFAULT 'connected',
  last_error VARCHAR(500) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS oura_oauth_state (
  state VARCHAR(100) NOT NULL PRIMARY KEY,
  expires_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
