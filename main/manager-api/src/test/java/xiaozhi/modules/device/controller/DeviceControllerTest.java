package xiaozhi.modules.device.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockStatic;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.Map;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.MockedStatic;
import org.springframework.web.client.RestTemplate;

import xiaozhi.common.exception.ErrorCode;
import xiaozhi.common.exception.RenException;
import xiaozhi.common.redis.RedisUtils;
import xiaozhi.common.user.UserDetail;
import xiaozhi.common.utils.MessageUtils;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.device.dto.DeviceRebindDTO;
import xiaozhi.modules.device.dto.DeviceEventReportDTO;
import xiaozhi.modules.device.dto.DeviceUpdateDTO;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceAddressBookService;
import xiaozhi.modules.device.service.DeviceAttributeService;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.device.vo.DeviceRebindVO;
import xiaozhi.modules.security.user.SecurityUser;
import xiaozhi.modules.sys.service.SysParamsService;

@DisplayName("设备接口回归测试")
class DeviceControllerTest {

    private static final String DEVICE_ID = "device-id";
    private static final long USER_ID = 1L;

    @Test
    @DisplayName("数据库未更新时不误报自动升级状态修改成功")
    void updateFailureIsReturnedToCaller() {
        DeviceService deviceService = mock(DeviceService.class);
        DeviceEntity entity = ownedDevice();
        when(deviceService.selectById(DEVICE_ID)).thenReturn(entity);
        when(deviceService.updateById(entity)).thenReturn(false);
        DeviceController controller = controller(deviceService);

        DeviceUpdateDTO update = new DeviceUpdateDTO();
        update.setAutoUpdate(0);

        try (MockedStatic<SecurityUser> securityUser = mockStatic(SecurityUser.class);
                MockedStatic<MessageUtils> messageUtils = mockStatic(MessageUtils.class)) {
            securityUser.when(SecurityUser::getUser).thenReturn(currentUser());
            messageUtils.when(() -> MessageUtils.getMessage(ErrorCode.UPDATE_DATA_FAILED))
                    .thenReturn("Failed to update data");

            Result<Void> result = controller.updateDeviceInfo(DEVICE_ID, update);

            assertEquals(ErrorCode.UPDATE_DATA_FAILED, result.getCode());
            assertEquals(0, entity.getAutoUpdate());
            verify(deviceService).updateById(entity);
        }
    }

    @Test
    @DisplayName("换绑接口委托 DeviceService 并返回成功结果")
    void rebindDelegatesToService() {
        DeviceService deviceService = mock(DeviceService.class);
        DeviceRebindDTO dto = new DeviceRebindDTO();
        dto.setDeviceId("aa:bb:cc:dd:ee:ff");
        dto.setCurrentAgentName("导游A");
        dto.setTargetAgentName("导游B");
        dto.setConfirm(true);
        DeviceRebindVO vo = new DeviceRebindVO();
        vo.setDeviceId(dto.getDeviceId());
        vo.setConfirmed(true);
        vo.setReconnectRequired(true);
        when(deviceService.rebindDevice(dto)).thenReturn(vo);

        Result<DeviceRebindVO> result = controller(deviceService).rebindDevice(dto);

        assertEquals(0, result.getCode());
        assertEquals(true, result.getData().getConfirmed());
        verify(deviceService).rebindDevice(dto);
    }

    @Test
    @DisplayName("目标测试智能体不存在时不保存语言也不调用内部接口")
    void missingTestAgentDoesNotPersistLanguage() {
        DeviceService deviceService = mock(DeviceService.class);
        DeviceAttributeService attributeService = mock(DeviceAttributeService.class);
        RestTemplate restTemplate = mock(RestTemplate.class);
        SysParamsService sysParamsService = mock(SysParamsService.class);
        DeviceEntity device = ownedDevice();
        when(deviceService.getDeviceByMacAddress(DEVICE_ID)).thenReturn(device);

        DeviceEventReportDTO dto = new DeviceEventReportDTO();
        dto.setDeviceId(DEVICE_ID);
        dto.setEvent("language_change");
        dto.setPayload(Map.of("language", "zh-CN-yue-test"));

        try (MockedStatic<MessageUtils> messageUtils = mockStatic(MessageUtils.class)) {
            messageUtils.when(() -> MessageUtils.getMessage(
                    ErrorCode.DEVICE_REBIND_TARGET_AGENT_NOT_FOUND, "小硕-粤语-测试"))
                    .thenReturn("需要名为小硕-粤语-测试的智能体");
            RenException failure = new RenException(
                    ErrorCode.DEVICE_REBIND_TARGET_AGENT_NOT_FOUND, "小硕-粤语-测试");
            when(deviceService.validateLanguageTargetAgent(DEVICE_ID, "zh-CN-yue-test"))
                    .thenThrow(failure);

            DeviceController controller = new DeviceController(
                    deviceService,
                    mock(DeviceAddressBookService.class),
                    attributeService,
                    mock(RedisUtils.class),
                    sysParamsService,
                    restTemplate);

            RenException thrown = assertThrows(RenException.class, () -> controller.reportDeviceEvent(dto));
            assertEquals("需要名为小硕-粤语-测试的智能体", thrown.getMsg());
        }

        verify(attributeService, never()).updateLanguage(DEVICE_ID, "zh-CN-yue-test");
        verify(sysParamsService, never()).getValue(org.mockito.ArgumentMatchers.anyString(),
                org.mockito.ArgumentMatchers.anyBoolean());
    }

    private DeviceController controller(DeviceService deviceService) {
        return new DeviceController(
                deviceService,
                mock(DeviceAddressBookService.class),
                mock(DeviceAttributeService.class),
                mock(RedisUtils.class),
                mock(SysParamsService.class),
                mock(RestTemplate.class));
    }

    private DeviceEntity ownedDevice() {
        DeviceEntity entity = new DeviceEntity();
        entity.setId(DEVICE_ID);
        entity.setUserId(USER_ID);
        entity.setAutoUpdate(1);
        return entity;
    }

    private UserDetail currentUser() {
        UserDetail user = new UserDetail();
        user.setId(USER_ID);
        return user;
    }
}
