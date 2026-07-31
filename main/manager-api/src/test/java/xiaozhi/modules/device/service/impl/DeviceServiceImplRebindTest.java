package xiaozhi.modules.device.service.impl;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockStatic;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.Collections;
import java.util.List;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.MockedStatic;
import org.springframework.test.util.ReflectionTestUtils;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;

import xiaozhi.common.exception.ErrorCode;
import xiaozhi.common.exception.RenException;
import xiaozhi.common.redis.RedisUtils;
import xiaozhi.common.utils.MessageUtils;
import xiaozhi.modules.agent.dao.AgentDao;
import xiaozhi.modules.agent.entity.AgentEntity;
import xiaozhi.modules.device.dao.DeviceDao;
import xiaozhi.modules.device.dto.DeviceRebindDTO;
import xiaozhi.modules.device.entity.DeviceAttributeEntity;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceAddressBookService;
import xiaozhi.modules.device.service.DeviceAttributeService;
import xiaozhi.modules.device.service.OtaService;
import xiaozhi.modules.device.vo.DeviceRebindVO;
import xiaozhi.modules.sys.service.SysParamsService;
import xiaozhi.modules.sys.service.SysUserUtilService;

@DisplayName("设备智能体换绑")
class DeviceServiceImplRebindTest {

    private static final String MAC = "aa:bb:cc:dd:ee:ff";
    private static final String OLD_AGENT_ID = "agent-old";
    private static final String NEW_AGENT_ID = "agent-new";
    private static final long USER_ID = 7L;

    private DeviceDao deviceDao;
    private RedisUtils redisUtils;
    private DeviceAttributeService deviceAttributeService;
    private AgentDao agentDao;
    private DeviceServiceImpl service;

    @BeforeEach
    void setUp() {
        deviceDao = mock(DeviceDao.class);
        redisUtils = mock(RedisUtils.class);
        deviceAttributeService = mock(DeviceAttributeService.class);
        agentDao = mock(AgentDao.class);
        service = new DeviceServiceImpl(
                deviceDao,
                mock(SysUserUtilService.class),
                mock(SysParamsService.class),
                redisUtils,
                mock(OtaService.class),
                mock(DeviceAddressBookService.class),
                deviceAttributeService,
                agentDao);
        ReflectionTestUtils.setField(service, "baseDao", deviceDao);
    }

    @Test
    @DisplayName("成功换绑：条件更新、属性同步、回读确认与缓存清理")
    void rebindSucceedsWithConfirmation() {
        DeviceEntity device = device(OLD_AGENT_ID);
        AgentEntity current = agent(OLD_AGENT_ID, "导游A");
        AgentEntity target = agent(NEW_AGENT_ID, "导游B");
        DeviceEntity refreshed = device(NEW_AGENT_ID);
        DeviceAttributeEntity attribute = new DeviceAttributeEntity();
        attribute.setDeviceId(MAC);
        attribute.setAgentName("导游B");

        when(deviceDao.selectOne(any(QueryWrapper.class))).thenReturn(device);
        when(agentDao.selectById(OLD_AGENT_ID)).thenReturn(current);
        when(agentDao.selectList(any(QueryWrapper.class))).thenReturn(List.of(target));
        when(deviceDao.update(isNull(), any(UpdateWrapper.class))).thenReturn(1);
        when(deviceDao.selectById(MAC)).thenReturn(refreshed);
        when(agentDao.selectById(NEW_AGENT_ID)).thenReturn(target);
        when(deviceAttributeService.getByDeviceId(MAC)).thenReturn(attribute);

        DeviceRebindVO result = service.rebindDevice(request("导游A", "导游B", true));

        assertEquals(MAC, result.getDeviceId());
        assertEquals(OLD_AGENT_ID, result.getPreviousAgentId());
        assertEquals("导游A", result.getPreviousAgentName());
        assertEquals(NEW_AGENT_ID, result.getAgentId());
        assertEquals("导游B", result.getAgentName());
        assertTrue(result.getConfirmed());
        assertTrue(result.getReconnectRequired());
        verify(deviceAttributeService).updateAgentName(MAC, "导游B");
        verify(redisUtils).delete(anyList());
    }

    @Test
    @DisplayName("未确认时拒绝换绑")
    void rejectsWithoutConfirm() {
        try (MockedStatic<MessageUtils> messageUtils = mockMessageUtils()) {
            RenException ex = assertThrows(RenException.class,
                    () -> service.rebindDevice(request("导游A", "导游B", false)));
            assertEquals(ErrorCode.DEVICE_REBIND_CONFIRM_REQUIRED, ex.getCode());
        }
        verify(deviceDao, never()).update(any(), any());
    }

    @Test
    @DisplayName("当前智能体名称不匹配时拒绝")
    void rejectsCurrentAgentMismatch() {
        when(deviceDao.selectOne(any(QueryWrapper.class))).thenReturn(device(OLD_AGENT_ID));
        when(agentDao.selectById(OLD_AGENT_ID)).thenReturn(agent(OLD_AGENT_ID, "导游A"));

        try (MockedStatic<MessageUtils> messageUtils = mockMessageUtils()) {
            RenException ex = assertThrows(RenException.class,
                    () -> service.rebindDevice(request("错误名称", "导游B", true)));
            assertEquals(ErrorCode.DEVICE_REBIND_CURRENT_AGENT_MISMATCH, ex.getCode());
        }
    }

