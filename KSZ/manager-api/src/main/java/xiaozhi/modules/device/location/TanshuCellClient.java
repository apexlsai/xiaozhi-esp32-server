package xiaozhi.modules.device.location;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.springframework.stereotype.Component;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

@Component
public class TanshuCellClient {
    private final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    public CellGeo lookup(CellKey key, String apiKey) throws IOException, InterruptedException {
        if (!apiKey.matches("[a-zA-Z0-9_-]+")) {
            throw new IOException("invalid_key_configuration");
        }
        URI uri = URI.create("https://api.tanshuapi.com/api/cell/v1/index?mnc=" + key.providerMnc()
                + "&lac=" + key.lac() + "&cellid=" + key.cellid() + "&key=" + apiKey);
        HttpRequest request = HttpRequest.newBuilder(uri).timeout(Duration.ofSeconds(6))
                .header("Accept", "application/json").GET().build();
        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            throw new IOException("http_" + response.statusCode());
        }
        return parse(response.body());
    }

    static CellGeo parse(String body) throws IOException {
        JsonNode root = new ObjectMapper().readTree(body);
        if (root.path("code").asInt() != 1) {
            throw new IOException("provider_" + root.path("code").asInt());
        }
        JsonNode data = root.path("data");
        double lat = data.path("lat").asDouble(Double.NaN);
        double lng = data.path("lng").asDouble(Double.NaN);
        double radius = data.path("radius").asDouble(Double.NaN);
        String address = data.path("addr").asText("").strip();
        String local = address.replaceFirst("^.*?(?:省|自治区)", "");
        Matcher matcher = Pattern.compile("^(.+?(?:市|自治州|地区|盟))").matcher(local);
        if (!Double.isFinite(lat) || lat < -90 || lat > 90 || !Double.isFinite(lng)
                || lng < -180 || lng > 180 || !Double.isFinite(radius) || radius <= 0
                || address.length() > 1000 || !matcher.find()) {
            throw new IOException("invalid_position");
        }
        return new CellGeo(matcher.group(1), address, lat, lng, radius);
    }
}
