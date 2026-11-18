package xiaozhi.modules.device.controller;

import java.util.List;
import java.util.Locale;

import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.fasterxml.jackson.annotation.JsonProperty;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import lombok.RequiredArgsConstructor;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.device.dao.DeviceDao;
import xiaozhi.modules.device.entity.DeviceEntity;

@RestController
@RequiredArgsConstructor
@RequestMapping("/config/device-metadata")
public class DeviceMetadataController {
    private final DeviceDao devices;

    public record Request(
            @NotEmpty @Size(max = 100) List<@jakarta.validation.constraints.NotNull
                    @Pattern(regexp = "(?i)[0-9a-f]{2}(?::[0-9a-f]{2}){5}") String> deviceIds) {
    }

    public record Metadata(
            @JsonProperty("device_mac") String deviceMac,
            String model,
            @JsonProperty("firmware_version") String firmwareVersion) {
    }

    @PostMapping
    public Result<List<Metadata>> read(@Valid @RequestBody Request request) {
        List<String> macs = request.deviceIds().stream().map(value -> value.toLowerCase(Locale.ROOT))
                .distinct().toList();
        List<Metadata> result = devices.selectList(new LambdaQueryWrapper<DeviceEntity>()
                .select(DeviceEntity::getMacAddress, DeviceEntity::getBoard, DeviceEntity::getAppVersion)
                .in(DeviceEntity::getMacAddress, macs)).stream()
                .map(device -> new Metadata(device.getMacAddress().toUpperCase(Locale.ROOT),
                        device.getBoard(), device.getAppVersion()))
                .toList();
        return new Result<List<Metadata>>().ok(result);
    }
}
