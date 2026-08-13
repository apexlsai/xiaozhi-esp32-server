package xiaozhi.modules.device.controller;

import java.util.List;
import java.util.HashMap;
import java.util.Map;

import org.apache.commons.lang3.StringUtils;
import org.apache.shiro.authz.annotation.RequiresPermissions;
import org.springframework.beans.BeanUtils;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.RestTemplate;

import xiaozhi.common.constant.Constant;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import xiaozhi.common.exception.ErrorCode;
import xiaozhi.common.redis.RedisKeys;
import xiaozhi.common.redis.RedisUtils;
import xiaozhi.common.user.UserDetail;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.device.dto.DeviceAddressBookAliasDTO;
import xiaozhi.modules.device.dto.DeviceAddressBookPermissionDTO;
import xiaozhi.modules.device.dto.DeviceEventReportDTO;
import xiaozhi.modules.device.dto.DeviceManualAddDTO;
import xiaozhi.modules.device.dto.DeviceRebindDTO;
import xiaozhi.modules.device.dto.DeviceRegisterDTO;
import xiaozhi.modules.device.dto.DeviceToolsCallReqDTO;
import xiaozhi.modules.device.dto.DeviceUnBindDTO;
import xiaozhi.modules.device.dto.DeviceUpdateDTO;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceAddressBookService;
import xiaozhi.modules.device.service.DeviceAttributeService;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.device.vo.DeviceRebindVO;
import xiaozhi.modules.device.vo.UserShowDeviceListVO;
import xiaozhi.modules.security.user.SecurityUser;
import xiaozhi.modules.sys.service.SysParamsService;

@Tag(name = "设备管理")
@RestController
@RequestMapping("/device")
public class DeviceController {
    private final DeviceService deviceService;
    private final DeviceAddressBookService deviceAddressBookService;
    private final DeviceAttributeService deviceAttributeService;
    private final RedisUtils redisUtils;
    private final SysParamsService sysParamsService;
    private final RestTemplate restTemplate;

    public DeviceController(DeviceService deviceService, DeviceAddressBookService deviceAddressBookService,
            DeviceAttributeService deviceAttributeService, RedisUtils redisUtils, SysParamsService sysParamsService,
            RestTemplate restTemplate) {
        this.deviceService = deviceService;
        this.deviceAddressBookService = deviceAddressBookService;
        this.deviceAttributeService = deviceAttributeService;
        this.redisUtils = redisUtils;
        this.sysParamsService = sysParamsService;
        this.restTemplate = restTemplate;
    }

    @PostMapping("/bind/{agentId}/{deviceCode}")
    @Operation(summary = "绑定设备")
    @RequiresPermissions("sys:role:normal")
    public Result<Void> bindDevice(@PathVariable String agentId, @PathVariable String deviceCode) {
        deviceService.deviceActivation(agentId, deviceCode);
        return new Result<>();
    }

    @PostMapping("/register")
    @Operation(summary = "注册设备")
    public Result<String> registerDevice(@RequestBody DeviceRegisterDTO deviceRegisterDTO) {
        String macAddress = deviceRegisterDTO.getMacAddress();
        if (StringUtils.isBlank(macAddress)) {
            return new Result<String>().error(ErrorCode.MCA_NOT_NULL);
        }
        // 生成六位验证码
        String code;
        String key;
        String existsMac = null;
        do {
            code = String.valueOf(Math.random()).substring(2, 8);
            key = RedisKeys.getDeviceCaptchaKey(code);
            existsMac = (String) redisUtils.get(key);
        } while (StringUtils.isNotBlank(existsMac));

        redisUtils.set(key, macAddress);
        return new Result<String>().ok(code);
    }

