-- 设备扩展属性增加关联智能体名称（冗余字段，权威绑定仍为 ai_device.agent_id）
-- 迁移日期: 2026-07-31

SET @dbname = DATABASE();
SET @tablename = 'ai_device_attribute';
SET @columnname = 'agent_name';
SET @preparedStatement = (SELECT IF(
  (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = @tablename AND COLUMN_NAME = @columnname
  ) > 0,
  'SELECT 1',
  'ALTER TABLE `ai_device_attribute` ADD COLUMN `agent_name` VARCHAR(64) DEFAULT NULL COMMENT ''关联智能体名称'' AFTER `last_beacon_id`'
));
PREPARE alterIfNotExists FROM @preparedStatement;
EXECUTE alterIfNotExists;
DEALLOCATE PREPARE alterIfNotExists;

SET @indexname = 'idx_device_attr_agent_name';
SET @preparedStatement = (SELECT IF(
  (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = @dbname AND TABLE_NAME = @tablename AND INDEX_NAME = @indexname
  ) > 0,
  'SELECT 1',
  'ALTER TABLE `ai_device_attribute` ADD INDEX `idx_device_attr_agent_name` (`agent_name`)'
));
PREPARE alterIndexIfNotExists FROM @preparedStatement;
EXECUTE alterIndexIfNotExists;
DEALLOCATE PREPARE alterIndexIfNotExists;

UPDATE `ai_device_attribute` a
INNER JOIN `ai_device` d ON a.device_id = d.mac_address
INNER JOIN `ai_agent` ag ON d.agent_id = ag.id
SET a.agent_name = ag.agent_name
WHERE a.agent_name IS NULL OR a.agent_name = '';
