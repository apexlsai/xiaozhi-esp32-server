-- MiMo TTS 分段情绪描述参数

UPDATE `ai_model_provider`
SET `fields` = JSON_ARRAY(
    JSON_OBJECT('key', 'api_key', 'type', 'string', 'label', 'API Key'),
    JSON_OBJECT('key', 'api_url', 'type', 'string', 'label', 'API地址'),
    JSON_OBJECT('key', 'model', 'type', 'string', 'label', '模型', 'default', 'mimo-v2.5-tts'),
    JSON_OBJECT('key', 'voice', 'type', 'string', 'label', '音色', 'default', 'mimo_default'),
    JSON_OBJECT('key', 'format', 'type', 'string', 'label', '音频格式', 'default', 'wav'),
    JSON_OBJECT('key', 'style', 'type', 'string', 'label', '基础风格描述（可选）'),
    JSON_OBJECT(
        'key', 'emotion_style_enabled',
        'type', 'boolean',
        'label', '启用分段情绪描述',
        'default', true
    ),
    JSON_OBJECT(
        'key', 'emotion_styles',
        'type', 'dict',
        'label', '情绪描述映射（JSON）',
        'default', JSON_OBJECT()
    ),
    JSON_OBJECT('key', 'output_dir', 'type', 'string', 'label', '输出目录', 'default', 'tmp/')
)
WHERE `id` = 'SYSTEM_TTS_MimoTTS';

UPDATE `ai_model_config`
SET `config_json` = JSON_SET(
    `config_json`,
    '$.emotion_style_enabled', true,
    '$.emotion_styles', JSON_OBJECT(
        'neutral', '平稳自然，语气克制。',
        'warm', '亲切温暖，带自然笑意。',
        'joy', '声音轻快，带明显笑意，节奏活泼。',
        'sad', '声音温和低沉，语速稍缓，避免夸张哭腔。',
        'sleepy', '声音轻柔慵懒，语速稍缓，避免含混。',
        'angry', '语气坚定有力度，保持克制，不要吼叫。',
        'surprise', '带短暂惊讶感，语调适度上扬，同时保持专业。',
        'thinking', '若有所思，语速稍缓，带探索感。',
        'confident', '从容自信，节奏清晰，带轻微幽默感。'
    )
)
WHERE `id` = 'TTS_MimoTTS';

UPDATE `ai_model_config`
SET `remark` = 'MiMo TTS 配置说明：
1. 前往 https://api.xiaomimimo.com 获取 api-key
2. 模型固定为 mimo-v2.5-tts
3. voice 默认 mimo_default，暂不可选其他
4. style 为基础风格描述，作为 user message 传入
5. emotion_style_enabled 控制是否按回复 Emoji 追加分段情绪描述
6. emotion_styles 可按 neutral、warm、joy、sad、sleepy、angry、surprise、thinking、confident 覆盖内置描述，也可用具体 Emoji 作为键
7. 该接口返回 JSON 中的 base64 音频，由服务端解码后流式发送'
WHERE `id` = 'TTS_MimoTTS';
