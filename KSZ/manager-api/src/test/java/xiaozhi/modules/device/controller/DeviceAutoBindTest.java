package xiaozhi.modules.device.controller;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.apache.ibatis.mapping.Environment;
import org.mybatis.spring.SqlSessionTemplate;
import org.mybatis.spring.transaction.SpringManagedTransactionFactory;
import org.mockito.AdditionalAnswers;
import org.springframework.aop.framework.AopContext;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.test.util.ReflectionTestUtils;

import com.baomidou.mybatisplus.core.MybatisConfiguration;
import com.baomidou.mybatisplus.core.MybatisSqlSessionFactoryBuilder;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import xiaozhi.common.redis.RedisKeys;
import xiaozhi.common.redis.RedisUtils;
import xiaozhi.modules.agent.dao.AgentDao;
import xiaozhi.modules.device.dao.DeviceDao;
import xiaozhi.modules.device.dto.DeviceReportReqDTO;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceAddressBookService;
import xiaozhi.modules.device.service.DeviceAttributeService;
import xiaozhi.modules.device.service.DeviceAutoBindService;
import xiaozhi.modules.device.service.DeviceCellService;
import xiaozhi.modules.device.service.OtaService;
import xiaozhi.modules.device.service.impl.DeviceServiceImpl;
import xiaozhi.modules.sys.service.SysParamsService;
import xiaozhi.modules.sys.service.SysUserUtilService;

class DeviceAutoBindTest {
    private static final String MAC = "aa:bb:cc:dd:ee:ff";
    private static final String TEMP = "儿童-韩语-测试";
    private JdbcTemplate jdbc;
    private DeviceDao devices;
    private AgentDao agents;
    private DeviceAttributeService attributes;
    private RedisUtils redis;
    private DataSourceTransactionManager transactions;
    private DeviceServiceImpl service;
    private DeviceAutoBindService autoBind;
    private SysParamsService params;

    @BeforeEach
    void setup() throws Exception {
        var source = new DriverManagerDataSource(
                "jdbc:h2:mem:" + UUID.randomUUID() + ";MODE=MySQL;IGNORECASE=TRUE;DB_CLOSE_DELAY=-1", "sa", "");
        jdbc = new JdbcTemplate(source);
        jdbc.execute("CREATE TABLE ai_agent (id VARCHAR(32) PRIMARY KEY, user_id BIGINT, agent_name VARCHAR(64))");
        jdbc.execute("""
                CREATE TABLE ai_device (
                    id VARCHAR(32) PRIMARY KEY, user_id BIGINT, mac_address VARCHAR(50),
                    last_connected_at TIMESTAMP, auto_update INT, board VARCHAR(50), alias VARCHAR(64),
                    agent_id VARCHAR(32), app_version VARCHAR(20), sort INT DEFAULT 0,
                    creator BIGINT, create_date TIMESTAMP, updater BIGINT, update_date TIMESTAMP)
                """);
        jdbc.execute("CREATE TABLE ai_device_attribute (device_id VARCHAR(50) PRIMARY KEY, agent_name VARCHAR(64))");
        jdbc.update("INSERT INTO ai_agent VALUES ('temporary', 7, ?)", TEMP);
        var configuration = new MybatisConfiguration();
        configuration.setMapUnderscoreToCamelCase(true);
        configuration.setEnvironment(new Environment("test", new SpringManagedTransactionFactory(), source));
        configuration.addMapper(DeviceDao.class);
        configuration.addMapper(AgentDao.class);
        var session = new SqlSessionTemplate(new MybatisSqlSessionFactoryBuilder().build(configuration));
        devices = session.getMapper(DeviceDao.class);
        agents = session.getMapper(AgentDao.class);
        attributes = mock(DeviceAttributeService.class);
        doAnswer(invocation -> {
            String mac = invocation.getArgument(0);
            String name = invocation.getArgument(1);
            jdbc.update("MERGE INTO ai_device_attribute KEY(device_id) VALUES (?, ?)",
                    mac, name);
            return null;
        }).when(attributes).updateAgentName(anyString(), anyString());
        redis = mock(RedisUtils.class);
        transactions = new DataSourceTransactionManager(source);
        autoBind = binder(devices, TEMP);
        params = mock(SysParamsService.class);
        when(params.getValue("server.websocket", true)).thenReturn("ws://example.test/xiaozhi/v1/");
        service = spy(new DeviceServiceImpl(devices, mock(SysUserUtilService.class), params,
                redis, mock(OtaService.class), mock(DeviceAddressBookService.class), attributes, agents));
        ReflectionTestUtils.setField(service, "baseDao", devices);
        doNothing().when(service).updateDeviceConnectionInfo(any(), any(), any());
    }

