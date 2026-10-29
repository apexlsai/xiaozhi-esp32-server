package xiaozhi.modules.device.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

import org.apache.shiro.subject.Subject;
import org.apache.shiro.util.ThreadContext;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.http.converter.StringHttpMessageConverter;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;

import xiaozhi.common.redis.RedisUtils;
import xiaozhi.common.user.UserDetail;
import xiaozhi.modules.device.dto.DeviceCellReport;
import xiaozhi.modules.device.dto.DeviceReportRespDTO;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceCellService;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.device.location.CellLocationStore;
import xiaozhi.modules.sys.service.SysParamsService;

class DeviceCellReportTest {
    private static final String MAC = "80:f1:b2:d4:ce:64";
    private static final String CACHE_KEY = "ksz:device:cell:" + MAC;
    private static final String REPORT = """
            {
              "application":{"version":"1.3.2"},
              "board":{
                "type":"ksz-traveler-esp32p4",
                "name":"ksz-traveler-esp32p4-no-lang-role",
                "manufacturer":"","revision":"ML307R-DL-MBRH0S01",
                "carrier":"","csq":"16","imei":"863913000000000",
                "cereg":{"stat":1,"tac":"27AE","ci":"0540278E","AcT":7},
                "cell":{"valid":1,"stat":1,"op":1,"mcc":"460","mnc":"00",
                        "act":7,"lac":10158,"cellid":88090510}
              }
            }
            """;

    private final RedisUtils redis = mock(RedisUtils.class);
    private final DeviceService devices = mock(DeviceService.class);
    private final DeviceCellService cells = new DeviceCellService(redis, mock(CellLocationStore.class));
    private final Map<String, Object> cache = new HashMap<>();
    private MockMvc mvc;

