package xiaozhi.modules.device.support;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

import org.apache.commons.lang3.StringUtils;

public final class DeviceLanguageSpec {
    private static final Map<String, String> AGENT_SUFFIXES = createAgentSuffixes();
    private static final Map<String, String> ROLE_PREFIXES = Map.of(
            "child", "儿童",
            "youth", "青年",
            "expert", "专家");
    private static final Set<String> PROFILE_LANGUAGES = Set.of("zh-CN", "en", "ja", "ko");

    private DeviceLanguageSpec() {
    }

    public static boolean isSupported(String language) {
        return AGENT_SUFFIXES.containsKey(language);
    }

    public static boolean isProfileLanguageSupported(String language) {
        return language != null && PROFILE_LANGUAGES.contains(language);
    }

    public static boolean isRoleSupported(String role) {
        return role != null && ROLE_PREFIXES.containsKey(role);
    }

    public static String resolveProfileAgentName(String language, String role, boolean dev) {
        if (!isProfileLanguageSupported(language) || !isRoleSupported(role)) {
            return null;
        }
        return ROLE_PREFIXES.get(role) + "-" + AGENT_SUFFIXES.get(language) + (dev ? "-测试" : "");
    }

    public static String resolveTargetAgentName(String currentAgentName, String language, boolean dev) {
        String languageSuffix = AGENT_SUFFIXES.get(language);
        String targetSuffix = languageSuffix == null ? null : languageSuffix + (dev ? "-测试" : "");
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
