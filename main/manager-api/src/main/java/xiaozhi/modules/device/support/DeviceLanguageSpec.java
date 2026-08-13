package xiaozhi.modules.device.support;

import java.util.LinkedHashMap;
import java.util.Map;

import org.apache.commons.lang3.StringUtils;

public final class DeviceLanguageSpec {
    private static final String TEST_SUFFIX = "-test";
    private static final Map<String, String> AGENT_SUFFIXES = createAgentSuffixes();

    private DeviceLanguageSpec() {
    }

    public static boolean isSupported(String language) {
        return resolveAgentSuffix(language) != null;
    }

    public static String resolveTargetAgentName(String currentAgentName, String language) {
        String targetSuffix = resolveAgentSuffix(language);
        if (StringUtils.isBlank(currentAgentName) || targetSuffix == null) {
            return null;
        }

        for (String separator : new String[] { "-", "－", "—" }) {
            for (String currentSuffix : AGENT_SUFFIXES.values()) {
                String testMarker = separator + currentSuffix + "-测试";
                if (currentAgentName.endsWith(testMarker)) {
                    String prefix = currentAgentName.substring(0, currentAgentName.length() - testMarker.length());
                    return StringUtils.isBlank(prefix) ? null : prefix + separator + targetSuffix;
                }

                String marker = separator + currentSuffix;
                if (currentAgentName.endsWith(marker)) {
                    String prefix = currentAgentName.substring(0, currentAgentName.length() - marker.length());
                    return StringUtils.isBlank(prefix) ? null : prefix + separator + targetSuffix;
                }
            }
        }
        return null;
    }

    private static String resolveAgentSuffix(String language) {
        if (StringUtils.isBlank(language)) {
            return null;
        }
        boolean test = language.endsWith(TEST_SUFFIX);
        String baseLanguage = test ? language.substring(0, language.length() - TEST_SUFFIX.length()) : language;
        String suffix = AGENT_SUFFIXES.get(baseLanguage);
        return suffix == null ? null : suffix + (test ? "-测试" : "");
    }

    private static Map<String, String> createAgentSuffixes() {
        Map<String, String> suffixes = new LinkedHashMap<>();
        suffixes.put("zh-CN", "汉语");
        suffixes.put("en", "英语");
        suffixes.put("ja", "日语");
        suffixes.put("ko", "韩语");
        suffixes.put("zh-CN-yue", "粤语");
        suffixes.put("zh-CN-sichuan", "四川话");
        suffixes.put("zh-CN-shanghai", "上海话");
        suffixes.put("zh-CN-minnan", "闽南语");
        suffixes.put("zh-CN-shanxi", "陕西话");
        return Map.copyOf(suffixes);
    }
}