    @PostMapping("/event/report")
    @Operation(summary = "上报设备事件（xiaozhi-server间调用）")
    public Result<Void> reportDeviceEvent(@RequestBody DeviceEventReportDTO dto) {
        if (dto == null || StringUtils.isBlank(dto.getDeviceId()) || StringUtils.isBlank(dto.getEvent())) {
            return new Result<Void>().error("参数错误");
        }
        String deviceId = dto.getDeviceId();
        DeviceEntity device = deviceService.getDeviceByMacAddress(deviceId);
        if (device == null) {
            return new Result<Void>().error("设备不存在");
        }

        // 处理语言变更事件
        if ("language_change".equals(dto.getEvent())) {
            Map<String, Object> eventPayload = dto.getPayload();
            Object language = eventPayload == null ? null : eventPayload.get("language");
            if (language == null) {
                return new Result<Void>().error("language 不能为空");
            }
            Object devValue = eventPayload.get("dev");
            if (eventPayload.containsKey("dev") && !(devValue instanceof Boolean)) {
                return new Result<Void>().error("dev 必须是布尔值");
            }
            String languageValue = language.toString();
            boolean dev = Boolean.TRUE.equals(devValue);
            String targetAgentName = deviceService.validateLanguageTargetAgent(deviceId, languageValue, dev);
            try {
                notifyLanguageChange(
                        deviceId,
                        languageValue,
                        dev,
                        targetAgentName,
                        eventPayload.get("request_id"));
            } catch (Exception e) {
                return new Result<Void>().error("下发设备语言切换命令失败: " + e.getMessage());
            }
        }
        // 处理蓝牙信标变更事件
        else if ("beacon_change".equals(dto.getEvent())) {
            Object beaconId = dto.getPayload() == null ? null : dto.getPayload().get("beacon_id");
            if (beaconId != null) {
                deviceAttributeService.updateLastBeaconId(deviceId, beaconId.toString());
            }
        }

        return new Result<Void>();
    }

    private void notifyLanguageChange(
            String deviceId,
            String language,
            boolean dev,
            String targetAgentName,
            Object requestId) {
        String internalApi = sysParamsService.getValue(Constant.SERVER_INTERNAL_API, false);
        if (StringUtils.isBlank(internalApi) || "null".equalsIgnoreCase(internalApi)) {
            throw new IllegalStateException("未配置 server.internal_api");
        }
        String secret = sysParamsService.getValue(Constant.SERVER_SECRET, false);
        if (StringUtils.isBlank(secret) || "null".equalsIgnoreCase(secret)) {
            throw new IllegalStateException("未配置 server.secret");
        }

        HttpHeaders headers = new HttpHeaders();
        headers.setBearerAuth(secret);
        headers.setContentType(MediaType.APPLICATION_JSON);
        Map<String, Object> payload = new HashMap<>();
        payload.put("deviceId", deviceId);
        payload.put("language", language);
        payload.put("dev", dev);
        payload.put("targetAgentName", targetAgentName);
        if (requestId != null) {
            payload.put("requestId", requestId);
        }
        restTemplate.postForEntity(
                StringUtils.removeEnd(internalApi, "/") + "/internal/device/language-change-v2",
                new HttpEntity<>(payload, headers),
                Void.class);
    }

    @PostMapping("/rebind")
    @Operation(summary = "设备智能体换绑（xiaozhi-server间调用）")
    public Result<DeviceRebindVO> rebindDevice(@Valid @RequestBody DeviceRebindDTO dto) {
        return new Result<DeviceRebindVO>().ok(deviceService.rebindDevice(dto));
    }

    @GetMapping("/bind/{agentId}")
    @Operation(summary = "获取已绑定设备")
    @RequiresPermissions("sys:role:normal")
    public Result<List<UserShowDeviceListVO>> getUserDevices(@PathVariable String agentId) {
        UserDetail user = SecurityUser.getUser();
        List<UserShowDeviceListVO> devices = deviceService.getUserDeviceList(user.getId(), agentId);
        return new Result<List<UserShowDeviceListVO>>().ok(devices);
    }

    @PostMapping("/bind/{agentId}")
    @Operation(summary = "设备在线接口")
    @RequiresPermissions("sys:role:normal")
    public Result<String> forwardToMqttGateway(@PathVariable String agentId, @RequestBody String requestBody) {
        try {
            return new Result<String>().ok(deviceService.getDeviceOnlineData(agentId));
        } catch (Exception e) {
            return new Result<String>().error("转发请求失败: " + e.getMessage());
        }
    }

    @PostMapping("/unbind")
    @Operation(summary = "解绑设备")
    @RequiresPermissions("sys:role:normal")
    public Result<Void> unbindDevice(@RequestBody DeviceUnBindDTO unDeviveBind) {
        UserDetail user = SecurityUser.getUser();
        deviceService.unbindDevice(user.getId(), unDeviveBind.getDeviceId());
        return new Result<Void>();
    }

