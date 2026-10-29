package xiaozhi.modules.device.dto;

public record DeviceCellReport(String deviceId, String boardType, long reportedAt, DeviceCellInfo cell) {
}
