package xiaozhi.modules.device.dto;

import java.io.Serializable;

import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.AssertTrue;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import lombok.Data;

@Data
@Schema(description = "设备智能体换绑请求")
public class DeviceRebindDTO implements Serializable {

    @Schema(description = "设备ID（MAC地址）")
    @NotBlank(message = "设备ID不能为空")
    private String deviceId;

    @Schema(description = "当前绑定智能体名称")
    @NotBlank(message = "当前智能体名称不能为空")
    private String currentAgentName;

    @Schema(description = "目标智能体名称")
    @NotBlank(message = "目标智能体名称不能为空")
    private String targetAgentName;

    @Schema(description = "确认执行换绑，必须为 true")
    @NotNull(message = "confirm不能为空")
    @AssertTrue(message = "必须确认换绑")
    private Boolean confirm;

    @Schema(description = "语言切换时同步保存的规范语言码")
    private String language;

    @Schema(description = "语言切换时是否选择测试智能体")
    private Boolean dev;
}
