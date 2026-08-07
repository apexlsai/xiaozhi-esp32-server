-- 新增小米 MiMo TTS 供应器与模型配置
-- 日期: 2026-08-01

-- 供应器定义（智控台「模型配置」页面可看到并编辑字段）
delete from `ai_model_provider` where id = 'SYSTEM_TTS_MimoTTS';
INSERT INTO `ai_model_provider` (`id`, `model_type`, `provider_code`, `name`, `fields`, `sort`, `creator`, `create_date`, `updater`, `update_date`) VALUES
('SYSTEM_TTS_MimoTTS', 'TTS', 'mimo', '小米MiMo语音合成', '[
  {"key": "api_key", "type": "string", "label": "API Key"},
  {"key": "api_url", "type": "string", "label": "API地址"},
  {"key": "model", "type": "string", "label": "模型", "default": "mimo-v2.5-tts"},
  {"key": "voice", "type": "string", "label": "音色", "default": "mimo_default"},
  {"key": "format", "type": "string", "label": "音频格式", "default": "wav"},
  {"key": "style", "type": "string", "label": "风格描述（可选）"},
  {"key": "output_dir", "type": "string", "label": "输出目录", "default": "tmp/"}
]', 20, 1, NOW(), 1, NOW());

-- 默认模型配置（智控台「模型配置」列表中的预设项）
delete from `ai_model_config` where id = 'TTS_MimoTTS';
INSERT INTO `ai_model_config` (`id`, `model_type`, `model_code`, `model_name`, `is_default`, `is_enabled`, `config_json`, `doc_link`, `remark`, `sort`, `creator`, `create_date`, `updater`, `update_date`) VALUES
('TTS_MimoTTS', 'TTS', 'MimoTTS', '小米MiMo语音合成', 0, 1, '{
  "type": "mimo",
  "api_key": "",
  "api_url": "https://api.xiaomimimo.com/v1/chat/completions",
  "model": "mimo-v2.5-tts",
  "voice": "mimo_default",
  "format": "wav",
  "style": "",
  "output_dir": "tmp/"
}', 'https://api.xiaomimimo.com', 'MiMo TTS 配置说明：
1. 前往 https://api.xiaomimimo.com 获取 api-key
2. 模型固定为 mimo-v2.5-tts
3. voice 默认 mimo_default，暂不可选其他
4. style 为可选语气描述，作为 user message 传入，可留空
5. 该接口返回 JSON 中的 base64 音频，由服务端解码后流式发送', 20, 1, NOW(), 1, NOW());

-- 默认音色（供智能体模板或用户选择）
delete from `ai_tts_voice` where id = 'TTS_MimoTTS0001';
INSERT INTO `ai_tts_voice` VALUES ('TTS_MimoTTS0001', 'TTS_MimoTTS', 'MiMo默认音色', 'mimo_default', '中文', NULL, NULL, NULL, NULL, 1, NULL, NULL, NULL, NULL);
