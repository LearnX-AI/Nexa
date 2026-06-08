CREATE DATABASE IF NOT EXISTS nexa_ai CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE nexa_ai;

CREATE TABLE IF NOT EXISTS nexa_chat_logs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    log_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NULL,
    user_email VARCHAR(255) NULL,
    user_name TEXT NOT NULL,
    user_prompt LONGTEXT NOT NULL,
    nexa_response LONGTEXT NOT NULL,
    image_base64 LONGTEXT NULL,
    image_blob LONGBLOB NULL,
    image_mime_type VARCHAR(100) NULL,
    image_filename VARCHAR(255) NULL,
    image_saved_at DATETIME NULL,
    timestamp_utc DATETIME NOT NULL,
    stars TINYINT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_log_id (log_id),
    KEY idx_session_id (session_id),
    KEY idx_user_email (user_email),
    KEY idx_timestamp_utc (timestamp_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
