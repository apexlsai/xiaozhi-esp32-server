USE `xiaozhi_esp32_server`;

START TRANSACTION;

UPDATE `ai_agent` a
JOIN `ai_model_config` m ON m.`id` = 'ASR_Qwen3Flash' AND m.`is_enabled` = 1
SET a.`asr_model_id` = m.`id`, a.`updated_at` = NOW()
WHERE a.`agent_name` IN ('儿童-韩语', '青年-韩语', '专家-韩语')
  AND a.`asr_model_id` IN ('ASR_OpenaiASR', 'ASR_Qwen3Flash_Korean');

DELETE FROM `ai_model_config`
WHERE `id` = 'ASR_Qwen3Flash_Korean'
  AND NOT EXISTS (SELECT 1 FROM `ai_agent` WHERE `asr_model_id` = 'ASR_Qwen3Flash_Korean')
  AND NOT EXISTS (SELECT 1 FROM `ai_agent_template` WHERE `asr_model_id` = 'ASR_Qwen3Flash_Korean');

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
