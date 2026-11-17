package xiaozhi.modules.device.controller;

import java.util.Locale;
import java.util.Set;

import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import lombok.RequiredArgsConstructor;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.device.location.CellLocationStore;
import xiaozhi.modules.device.location.DeviceGeo;
import xiaozhi.modules.device.location.DeviceLocationService;
import xiaozhi.modules.device.service.DeviceService;

@RestController
@RequiredArgsConstructor
@RequestMapping("/config/device-location")
public class DeviceLocationController {
    private final DeviceService devices;
    private final DeviceLocationService locations;
    private final CellLocationStore store;

    public record Request(String deviceId, String mode, String city) {
    }

    @PostMapping
    public Result<DeviceGeo> resolve(@RequestBody Request request) {
        String mac = mac(request.deviceId());
        if (mac == null || devices.getDeviceByMacAddress(mac) == null) {
            return new Result<DeviceGeo>().error("设备不存在");
        }
        return new Result<DeviceGeo>().ok(locations.resolve(mac));
    }

    @PostMapping("/settings")
    public Result<Void> configure(@RequestBody Request request) {
        String mac = mac(request.deviceId());
        String city = request.city() == null ? null : request.city().strip();
        if (mac == null || devices.getDeviceByMacAddress(mac) == null) {
            return new Result<Void>().error("设备不存在");
        }
        if (request.mode() == null || !Set.of("cell", "fixed", "disabled").contains(request.mode())
                || (city != null && (city.length() > 100 || city.chars().anyMatch(Character::isISOControl)))
                || ("fixed".equals(request.mode()) && (city == null || city.isEmpty()))) {
            return new Result<Void>().error("定位模式或城市配置无效");
        }
        store.configure(mac, request.mode(), city);
        return new Result<Void>().ok(null);
    }

    private String mac(String value) {
        return value != null && value.matches("(?i)[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
                ? value.toLowerCase(Locale.ROOT) : null;
    }
}
