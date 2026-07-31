package xiaozhi.modules.device.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockStatic;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.MockedStatic;

import xiaozhi.common.exception.ErrorCode;
import xiaozhi.common.redis.RedisUtils;
import xiaozhi.common.user.UserDetail;
import xiaozhi.common.utils.MessageUtils;
import xiaozhi.common.utils.Result;
import xiaozhi.modules.device.dto.DeviceRebindDTO;
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

    private DeviceController controller(DeviceService deviceService) {
        return new DeviceController(
                deviceService,
                mock(DeviceAddressBookService.class),
                mock(DeviceAttributeService.class),
                mock(RedisUtils.class),
                mock(SysParamsService.class));
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
