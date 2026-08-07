-- 补偿：旧版 202608070154 误用 id=107（与 server.ota 冲突）导致参数未写入
-- Docker Host 网络下 manager-api Java 占用 8003，server HTTP 为 8004
INSERT INTO `sys_params` (`id`, `param_code`, `param_value`, `value_type`, `param_type`, `remark`)
SELECT 123, 'server.internal_api', 'http://127.0.0.1:8004', 'string', 1, 'xiaozhi-server 内部回调地址'
FROM DUAL
WHERE NOT EXISTS (
    SELECT 1 FROM `sys_params` WHERE `param_code` = 'server.internal_api'
);

UPDATE `sys_params`
SET `param_value` = 'http://127.0.0.1:8004',
    `remark` = 'xiaozhi-server 内部回调地址'
WHERE `param_code` = 'server.internal_api'
  AND (`param_value` IS NULL OR `param_value` IN ('null', 'http://127.0.0.1:8003'));
