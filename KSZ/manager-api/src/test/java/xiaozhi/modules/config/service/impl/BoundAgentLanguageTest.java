package xiaozhi.modules.config.service.impl;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.when;

import java.util.Map;
import java.util.stream.Stream;

import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.junit.jupiter.api.extension.ExtendWith;

import xiaozhi.common.redis.RedisUtils;
import xiaozhi.modules.agent.vo.AgentInfoVO;
import xiaozhi.modules.agent.service.AgentContextProviderService;
import xiaozhi.modules.agent.service.AgentMcpAccessPointService;
import xiaozhi.modules.agent.service.AgentPluginMappingService;
import xiaozhi.modules.agent.service.AgentService;
import xiaozhi.modules.agent.service.AgentTemplateService;
import xiaozhi.modules.agent.dao.AgentVoicePrintDao;
import xiaozhi.modules.correctword.service.CorrectWordFileService;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceAttributeService;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.model.service.ModelConfigService;
import xiaozhi.modules.sys.service.SysParamsService;
import xiaozhi.modules.timbre.service.TimbreService;
import xiaozhi.modules.voiceclone.service.VoiceCloneService;

@ExtendWith(MockitoExtension.class)
class BoundAgentLanguageTest {
    @Mock SysParamsService parameters;
    @Mock DeviceService devices;
    @Mock DeviceAttributeService attributes;
    @Mock ModelConfigService models;
    @Mock AgentService agents;
    @Mock AgentTemplateService templates;
    @Mock RedisUtils redis;
    @Mock TimbreService timbres;
    @Mock AgentPluginMappingService plugins;
    @Mock AgentMcpAccessPointService mcp;
    @Mock AgentContextProviderService context;
    @Mock VoiceCloneService voices;
    @Mock AgentVoicePrintDao voiceprints;
    @Mock CorrectWordFileService corrections;
    @InjectMocks ConfigServiceImpl service;

    static Stream<Map<String, String>> deviceAttributes() {
        return Stream.of(
                Map.of(),
                Map.of("language", "zh-CN"),
                Map.of("agent_name", "青年-汉语", "language", "zh-CN", "last_beacon_id", "gallery-1")
        );
    }

    @ParameterizedTest
    @MethodSource("deviceAttributes")
    void privateConfigUsesTheBoundAgentWithMissingOrStaleAttributes(Map<String, String> stored) {
        DeviceEntity device = new DeviceEntity();
        device.setMacAddress("02:00:00:00:00:01");
        device.setAgentId("korean-agent");
        AgentInfoVO agent = new AgentInfoVO();
        agent.setId("korean-agent");
        agent.setAgentName("青年-韩语");
        agent.setIntentModelId("Intent_nointent");
        when(devices.getDeviceByMacAddress(device.getMacAddress())).thenReturn(device);
        when(agents.getAgentById(agent.getId())).thenReturn(agent);
        when(attributes.getAttributesByDeviceId(device.getMacAddress())).thenReturn(stored);

        Map<?, ?> result = (Map<?, ?>) service.getAgentModels(device.getMacAddress(), Map.of())
                .get("device_attributes");

        assertEquals("青年-韩语", result.get("agent_name"));
        stored.forEach((key, value) -> {
            if (!key.equals("agent_name")) {
                assertEquals(value, result.get(key));
            }
        });
    }
}
