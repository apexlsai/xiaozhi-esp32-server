package xiaozhi.modules.device.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.Map;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.http.ResponseEntity;

import xiaozhi.modules.device.dto.DeviceReportReqDTO;
import xiaozhi.modules.device.dto.DeviceReportRespDTO;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.sys.service.SysParamsService;

@DisplayName("OTA 连接前事件测试")
class OTAControllerTest {

    private static final String DEVICE_ID = "aa:bb:cc:dd:ee:ff";

    @Test
    @DisplayName("连接前语言事件先换绑再返回连接配置")
    void languageChangeRebindsBeforeOtaResponse() {
        DeviceService deviceService = mock(DeviceService.class);
        DeviceReportReqDTO request = request();
        request.setDeviceId(DEVICE_ID);
        request.setEvent("language_change");
        request.setPayload(Map.of("language", "zh-CN", "dev", true));
        DeviceReportRespDTO otaResponse = new DeviceReportRespDTO();
        when(deviceService.checkDeviceActive(DEVICE_ID, "client-id", request)).thenReturn(otaResponse);

        ResponseEntity<String> response = controller(deviceService)
                .checkOTAVersion(request, DEVICE_ID, "client-id");

        assertEquals(200, response.getStatusCode().value());
        verify(deviceService).rebindDeviceLanguage(DEVICE_ID, "zh-CN", true);
        verify(deviceService).checkDeviceActive(DEVICE_ID, "client-id", request);
    }

    @Test
    @DisplayName("普通 OTA 请求保持原链路")
    void ordinaryOtaDoesNotRebind() {
        DeviceService deviceService = mock(DeviceService.class);
        DeviceReportReqDTO request = request();
        when(deviceService.checkDeviceActive(DEVICE_ID, "client-id", request))
                .thenReturn(new DeviceReportRespDTO());

        controller(deviceService).checkOTAVersion(request, DEVICE_ID, "client-id");

        verify(deviceService, never()).rebindDeviceLanguage(DEVICE_ID, "zh-CN", false);
        verify(deviceService).checkDeviceActive(DEVICE_ID, "client-id", request);
    }

    @Test
    @DisplayName("请求体设备 ID 与请求头不一致时拒绝换绑")
    void mismatchedDeviceIdIsRejected() {
        DeviceService deviceService = mock(DeviceService.class);
        DeviceReportReqDTO request = request();
        request.setDeviceId("11:22:33:44:55:66");
        request.setEvent("language_change");
        request.setPayload(Map.of("language", "zh-CN"));

        ResponseEntity<String> response = controller(deviceService)
                .checkOTAVersion(request, DEVICE_ID, "client-id");

        assertEquals(true, response.getBody().contains("deviceId"));
        verify(deviceService, never()).rebindDeviceLanguage(DEVICE_ID, "zh-CN", false);
        verify(deviceService, never()).checkDeviceActive(DEVICE_ID, "client-id", request);
    }

    private OTAController controller(DeviceService deviceService) {
        return new OTAController(deviceService, mock(SysParamsService.class));
    }

    private DeviceReportReqDTO request() {
        DeviceReportReqDTO request = new DeviceReportReqDTO();
        DeviceReportReqDTO.Application application = new DeviceReportReqDTO.Application();
        application.setVersion("1.0.0");
        request.setApplication(application);
        return request;
    }
}