    @Test
    void firstOtaRegistersUnderTemporaryAgentWithoutActivationOrFirmwareUpgrade() throws Exception {
        JsonNode response = ota(request(), MAC.toUpperCase());

        assertEquals("ws://example.test/xiaozhi/v1/", response.at("/websocket/url").asText());
        assertFalse(response.has("activation"));
        assertFalse(response.has("firmware"));
        assertFalse(response.has("error"));
        Map<String, Object> device = jdbc.queryForMap("SELECT * FROM ai_device");
        assertEquals(MAC, device.get("ID"));
        assertEquals(MAC, device.get("MAC_ADDRESS"));
        assertEquals("temporary", device.get("AGENT_ID"));
        assertEquals(7L, device.get("USER_ID"));
        assertEquals(7L, device.get("CREATOR"));
        assertEquals("1.0.0", device.get("APP_VERSION"));
        assertEquals("ksz-traveler", device.get("BOARD"));
        assertEquals(0, device.get("AUTO_UPDATE"));
        assertEquals(TEMP, jdbc.queryForObject("SELECT agent_name FROM ai_device_attribute", String.class));
        assertEquals(200, controller().activateDevice(MAC, null).getStatusCode().value());
        verify(redis).delete(RedisKeys.getAgentDeviceCountById("temporary"));
        verify(service, never()).buildActivation(any(), any());
    }

    @Test
    void reconnectPreservesManualFinalBindingAndAttributesEvenIfTemporaryAgentIsGone() throws Exception {
        ota(request(), MAC);
        jdbc.update("UPDATE ai_device SET agent_id='final', alias='正式设备', auto_update=1 WHERE id=?", MAC);
        jdbc.update("UPDATE ai_device_attribute SET agent_name='儿童-汉语' WHERE device_id=?", MAC);
        jdbc.update("DELETE FROM ai_agent");
        clearInvocations(attributes, redis);
        when(params.getValue("server.ota", true)).thenReturn(null);

        JsonNode response = ota(request(), MAC);

        assertFalse(response.has("activation"));
        assertEquals("final", jdbc.queryForObject("SELECT agent_id FROM ai_device", String.class));
        assertEquals("正式设备", jdbc.queryForObject("SELECT alias FROM ai_device", String.class));
        assertEquals(1, jdbc.queryForObject("SELECT auto_update FROM ai_device", Integer.class));
        assertEquals("儿童-汉语", jdbc.queryForObject("SELECT agent_name FROM ai_device_attribute", String.class));
        assertEquals(1, countDevices());
        verifyNoInteractions(attributes, redis);
    }

    @Test
    void disabledAutoBindingRetainsOriginalActivationFlow() throws Exception {
        autoBind = binder(devices, "");
        when(params.getValue("server.fronted_url", true)).thenReturn("http://example.test");

        JsonNode response = ota(request(), MAC);

        assertTrue(response.has("activation"));
        assertEquals(0, countDevices());
        verify(service).buildActivation(eq(MAC), any());
    }

    @Test
    void missingTemporaryAgentReturnsErrorWithoutConnectionOrRegistration() throws Exception {
        jdbc.update("DELETE FROM ai_agent");

        JsonNode response = ota(request(), MAC);

        assertTrue(response.has("error"));
        assertFalse(response.has("websocket"));
        assertEquals(0, countDevices());
        verify(service, never()).checkDeviceActive(any(), any(), any());
    }

    @Test
    void duplicateTemporaryNamesAcrossOwnersAreRejected() throws Exception {
        jdbc.update("INSERT INTO ai_agent VALUES ('other-owner', 8, ?)", TEMP);

        assertTrue(ota(request(), MAC).has("error"));
        assertEquals(0, countDevices());
    }

    @Test
    void temporaryAgentWithoutOwnerIsRejected() throws Exception {
        jdbc.update("UPDATE ai_agent SET user_id=NULL");

        assertTrue(ota(request(), MAC).has("error"));
        assertEquals(0, countDevices());
    }

    @Test
    void incompleteFirstReportDoesNotCreateDevice() throws Exception {
        DeviceReportReqDTO request = request();
        request.setApplication(null);

        assertTrue(ota(request, MAC).has("error"));
        assertEquals(0, countDevices());
    }

    @Test
    void invalidMacDoesNotRegisterEvenWhenLegacyOtaValidationAcceptsLetters() throws Exception {
        assertTrue(ota(request(), "zz:bb:cc:dd:ee:ff").has("error"));
        assertEquals(0, countDevices());
    }

    @Test
    void mismatchedBodyIdentityDoesNotCreateDevice() throws Exception {
        DeviceReportReqDTO request = request();
        request.setDeviceId("11:22:33:44:55:66");

        assertTrue(ota(request, MAC).has("error"));
        assertEquals(0, countDevices());
    }

    @Test
    void invalidPreConnectEventDoesNotCreateDevice() throws Exception {
        DeviceReportReqDTO request = request();
        request.setEvent("profile_change");
        request.setPayload(Map.of("language", "ko", "role", "unknown"));

        assertTrue(ota(request, MAC).has("error"));
        assertEquals(0, countDevices());
        verifyNoInteractions(attributes);
    }

