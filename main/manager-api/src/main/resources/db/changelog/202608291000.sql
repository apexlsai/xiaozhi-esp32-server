-- OpenAI 兼容 LLM 思考模式开关

UPDATE `ai_model_provider`
SET `fields` = JSON_ARRAY_APPEND(
    `fields`,
    '$',
    JSON_OBJECT(
        'key', 'enable_thinking',
        'label', '允许思考模式',
        'type', 'boolean',
        'default', false
    )
)
WHERE `id` = 'SYSTEM_LLM_openai'
  AND JSON_SEARCH(`fields`, 'one', 'enable_thinking', NULL, '$[*].key') IS NULL;

UPDATE `ai_model_config`
SET `config_json` = JSON_SET(`config_json`, '$.enable_thinking', false)
WHERE `model_type` = 'LLM'
  AND JSON_UNQUOTE(JSON_EXTRACT(`config_json`, '$.type')) = 'openai'
  AND JSON_EXTRACT(`config_json`, '$.enable_thinking') IS NULL;
