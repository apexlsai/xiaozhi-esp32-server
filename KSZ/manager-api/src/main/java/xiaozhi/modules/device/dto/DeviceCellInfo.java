package xiaozhi.modules.device.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonInclude;

import lombok.Data;

@Data
@JsonIgnoreProperties(ignoreUnknown = true)
@JsonInclude(JsonInclude.Include.NON_NULL)
public class DeviceCellInfo {
    private Integer valid;
    private Integer stat;
    private Integer op;
    private String mcc;
    private String mnc;
    private Integer act;
    private Long lac;
    private Long cellid;
    private String tac;
    private String ci;
}
