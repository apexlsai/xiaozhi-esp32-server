package xiaozhi.modules.device.support;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;

import org.junit.jupiter.api.Test;

class DeviceLanguageSpecTest {
    private static final List<String> BASE_LANGUAGES = List.of(
            "zh-CN", "en", "ja", "ko", "zh-CN-yue", "zh-CN-sichuan",
            "zh-CN-shanghai", "zh-CN-minnan", "zh-CN-shanxi");

    @Test
    void acceptsOnlyBaseLanguages() {
        for (String language : BASE_LANGUAGES) {
            assertTrue(DeviceLanguageSpec.isSupported(language));
            assertFalse(DeviceLanguageSpec.isSupported(language + "-test"));
        }
    }

    @Test
    void rejectsInvalidLanguages() {
        assertFalse(DeviceLanguageSpec.isSupported("zh-CN-yue-Test"));
        assertFalse(DeviceLanguageSpec.isSupported("zh-CN-yue-test-test"));
        assertFalse(DeviceLanguageSpec.isSupported("unknown-test"));
    }

    @Test
    void resolvesProductionAndTestAgentNames() {
        assertEquals("小硕-粤语-测试",
                DeviceLanguageSpec.resolveTargetAgentName("小硕-汉语", "zh-CN-yue", true));
        assertEquals("小硕-英语-测试",
                DeviceLanguageSpec.resolveTargetAgentName("小硕-粤语-测试", "en", true));
        assertEquals("小硕-英语",
                DeviceLanguageSpec.resolveTargetAgentName("小硕-粤语-测试", "en", false));
    }
}
