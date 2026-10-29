package xiaozhi.modules.device.location;

import org.springframework.stereotype.Service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import xiaozhi.modules.device.dto.DeviceCellReport;
import xiaozhi.modules.device.service.DeviceCellService;

@Slf4j
@Service
@RequiredArgsConstructor
public class DeviceLocationService {
    private final CellLocationStore store;
    private final DeviceCellService reports;
    private final CellLocationSettings settings;
    private final TanshuCellClient client;

    public DeviceGeo resolve(String deviceId) {
        long now = System.currentTimeMillis();
        CellLocationStore.DeviceRow device = store.device(deviceId);
        DeviceCellReport report = device.report();
        try {
            DeviceCellReport recent = reports.get(deviceId);
            if (recent != null && (report == null || recent.reportedAt() > report.reportedAt())) {
                store.record(recent);
                report = recent;
            }
        } catch (Exception error) {
            log.debug("设备 {} cell Redis 回读失败，使用数据库记录", deviceId);
        }
        boolean hasCell = report != null;
        String city = device.city() == null ? settings.defaultCity() : device.city();
        if ("fixed".equals(device.mode())) {
            return fallback(city, true, hasCell, "configured", now, 0);
        }
        if ("disabled".equals(device.mode())) {
            return fallback(city, false, hasCell, "disabled", now, 0);
        }
        if (!hasCell) {
            if (device.city() != null && !device.city().isBlank()) {
                return fallback(city, false, false, "no_cell", now, 0);
            }
            return new DeviceGeo("ip", null, null, 0, now + 60000, false, "no_cell", 0);
        }
        if (!settings.enabled()) {
            return fallback(city, false, true, "disabled", now, 0);
        }
        if (now - report.reportedAt() > settings.reportMaxAgeSeconds() * 1000L) {
            return fallback(city, false, true, "report_expired", now, 0);
        }
        CellKey key = CellKey.from(report.cell());
        if (key == null) {
            return fallback(city, false, true, "invalid_or_unsupported_cell", now, 0);
        }
        CellLocationStore.CachedRow cached = store.cached(key.value());
        if (cached != null && cached.geo() != null && cached.expiresAt() > now) {
            return located(cached, report);
        }
        if (settings.apiKey().isBlank()) {
            return fallback(city, false, true, "missing_api_key", now, 0);
        }
        if (!store.claim(key.value(), now)) {
            cached = store.cached(key.value());
            return fallback(city, false, true, "retry_pending", now,
                    cached == null ? now + 15000 : cached.retryAfter());
        }
        try {
            CellGeo geo = client.lookup(key, settings.apiKey());
            long fetched = System.currentTimeMillis();
            long expires = fetched + settings.cacheSeconds() * 1000L;
            store.success(key.value(), geo, fetched, expires);
            DeviceCellReport current = store.device(deviceId).report();
            if (current != null && !key.equals(CellKey.from(current.cell()))) {
                return fallback(city, false, true, "cell_changed", fetched, fetched + 1000);
            }
            log.info("设备 {} 基站定位成功: city={} radius={}m", deviceId, geo.city(), geo.radius());
            return located(new CellLocationStore.CachedRow(geo, fetched, expires, 0), report);
        } catch (Exception error) {
            if (error instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            long retryAt = System.currentTimeMillis() + settings.retrySeconds() * 1000L;
            String code = error.getMessage() != null && error.getMessage().matches("(?:provider_|http_)[0-9]+")
                    ? error.getMessage() : error.getClass().getSimpleName();
            store.failure(key.value(), code, retryAt);
            log.warn("设备 {} 基站定位失败: {}", deviceId, code);
            return fallback(city, false, true, "lookup_failed", now, retryAt);
        }
    }

    private DeviceGeo located(CellLocationStore.CachedRow cached, DeviceCellReport report) {
        return new DeviceGeo("cell", cached.geo().city(), cached.geo(), cached.fetchedAt(),
                Math.min(cached.expiresAt(), report.reportedAt() + settings.reportMaxAgeSeconds() * 1000L),
                true, "ok", 0);
    }

    private DeviceGeo fallback(String city, boolean fixed, boolean hasCell, String status, long now, long retryAt) {
        String normalized = city == null ? "" : city.strip();
        return new DeviceGeo(normalized.isEmpty() ? "unknown" : fixed ? "fixed" : "default",
                normalized.isEmpty() ? null : normalized, null, 0,
                retryAt > now ? retryAt : now + 60000, hasCell, status, retryAt);
    }
}
