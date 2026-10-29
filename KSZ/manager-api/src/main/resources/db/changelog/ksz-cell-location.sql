CREATE TABLE ksz_cell_location_cache (
    cell_key VARCHAR(128) NOT NULL PRIMARY KEY,
    result_json LONGTEXT NULL,
    fetched_at BIGINT NOT NULL DEFAULT 0,
    expires_at BIGINT NOT NULL DEFAULT 0,
    retry_after BIGINT NOT NULL DEFAULT 0,
    error_code VARCHAR(64) NULL
);

CREATE TABLE ksz_device_location (
    device_id VARCHAR(64) NOT NULL PRIMARY KEY,
    cell_report LONGTEXT NULL,
    reported_at BIGINT NOT NULL DEFAULT 0,
    mode VARCHAR(16) NOT NULL DEFAULT 'cell',
    configured_city VARCHAR(100) NULL
);
