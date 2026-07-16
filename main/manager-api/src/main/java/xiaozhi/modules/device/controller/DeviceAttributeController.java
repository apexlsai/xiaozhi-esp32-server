package xiaozhi.modules.device.controller;

import java.util.Map;

import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import lombok.AllArgsConstructor;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.device.entity.DeviceAttributeEntity;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceAttributeService;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.security.user.SecurityUser;

@Tag(name = "设备属性管理")
@RestController
@RequestMapping("/device/attribute")
@AllArgsConstructor
public class DeviceAttributeController {

    private final DeviceAttributeService deviceAttributeService;
    private final DeviceService deviceService;

    @GetMapping("/{deviceId}")
    @Operation(summary = "获取设备所有属性")
    @RequiresPermissions("sys:role:normal")
    public Result<Map<String, String>> list(@PathVariable String deviceId) {
        DeviceEntity device = deviceService.getDeviceByMacAddress(deviceId);
        if (device == null || !device.getUserId().equals(SecurityUser.getUser().getId())) {
            return new Result<Map<String, String>>().error("设备不存在");
        }
        return new Result<Map<String, String>>().ok(deviceAttributeService.getAttributesByDeviceId(deviceId));
    }

    @GetMapping("/{deviceId}/entity")
    @Operation(summary = "获取设备属性实体")
    @RequiresPermissions("sys:role:normal")
    public Result<DeviceAttributeEntity> getEntity(@PathVariable String deviceId) {
        DeviceEntity device = deviceService.getDeviceByMacAddress(deviceId);
        if (device == null || !device.getUserId().equals(SecurityUser.getUser().getId())) {
            return new Result<DeviceAttributeEntity>().error("设备不存在");
        }
        return new Result<DeviceAttributeEntity>().ok(deviceAttributeService.getByDeviceId(deviceId));
    }

    @PutMapping("/{deviceId}/language")
    @Operation(summary = "更新设备语言")
    @RequiresPermissions("sys:role:normal")
    public Result<Void> updateLanguage(@PathVariable String deviceId, @RequestBody(required = false) String language) {
        DeviceEntity device = deviceService.getDeviceByMacAddress(deviceId);
        if (device == null || !device.getUserId().equals(SecurityUser.getUser().getId())) {
            return new Result<Void>().error("设备不存在");
        }
        deviceAttributeService.updateLanguage(deviceId, language);
        return new Result<Void>();
    }

    @PutMapping("/{deviceId}/last_beacon_id")
    @Operation(summary = "更新设备蓝牙信标ID")
    @RequiresPermissions("sys:role:normal")
    public Result<Void> updateLastBeaconId(@PathVariable String deviceId,
            @RequestBody(required = false) String lastBeaconId) {
        DeviceEntity device = deviceService.getDeviceByMacAddress(deviceId);
        if (device == null || !device.getUserId().equals(SecurityUser.getUser().getId())) {
            return new Result<Void>().error("设备不存在");
        }
        deviceAttributeService.updateLastBeaconId(deviceId, lastBeaconId);
        return new Result<Void>();
    }

    @PutMapping("/{deviceId}/{attrKey}")
    @Operation(summary = "更新设备属性（兼容旧接口）")
    @RequiresPermissions("sys:role:normal")
    public Result<Void> update(@PathVariable String deviceId, @PathVariable String attrKey,
            @RequestBody(required = false) String attrValue) {
        DeviceEntity device = deviceService.getDeviceByMacAddress(deviceId);
        if (device == null || !device.getUserId().equals(SecurityUser.getUser().getId())) {
            return new Result<Void>().error("设备不存在");
        }
        deviceAttributeService.saveOrUpdateAttribute(deviceId, attrKey, attrValue);
        return new Result<Void>();
    }
}