    @Test
    @DisplayName("目标智能体不存在时拒绝")
    void rejectsMissingTargetAgent() {
        when(deviceDao.selectOne(any(QueryWrapper.class))).thenReturn(device(OLD_AGENT_ID));
        when(agentDao.selectById(OLD_AGENT_ID)).thenReturn(agent(OLD_AGENT_ID, "导游A"));
        when(agentDao.selectList(any(QueryWrapper.class))).thenReturn(Collections.emptyList());

        try (MockedStatic<MessageUtils> messageUtils = mockMessageUtils()) {
            RenException ex = assertThrows(RenException.class,
                    () -> service.rebindDevice(request("导游A", "不存在", true)));
            assertEquals(ErrorCode.DEVICE_REBIND_TARGET_AGENT_NOT_FOUND, ex.getCode());
        }
    }

    @Test
    @DisplayName("同用户目标智能体重名时拒绝")
    void rejectsAmbiguousTargetAgent() {
        when(deviceDao.selectOne(any(QueryWrapper.class))).thenReturn(device(OLD_AGENT_ID));
        when(agentDao.selectById(OLD_AGENT_ID)).thenReturn(agent(OLD_AGENT_ID, "导游A"));
        when(agentDao.selectList(any(QueryWrapper.class)))
                .thenReturn(List.of(agent("a1", "导游B"), agent("a2", "导游B")));

        try (MockedStatic<MessageUtils> messageUtils = mockMessageUtils()) {
            RenException ex = assertThrows(RenException.class,
                    () -> service.rebindDevice(request("导游A", "导游B", true)));
            assertEquals(ErrorCode.DEVICE_REBIND_TARGET_AGENT_AMBIGUOUS, ex.getCode());
        }
    }

    @Test
    @DisplayName("条件更新冲突时拒绝")
    void rejectsUpdateConflict() {
        when(deviceDao.selectOne(any(QueryWrapper.class))).thenReturn(device(OLD_AGENT_ID));
        when(agentDao.selectById(OLD_AGENT_ID)).thenReturn(agent(OLD_AGENT_ID, "导游A"));
        when(agentDao.selectList(any(QueryWrapper.class))).thenReturn(List.of(agent(NEW_AGENT_ID, "导游B")));
        when(deviceDao.update(isNull(), any(UpdateWrapper.class))).thenReturn(0);

        try (MockedStatic<MessageUtils> messageUtils = mockMessageUtils()) {
            RenException ex = assertThrows(RenException.class,
                    () -> service.rebindDevice(request("导游A", "导游B", true)));
            assertEquals(ErrorCode.DEVICE_REBIND_CONFLICT, ex.getCode());
        }
        verify(deviceAttributeService, never()).updateAgentName(any(), any());
    }

    @Test
    @DisplayName("回读确认失败时拒绝")
    void rejectsFailedReadBack() {
        when(deviceDao.selectOne(any(QueryWrapper.class))).thenReturn(device(OLD_AGENT_ID));
        when(agentDao.selectById(OLD_AGENT_ID)).thenReturn(agent(OLD_AGENT_ID, "导游A"));
        when(agentDao.selectList(any(QueryWrapper.class))).thenReturn(List.of(agent(NEW_AGENT_ID, "导游B")));
        when(deviceDao.update(isNull(), any(UpdateWrapper.class))).thenReturn(1);
        when(deviceDao.selectById(MAC)).thenReturn(device(NEW_AGENT_ID));
        when(agentDao.selectById(NEW_AGENT_ID)).thenReturn(agent(NEW_AGENT_ID, "导游B"));
        DeviceAttributeEntity attribute = new DeviceAttributeEntity();
        attribute.setAgentName("旧名称");
        when(deviceAttributeService.getByDeviceId(MAC)).thenReturn(attribute);

        try (MockedStatic<MessageUtils> messageUtils = mockMessageUtils()) {
            RenException ex = assertThrows(RenException.class,
                    () -> service.rebindDevice(request("导游A", "导游B", true)));
            assertEquals(ErrorCode.DEVICE_REBIND_CONFIRM_FAILED, ex.getCode());
        }
    }

    @Test
    @DisplayName("目标查询限定在设备所属用户")
    void scopesTargetAgentLookupByDeviceUser() {
        when(deviceDao.selectOne(any(QueryWrapper.class))).thenReturn(device(OLD_AGENT_ID));
        when(agentDao.selectById(OLD_AGENT_ID)).thenReturn(agent(OLD_AGENT_ID, "导游A"));
        when(agentDao.selectList(any(QueryWrapper.class))).thenReturn(Collections.emptyList());

        try (MockedStatic<MessageUtils> messageUtils = mockMessageUtils()) {
            assertThrows(RenException.class, () -> service.rebindDevice(request("导游A", "导游B", true)));
        }

        verify(agentDao).selectList(any(QueryWrapper.class));
    }

    private MockedStatic<MessageUtils> mockMessageUtils() {
        MockedStatic<MessageUtils> messageUtils = mockStatic(MessageUtils.class);
        messageUtils.when(() -> MessageUtils.getMessage(anyInt())).thenReturn("error");
        messageUtils.when(() -> MessageUtils.getMessage(anyInt(), any())).thenReturn("error");
        return messageUtils;
    }

    private DeviceRebindDTO request(String current, String target, boolean confirm) {
        DeviceRebindDTO dto = new DeviceRebindDTO();
        dto.setDeviceId(MAC);
        dto.setCurrentAgentName(current);
        dto.setTargetAgentName(target);
        dto.setConfirm(confirm);
        return dto;
    }

    private DeviceEntity device(String agentId) {
        DeviceEntity entity = new DeviceEntity();
        entity.setId(MAC);
        entity.setMacAddress(MAC);
        entity.setUserId(USER_ID);
        entity.setAgentId(agentId);
        return entity;
    }

    private AgentEntity agent(String id, String name) {
        AgentEntity entity = new AgentEntity();
        entity.setId(id);
        entity.setAgentName(name);
        entity.setUserId(USER_ID);
        return entity;
    }
}
