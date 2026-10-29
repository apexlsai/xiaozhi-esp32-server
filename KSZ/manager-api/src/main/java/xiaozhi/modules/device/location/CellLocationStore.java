package xiaozhi.modules.device.location;

import java.util.List;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import lombok.RequiredArgsConstructor;
import xiaozhi.common.utils.JsonUtils;
import xiaozhi.modules.device.dto.DeviceCellReport;

@Repository
@RequiredArgsConstructor
public class CellLocationStore {
    private final JdbcTemplate jdbc;

    public record DeviceRow(DeviceCellReport report, String mode, String city) {
    }

    public record CachedRow(CellGeo geo, long fetchedAt, long expiresAt, long retryAfter) {
    }

    public void record(DeviceCellReport report) {
        jdbc.update("""
                INSERT INTO ksz_device_location (device_id, cell_report, reported_at) VALUES (?, ?, ?)
                ON DUPLICATE KEY UPDATE
                cell_report=CASE WHEN VALUES(reported_at)>=reported_at THEN VALUES(cell_report) ELSE cell_report END,
                reported_at=GREATEST(reported_at, VALUES(reported_at))
                """, report.deviceId(), JsonUtils.toJsonString(report), report.reportedAt());
    }

    public DeviceRow device(String deviceId) {
        List<DeviceRow> rows = jdbc.query(
                "SELECT cell_report, mode, configured_city FROM ksz_device_location WHERE device_id=?",
                (rs, index) -> new DeviceRow(rs.getString(1) == null ? null
                        : JsonUtils.parseObject(rs.getString(1), DeviceCellReport.class),
                        rs.getString(2), rs.getString(3)), deviceId);
        return rows.isEmpty() ? new DeviceRow(null, "cell", null) : rows.getFirst();
    }

    public void configure(String deviceId, String mode, String city) {
        jdbc.update("""
                INSERT INTO ksz_device_location (device_id, mode, configured_city) VALUES (?, ?, ?)
                ON DUPLICATE KEY UPDATE mode=VALUES(mode), configured_city=VALUES(configured_city)
                """, deviceId, mode, city);
    }

    public CachedRow cached(String key) {
        List<CachedRow> rows = jdbc.query(
                "SELECT result_json, fetched_at, expires_at, retry_after FROM ksz_cell_location_cache WHERE cell_key=?",
                (rs, index) -> new CachedRow(rs.getString(1) == null ? null
                        : JsonUtils.parseObject(rs.getString(1), CellGeo.class),
                        rs.getLong(2), rs.getLong(3), rs.getLong(4)), key);
        return rows.isEmpty() ? null : rows.getFirst();
    }

    public boolean claim(String key, long now) {
        jdbc.update("INSERT IGNORE INTO ksz_cell_location_cache (cell_key) VALUES (?)", key);
        return jdbc.update("""
                UPDATE ksz_cell_location_cache SET retry_after=? WHERE cell_key=?
                AND retry_after<=? AND (result_json IS NULL OR expires_at<=?)
                """, now + 15000, key, now, now) == 1;
    }

    public void success(String key, CellGeo geo, long now, long expiresAt) {
        jdbc.update("""
                UPDATE ksz_cell_location_cache SET result_json=?, fetched_at=?, expires_at=?,
                retry_after=0, error_code=NULL WHERE cell_key=?
                """, JsonUtils.toJsonString(geo), now, expiresAt, key);
    }

    public void failure(String key, String code, long retryAfter) {
        jdbc.update("UPDATE ksz_cell_location_cache SET retry_after=?, error_code=? WHERE cell_key=?",
                retryAfter, code, key);
    }
}
