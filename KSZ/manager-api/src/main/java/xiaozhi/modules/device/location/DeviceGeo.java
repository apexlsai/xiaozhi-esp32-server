package xiaozhi.modules.device.location;

public record DeviceGeo(String source, String city, CellGeo position, long locatedAt, long expiresAt,
        boolean cellReported, String status, long retryAt) {
}
