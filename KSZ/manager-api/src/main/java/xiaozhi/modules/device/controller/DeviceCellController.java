package xiaozhi.modules.device.controller;

import java.util.Locale;

import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import io.swagger.v3.oas.annotations.Operation;
import lombok.RequiredArgsConstructor;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.device.dto.DeviceCellReport;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceCellService;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.security.user.SecurityUser;

@RestController
@RequiredArgsConstructor
@RequestMapping("/device")
public class DeviceCellController {
    private final DeviceService deviceService;
    private final DeviceCellService deviceCellService;

    @GetMapping("/{deviceId}/cell")
    @Operation(summary = "获取设备最近上报的蜂窝小区信息")
    @RequiresPermissions("sys:role:normal")
    public Result<DeviceCellReport> get(@PathVariable String deviceId) {
        String mac = deviceId.toLowerCase(Locale.ROOT);
        Long userId = SecurityUser.getUserId();
        DeviceEntity device = deviceService.getDeviceByMacAddress(mac);
        if (userId == null || device == null || !userId.equals(device.getUserId())) {
            return new Result<DeviceCellReport>().error("设备不存在");
        }
        return new Result<DeviceCellReport>().ok(deviceCellService.get(mac));
    }
}
