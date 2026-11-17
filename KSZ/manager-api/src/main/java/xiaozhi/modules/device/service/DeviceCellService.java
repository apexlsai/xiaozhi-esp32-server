package xiaozhi.modules.device.service;

import java.util.Locale;

import org.springframework.stereotype.Service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import xiaozhi.common.redis.RedisUtils;
import xiaozhi.common.utils.JsonUtils;
import xiaozhi.modules.device.dto.DeviceCellReport;
import xiaozhi.modules.device.dto.DeviceReportReqDTO;
import xiaozhi.modules.device.location.CellLocationStore;

@Slf4j
@Service
@RequiredArgsConstructor
public class DeviceCellService {
    private final RedisUtils redisUtils;
    private final CellLocationStore locationStore;

    public void record(String deviceId, DeviceReportReqDTO request) {
        if (request == null || request.getBoard() == null || request.getBoard().getCell() == null) {
            return;
        }
        String mac = deviceId.toLowerCase(Locale.ROOT);
        DeviceCellReport report = new DeviceCellReport(mac, request.getBoard().getType(),
                System.currentTimeMillis(), request.getBoard().getCell());
        try {
            log.info("设备 {} board.cell: {}", mac, JsonUtils.toJsonString(report.cell()));
            redisUtils.set(key(mac), JsonUtils.toJsonString(report), RedisUtils.DEFAULT_EXPIRE);
        } catch (Exception e) {
            log.warn("设备 {} cell 上报缓存失败", mac, e);
        }
        try {
            locationStore.record(report);
        } catch (Exception e) {
            log.warn("设备 {} cell 持久化失败: {}", mac, e.getClass().getSimpleName());
        }
    }

    public DeviceCellReport get(String deviceId) {
        Object value = redisUtils.get(key(deviceId));
        return value == null ? null : JsonUtils.parseObject(value.toString(), DeviceCellReport.class);
    }

    private String key(String deviceId) {
        return "ksz:device:cell:" + deviceId.toLowerCase(Locale.ROOT);
    }
}
