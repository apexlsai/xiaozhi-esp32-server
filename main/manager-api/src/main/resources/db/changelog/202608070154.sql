INSERT INTO `sys_params` (`id`, `param_code`, `param_value`, `value_type`, `param_type`, `remark`)
VALUES (107, 'server.internal_api', 'http://127.0.0.1:8003', 'string', 1, 'xiaozhi-server 内部回调地址')
ON DUPLICATE KEY UPDATE `param_value` = `param_value`;
