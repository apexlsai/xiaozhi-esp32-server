USE `xiaozhi_esp32_server`;

START TRANSACTION;

INSERT INTO `ai_model_config` (
    `id`, `model_type`, `model_code`, `model_name`, `is_default`, `is_enabled`,
    `config_json`, `doc_link`, `remark`, `sort`, `creator`, `create_date`
)
SELECT
    'ASR_Qwen3Flash_Korean', `model_type`, 'Qwen3Flash_Korean', 'Qwen3Flash韩语专用', 0, 1,
    JSON_SET(`config_json`, '$.language', 'ko'), `doc_link`, '韩语固定语种识别', `sort`, `creator`, NOW()
FROM `ai_model_config`
WHERE `id` = 'ASR_Qwen3Flash'
  AND NOT EXISTS (SELECT 1 FROM `ai_model_config` WHERE `id` = 'ASR_Qwen3Flash_Korean');

UPDATE `ai_agent` a
JOIN `ai_model_config` m ON m.`id` = 'ASR_Qwen3Flash_Korean'
SET a.`asr_model_id` = m.`id`, a.`updated_at` = NOW()
WHERE a.`agent_name` IN ('儿童-韩语', '青年-韩语', '专家-韩语')
  AND a.`asr_model_id` IN ('ASR_OpenaiASR', 'ASR_Qwen3Flash')
  AND JSON_UNQUOTE(JSON_EXTRACT(m.`config_json`, '$.language')) = 'ko';

INSERT INTO `ai_device_attribute` (`device_id`, `language`, `agent_name`, `create_date`)
SELECT d.`mac_address`, 'ko', a.`agent_name`, NOW()
FROM `ai_device` d
JOIN `ai_agent` a ON a.`id` = d.`agent_id`
LEFT JOIN `ai_device_attribute` attr ON attr.`device_id` = d.`mac_address`
WHERE a.`agent_name` IN ('儿童-韩语', '青年-韩语', '专家-韩语') AND attr.`id` IS NULL;

UPDATE `ai_device_attribute` attr
JOIN `ai_device` d ON d.`mac_address` = attr.`device_id`
JOIN `ai_agent` a ON a.`id` = d.`agent_id`
SET attr.`language` = 'ko', attr.`agent_name` = a.`agent_name`, attr.`update_date` = NOW()
WHERE a.`agent_name` IN ('儿童-韩语', '青年-韩语', '专家-韩语')
  AND (NOT (attr.`language` <=> 'ko') OR NOT (attr.`agent_name` <=> a.`agent_name`));

COMMIT;
