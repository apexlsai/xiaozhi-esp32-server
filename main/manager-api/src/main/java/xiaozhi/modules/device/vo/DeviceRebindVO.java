package xiaozhi.modules.device.vo;

import java.io.Serializable;

import io.swagger.v3.oas.annotations.media.Schema;
import lombok.Data;

@Data
@Schema(description = "设备智能体换绑结果")
public class DeviceRebindVO implements Serializable {

    @Schema(description = "设备ID（MAC地址）")
    private String deviceId;

    @Schema(description = "原智能体ID")
    private String previousAgentId;

    @Schema(description = "原智能体名称")
    private String previousAgentName;

    @Schema(description = "新智能体ID")
    private String agentId;

    @Schema(description = "新智能体名称")
    private String agentName;

    @Schema(description = "回读确认通过")
    private Boolean confirmed;

    @Schema(description = "设备需重连以加载新智能体配置")
    private Boolean reconnectRequired;
}