    @BeforeEach
    void setUp() {
        doAnswer(invocation -> {
            cache.put(invocation.getArgument(0), invocation.getArgument(1));
            return null;
        }).when(redis).set(anyString(), any(), anyLong());
        when(redis.get(anyString())).thenAnswer(invocation -> cache.get(invocation.getArgument(0)));
        when(devices.checkDeviceActive(anyString(), anyString(), any())).thenReturn(new DeviceReportRespDTO());
        ObjectMapper mapper = new ObjectMapper().disable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES);
        mvc = MockMvcBuilders.standaloneSetup(
                new OTAController(devices, mock(SysParamsService.class), cells),
                new DeviceCellController(devices, cells))
                .setMessageConverters(new StringHttpMessageConverter(StandardCharsets.UTF_8),
                        new MappingJackson2HttpMessageConverter(mapper))
                .build();
    }

    @AfterEach
    void tearDown() {
        ThreadContext.unbindSubject();
    }

    @Test
    void otaRetainsCellAndLeadingZeroNetworkCode() throws Exception {
        long before = System.currentTimeMillis();
        report(MAC.toUpperCase(), REPORT);

        DeviceCellReport result = cells.get(MAC);
        assertEquals(MAC, result.deviceId());
        assertEquals("ksz-traveler-esp32p4", result.boardType());
        assertEquals("460", result.cell().getMcc());
        assertEquals("00", result.cell().getMnc());
        assertEquals(88090510L, result.cell().getCellid());
        assertEquals(10158L, result.cell().getLac());
        assertEquals(1, result.cell().getOp());
        assertEquals(7, result.cell().getAct());
        assertTrue(result.reportedAt() >= before && result.reportedAt() <= System.currentTimeMillis());
        verify(redis).set(anyString(), any(), org.mockito.ArgumentMatchers.eq(86400L));
        assertTrue(cache.containsKey(CACHE_KEY));
    }

    @Test
    void retainsHexFieldsAndLargeCellId() throws Exception {
        report(MAC, REPORT.replace("\"cellid\":88090510",
                "\"cellid\":4294967295,\"tac\":\"27AE\",\"ci\":\"FFFFFFFF\",\"future_field\":true"));

        DeviceCellReport result = cells.get(MAC);
        assertEquals(4294967295L, result.cell().getCellid());
        assertEquals("27AE", result.cell().getTac());
        assertEquals("FFFFFFFF", result.cell().getCi());
    }

    @Test
    void missingCellDoesNotEraseOrRefreshLastReport() throws Exception {
        report(MAC, "{\"application\":{\"version\":\"1.3.2\"}}");
        assertNull(cells.get(MAC));
        report(MAC, REPORT);
        Object previous = cache.get(CACHE_KEY);
        report(MAC, "{\"board\":{\"type\":\"wifi\",\"cell\":null}}");

        assertEquals(previous, cache.get(CACHE_KEY));
        verify(redis, times(1)).set(anyString(), any(), anyLong());
    }

    @Test
    void invalidCellReplacesPreviousValidState() throws Exception {
        report(MAC, REPORT);
        report(MAC, REPORT.replace("\"valid\":1", "\"valid\":0"));

        assertEquals(0, cells.get(MAC).cell().getValid());
    }

    @Test
    void redisFailureDoesNotInterruptOta() throws Exception {
        doThrow(new IllegalStateException("redis unavailable")).when(redis).set(anyString(), any(), anyLong());

        report(MAC, REPORT);

        verify(devices).checkDeviceActive(anyString(), anyString(), any());
    }

    @Test
    void rejectedOtaDoesNotCacheCell() throws Exception {
        report("invalid-mac", REPORT);
        report(MAC, REPORT.replace("\"application\":", "\"deviceId\":\"aa:bb:cc:dd:ee:ff\","
                + "\"event\":\"language_change\",\"payload\":{\"language\":\"zh-CN\"},\"application\":"));

        verify(redis, never()).set(anyString(), any(), anyLong());
        verify(devices, never()).checkDeviceActive(anyString(), anyString(), any());
    }

    @Test
    void onlyOwnerCanReadReportedCell() throws Exception {
        report(MAC, REPORT);
        DeviceEntity device = new DeviceEntity();
        device.setUserId(7L);
        when(devices.getDeviceByMacAddress(MAC)).thenReturn(device);
        signIn(7L);
        mvc.perform(get("/device/{deviceId}/cell", MAC.toUpperCase()))
                .andExpect(status().isOk()).andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.cell.mnc").value("00"))
                .andExpect(jsonPath("$.data.cell.cellid").value(88090510));

        signIn(8L);
        mvc.perform(get("/device/{deviceId}/cell", MAC)).andExpect(jsonPath("$.msg").value("设备不存在"));
        ThreadContext.unbindSubject();
        mvc.perform(get("/device/{deviceId}/cell", MAC)).andExpect(jsonPath("$.msg").value("设备不存在"));
        signIn(7L);
        when(devices.getDeviceByMacAddress(MAC)).thenReturn(null);
        mvc.perform(get("/device/{deviceId}/cell", MAC)).andExpect(jsonPath("$.msg").value("设备不存在"));
        verify(redis, times(1)).get(CACHE_KEY);
    }

    @Test
    void missingOrExpiredReportReturnsNoData() throws Exception {
        DeviceEntity device = new DeviceEntity();
        device.setUserId(7L);
        when(devices.getDeviceByMacAddress(MAC)).thenReturn(device);
        signIn(7L);

        mvc.perform(get("/device/{deviceId}/cell", MAC))
                .andExpect(jsonPath("$.code").value(0)).andExpect(jsonPath("$.data").doesNotExist());
    }

    private void report(String deviceId, String body) throws Exception {
        mvc.perform(post("/ota/").header("Device-Id", deviceId)
                .contentType(MediaType.APPLICATION_JSON).content(body)).andExpect(status().isOk());
    }

    private void signIn(Long userId) {
        UserDetail user = new UserDetail();
        user.setId(userId);
        Subject subject = mock(Subject.class);
        when(subject.getPrincipal()).thenReturn(user);
        ThreadContext.bind(subject);
    }
}
