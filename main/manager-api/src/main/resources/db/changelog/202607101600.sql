-- 设备扩展属性表结构变更：从 key-value 模式改为直接字段模式
-- 迁移日期: 2026-07-10

-- 1. 备份旧表数据
CREATE TABLE IF NOT EXISTS `ai_device_attribute_backup` AS SELECT * FROM `ai_device_attribute`;

-- 2. 删除旧表
DROP TABLE IF EXISTS `ai_device_attribute`;

-- 3. 创建新表结构（device_id 唯一）
CREATE TABLE `ai_device_attribute` (
    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT 'ID',
    `device_id` VARCHAR(64) NOT NULL COMMENT '设备ID（mac地址）',
    `language` VARCHAR(16) DEFAULT NULL COMMENT '设备语言（en, zh-cn）',
    `last_beacon_id` VARCHAR(128) DEFAULT NULL COMMENT '最近检测到的蓝牙信标ID',
    `creator` BIGINT COMMENT '创建者',
    `create_date` DATETIME COMMENT '创建时间',
    `updater` BIGINT COMMENT '更新者',
    `update_date` DATETIME COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_device_id` (`device_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备扩展属性';

-- 4. 从备份表迁移数据（将 key-value 转为列）
INSERT INTO `ai_device_attribute` (`device_id`, `language`, `last_beacon_id`, `creator`, `create_date`, `updater`, `update_date`)
SELECT 
    b.device_id,
    MAX(CASE WHEN b.attr_key = 'language' THEN b.attr_value END) AS language,
    MAX(CASE WHEN b.attr_key = 'last_beacon_id' THEN b.attr_value END) AS last_beacon_id,
    MAX(b.creator) AS creator,
    MAX(b.create_date) AS create_date,
    MAX(b.updater) AS updater,
    MAX(b.update_date) AS update_date
FROM `ai_device_attribute_backup` b
GROUP BY b.device_id;

-- 5. 删除备份表（可选，建议生产环境保留一段时间后再删除）
-- DROP TABLE IF EXISTS `ai_device_attribute_backup`;
