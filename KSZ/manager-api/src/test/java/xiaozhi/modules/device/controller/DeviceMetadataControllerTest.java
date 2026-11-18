package xiaozhi.modules.device.controller;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.Collections;
import java.util.List;

import org.apache.ibatis.builder.MapperBuilderAssistant;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import com.baomidou.mybatisplus.core.MybatisConfiguration;
import com.baomidou.mybatisplus.core.metadata.TableInfoHelper;
import com.fasterxml.jackson.databind.ObjectMapper;

import xiaozhi.modules.device.dao.DeviceDao;
import xiaozhi.modules.device.entity.DeviceEntity;

class DeviceMetadataControllerTest {
    private final DeviceDao devices = mock(DeviceDao.class);
    private final ObjectMapper mapper = new ObjectMapper();
    private MockMvc mvc;

    @BeforeEach
    void setUp() {
        TableInfoHelper.initTableInfo(new MapperBuilderAssistant(new MybatisConfiguration(), ""),
                DeviceEntity.class);
        mvc = MockMvcBuilders.standaloneSetup(new DeviceMetadataController(devices)).build();
    }

    @Test
    void returnsOnlyOtaMetadataWithNormalizedMac() throws Exception {
        DeviceEntity device = new DeviceEntity();
        device.setMacAddress("aa:bb:cc:dd:ee:ff");
        device.setBoard("ksz-traveler");
        device.setAppVersion("1.3.2");
        device.setUserId(7L);
        device.setAgentId("private-agent");
        when(devices.selectList(any())).thenReturn(List.of(device));
        mvc.perform(post("/config/device-metadata").contentType(MediaType.APPLICATION_JSON)
                .content("{\"deviceIds\":[\"AA:BB:CC:DD:EE:FF\"]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data[0].device_mac").value("AA:BB:CC:DD:EE:FF"))
                .andExpect(jsonPath("$.data[0].model").value("ksz-traveler"))
                .andExpect(jsonPath("$.data[0].firmware_version").value("1.3.2"))
                .andExpect(jsonPath("$.data[0].userId").doesNotExist())
                .andExpect(jsonPath("$.data[0].agentId").doesNotExist());
    }

    @Test
    void rejectsInvalidOrMissingMacBeforeQuerying() throws Exception {
        for (String body : List.of("{}", "{\"deviceIds\":[]}", "{\"deviceIds\":[null]}",
                "{\"deviceIds\":[\"not-a-mac\"]}")) {
            mvc.perform(post("/config/device-metadata").contentType(MediaType.APPLICATION_JSON)
                    .content(body)).andExpect(status().isBadRequest());
        }
        verifyNoInteractions(devices);
    }

    @Test
    void enforcesHundredDeviceBatchLimit() throws Exception {
        String body = mapper.writeValueAsString(java.util.Map.of(
                "deviceIds", Collections.nCopies(101, "AA:BB:CC:DD:EE:FF")));
        mvc.perform(post("/config/device-metadata").contentType(MediaType.APPLICATION_JSON)
                .content(body)).andExpect(status().isBadRequest());
        verifyNoInteractions(devices);
    }

    @Test
    void unknownDevicesReturnEmptyList() throws Exception {
        when(devices.selectList(any())).thenReturn(List.of());
        mvc.perform(post("/config/device-metadata").contentType(MediaType.APPLICATION_JSON)
                .content("{\"deviceIds\":[\"AA:BB:CC:DD:EE:FF\"]}"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.data").isEmpty());
    }
}