    @PutMapping("/update/{id}")
    @Operation(summary = "更新设备信息")
    @RequiresPermissions("sys:role:normal")
    public Result<Void> updateDeviceInfo(@PathVariable String id, @Valid @RequestBody DeviceUpdateDTO deviceUpdateDTO) {
        DeviceEntity entity = deviceService.selectById(id);
        if (entity == null) {
            return new Result<Void>().error("设备不存在");
        }
        UserDetail user = SecurityUser.getUser();
        if (!entity.getUserId().equals(user.getId())) {
            return new Result<Void>().error("设备不存在");
        }
        BeanUtils.copyProperties(deviceUpdateDTO, entity);
        if (!deviceService.updateById(entity)) {
            return new Result<Void>().error(ErrorCode.UPDATE_DATA_FAILED);
        }
        return new Result<Void>();
    }

    @PostMapping("/manual-add")
    @Operation(summary = "手动添加设备")
    @RequiresPermissions("sys:role:normal")
    public Result<Void> manualAddDevice(@RequestBody @Valid DeviceManualAddDTO dto) {
        UserDetail user = SecurityUser.getUser();
        deviceService.manualAddDevice(user.getId(), dto);
        return new Result<>();
    }

    @PostMapping("/tools/list/{deviceId}")
    @Operation(summary = "获取设备工具列表")
    @RequiresPermissions("sys:role:normal")
    public Result<Object> getDeviceTools(@PathVariable String deviceId) {
        Object toolsData = deviceService.getDeviceTools(deviceId);
        if (toolsData == null) {
            return new Result<Object>().error(ErrorCode.DEVICE_NOT_EXIST);
        }

        return new Result<Object>().ok(toolsData);
    }

    @PostMapping("/tools/call/{deviceId}")
    @Operation(summary = "调用设备工具")
    @RequiresPermissions("sys:role:normal")
    public Result<Object> callDeviceTool(@PathVariable String deviceId,
            @Valid @RequestBody DeviceToolsCallReqDTO request) {
        String toolName = request.getName();
        Map<String, Object> arguments = request.getArguments();

        Object result = deviceService.callDeviceTool(deviceId, toolName, arguments);
        if (result == null) {
            return new Result<Object>().error(ErrorCode.DEVICE_NOT_EXIST);
        }

        Result<Object> response = new Result<Object>();
        response.setMsg("Tools called successfully");
        return response.ok(result);
    }

    @GetMapping("/address-book/{macAddress}")
    @Operation(summary = "获取设备通讯录")
    @RequiresPermissions("sys:role:normal")
    public Result<Object> getAddressBook(@PathVariable String macAddress) {
        return new Result<Object>().ok(deviceAddressBookService.getAddressBookList(macAddress));
    }

    @GetMapping("/address-book/call")
    @Operation(summary = "根据昵称发起呼叫")
    public Result<Map<String, Object>> callByNickname(String callerMac, String nickname,
            @RequestParam(required = false, defaultValue = "false") boolean answer) {
        Map<String, Object> result = deviceAddressBookService.callByNickname(callerMac, nickname, answer);
        if (result == null) {
            return new Result<Map<String, Object>>().error("未找到对应设备");
        }
        return new Result<Map<String, Object>>().ok(result);
    }

    @PutMapping("/address-book/alias")
    @Operation(summary = "更新设备通讯录别名")
    @RequiresPermissions("sys:role:normal")
    public Result<Void> updateAlias(@Valid @RequestBody DeviceAddressBookAliasDTO dto) {
        UserDetail user = SecurityUser.getUser();
        DeviceEntity callerDevice = deviceService.getDeviceByMacAddress(dto.getMacAddress());
        if (callerDevice == null || !callerDevice.getUserId().equals(user.getId())) {
            return new Result<Void>().error("无权限操作该设备");
        }
        deviceAddressBookService.saveOrUpdate(dto.getMacAddress(), dto.getTargetMac(), dto.getAlias(), null);
        return new Result<Void>();
    }

    @PutMapping("/address-book/permission")
    @Operation(summary = "更新设备通讯录权限")
    @RequiresPermissions("sys:role:normal")
    public Result<Void> updatePermission(@Valid @RequestBody DeviceAddressBookPermissionDTO dto) {
        UserDetail user = SecurityUser.getUser();
        DeviceEntity callerDevice = deviceService.getDeviceByMacAddress(dto.getMacAddress());
        if (callerDevice == null || !callerDevice.getUserId().equals(user.getId())) {
            return new Result<Void>().error("无权限操作该设备");
        }
        deviceAddressBookService.saveOrUpdate(dto.getMacAddress(), dto.getTargetMac(), null, dto.getHasPermission());
        return new Result<Void>();
    }
}