    @Test
    void firstProfileSelectionRunsAfterRegistrationAndBeforeReturningConnection() throws Exception {
        DeviceReportReqDTO request = request();
        request.setEvent("profile_change");
        request.setPayload(Map.of("language", "zh-CN", "role", "child", "dev", false));
        doAnswer(invocation -> {
            assertEquals("temporary", jdbc.queryForObject("SELECT agent_id FROM ai_device", String.class));
            assertEquals(TEMP, jdbc.queryForObject("SELECT agent_name FROM ai_device_attribute", String.class));
            jdbc.update("UPDATE ai_device SET agent_id='final' WHERE id=?", MAC);
            return null;
        }).when(service).rebindDeviceProfile(MAC, "zh-CN", "child", false);

        assertFalse(ota(request, MAC).has("activation"));
        assertEquals("final", jdbc.queryForObject("SELECT agent_id FROM ai_device", String.class));
        var order = inOrder(service);
        order.verify(service).rebindDeviceProfile(MAC, "zh-CN", "child", false);
        order.verify(service).checkDeviceActive(MAC, "client-id", request);
    }

    @Test
    void firstLanguageSelectionRunsAfterRegistration() throws Exception {
        DeviceReportReqDTO request = request();
        request.setEvent("language_change");
        request.setPayload(Map.of("language", "en", "dev", true));
        doAnswer(invocation -> {
            assertEquals(1, countDevices());
            return null;
        }).when(service).rebindDeviceLanguage(MAC, "en", true);

        assertFalse(ota(request, MAC).has("activation"));
        verify(service).rebindDeviceLanguage(MAC, "en", true);
    }

    @Test
    void attributeFailureRollsBackRegistration() {
        doThrow(new IllegalStateException("attribute write failed"))
                .when(attributes).updateAgentName(anyString(), anyString());

        assertThrows(IllegalStateException.class, () -> autoBind.bindIfAbsent(MAC, request()));
        assertEquals(0, countDevices());
        verifyNoInteractions(redis);
    }

    @Test
    void concurrentFirstReportsCreateOneDeviceAndOneAttribute() throws Exception {
        var barrier = new CyclicBarrier(2);
        DeviceDao racing = mock(DeviceDao.class, AdditionalAnswers.delegatesTo(devices));
        doAnswer(invocation -> {
            barrier.await(5, TimeUnit.SECONDS);
            return devices.insert(invocation.<DeviceEntity>getArgument(0));
        }).when(racing).insert(any(DeviceEntity.class));
        var binder = binder(racing, TEMP);
        try (var pool = Executors.newFixedThreadPool(2)) {
            var first = pool.submit(() -> binder.bindIfAbsent(MAC, request()));
            var second = pool.submit(() -> binder.bindIfAbsent(MAC.toUpperCase(), request()));
            first.get(10, TimeUnit.SECONDS);
            second.get(10, TimeUnit.SECONDS);
        }

        assertEquals(1, countDevices());
        assertEquals(1, jdbc.queryForObject("SELECT COUNT(*) FROM ai_device_attribute", Integer.class));
        verify(redis, times(1)).delete(RedisKeys.getAgentDeviceCountById("temporary"));
    }

    @Test
    void concurrentManualRegistrationWinsWithoutBeingOverwritten() {
        DeviceDao racing = mock(DeviceDao.class, AdditionalAnswers.delegatesTo(devices));
        doAnswer(invocation -> {
            CompletableFuture.runAsync(() -> jdbc.update(
                    "INSERT INTO ai_device(id,mac_address,user_id,agent_id) VALUES (?,?,8,'manual-final')", MAC, MAC))
                    .get(5, TimeUnit.SECONDS);
            return devices.insert(invocation.<DeviceEntity>getArgument(0));
        }).when(racing).insert(any(DeviceEntity.class));

        binder(racing, TEMP).bindIfAbsent(MAC, request());

        assertEquals("manual-final", jdbc.queryForObject("SELECT agent_id FROM ai_device", String.class));
        assertEquals(8L, jdbc.queryForObject("SELECT user_id FROM ai_device", Long.class));
        verifyNoInteractions(attributes, redis);
    }

    private DeviceAutoBindService binder(DeviceDao deviceDao, String name) {
        return new DeviceAutoBindService(deviceDao, agents, attributes, redis, transactions, name);
    }

    private OTAController controller() {
        return new OTAController(service, params, mock(DeviceCellService.class), autoBind);
    }

    private JsonNode ota(DeviceReportReqDTO request, String mac) throws Exception {
        try (var proxy = mockStatic(AopContext.class)) {
            proxy.when(AopContext::currentProxy).thenReturn(service);
            return new ObjectMapper().readTree(controller().checkOTAVersion(request, mac, "client-id").getBody());
        }
    }

    private int countDevices() {
        return jdbc.queryForObject("SELECT COUNT(*) FROM ai_device", Integer.class);
    }

    private DeviceReportReqDTO request() {
        DeviceReportReqDTO request = new DeviceReportReqDTO();
        DeviceReportReqDTO.Application application = new DeviceReportReqDTO.Application();
        application.setVersion("1.0.0");
        request.setApplication(application);
        DeviceReportReqDTO.BoardInfo board = new DeviceReportReqDTO.BoardInfo();
        board.setType("ksz-traveler");
        request.setBoard(board);
        return request;
    }
}
