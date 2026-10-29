package xiaozhi.modules.device.location;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public record CellLocationSettings(
        @Value("${KSZ_CELL_LOCATION_ENABLED:false}") boolean enabled,
        @Value("${KSZ_TANSHU_API_KEY:}") String apiKey,
        @Value("${KSZ_CELL_LOCATION_CACHE_SECONDS:2592000}") long cacheSeconds,
        @Value("${KSZ_CELL_LOCATION_RETRY_SECONDS:60}") long retrySeconds,
        @Value("${KSZ_CELL_REPORT_MAX_AGE_SECONDS:86400}") long reportMaxAgeSeconds,
        @Value("${KSZ_LOCATION_DEFAULT_CITY:}") String defaultCity) {
    public CellLocationSettings {
        if (cacheSeconds <= 0 || retrySeconds <= 0 || reportMaxAgeSeconds <= 0) {
            throw new IllegalArgumentException("定位缓存和重试时间必须大于零");
        }
    }
}
