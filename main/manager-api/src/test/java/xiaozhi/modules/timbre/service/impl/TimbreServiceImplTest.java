package xiaozhi.modules.timbre.service.impl;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import org.junit.jupiter.api.Test;

import xiaozhi.common.redis.RedisUtils;
import xiaozhi.modules.timbre.dao.TimbreDao;
import xiaozhi.modules.timbre.entity.TimbreEntity;
import xiaozhi.modules.voiceclone.dao.VoiceCloneDao;
import xiaozhi.modules.voiceclone.entity.VoiceCloneEntity;

class TimbreServiceImplTest {

    @Test
    void defaultLanguageUsesFirstValidRegularTimbreLanguageWithoutCloneQuery() {
        TimbreDao timbreDao = mock(TimbreDao.class);
        VoiceCloneDao voiceCloneDao = mock(VoiceCloneDao.class);
        TimbreServiceImpl service = new TimbreServiceImpl(timbreDao, voiceCloneDao, mock(RedisUtils.class));
        TimbreEntity timbre = new TimbreEntity();
        timbre.setLanguages("，， ; 普通话；粤语");
        when(timbreDao.selectById("voice-id")).thenReturn(timbre);

        assertEquals("普通话", service.getDefaultLanguageById("voice-id"));

        verify(voiceCloneDao, never()).selectById("voice-id");
    }

    @Test
    void defaultLanguageFallsBackToCloneTimbre() {
        TimbreDao timbreDao = mock(TimbreDao.class);
        VoiceCloneDao voiceCloneDao = mock(VoiceCloneDao.class);
        TimbreServiceImpl service = new TimbreServiceImpl(timbreDao, voiceCloneDao, mock(RedisUtils.class));
        VoiceCloneEntity voiceClone = new VoiceCloneEntity();
        voiceClone.setLanguages("、, English，中文");
        when(voiceCloneDao.selectById("clone-id")).thenReturn(voiceClone);

        assertEquals("English", service.getDefaultLanguageById("clone-id"));
    }

    @Test
    void delimiterOnlyLanguageConfigurationReturnsNull() {
        TimbreDao timbreDao = mock(TimbreDao.class);
        VoiceCloneDao voiceCloneDao = mock(VoiceCloneDao.class);
        TimbreServiceImpl service = new TimbreServiceImpl(timbreDao, voiceCloneDao, mock(RedisUtils.class));
        TimbreEntity timbre = new TimbreEntity();
        timbre.setLanguages(",，、；;;,,");
        when(timbreDao.selectById("voice-id")).thenReturn(timbre);

        assertNull(service.getDefaultLanguageById("voice-id"));
    }
}
