USE `xiaozhi_esp32_server`;

UPDATE `sys_params`
SET `param_value` = CASE `param_code`
    WHEN 'server.websocket' THEN REPLACE(`param_value`, ':8000', ':38000')
    WHEN 'server.ota' THEN REPLACE(`param_value`, ':8002', ':38002')
    WHEN 'server.http_port' THEN '38004'
    WHEN 'server.internal_api' THEN REPLACE(`param_value`, ':8004', ':38004')
    WHEN 'server.vision_explain' THEN REPLACE(`param_value`, ':8004', ':38004')
    WHEN 'server.mqtt_gateway' THEN REPLACE(`param_value`, ':1883', ':31883')
    WHEN 'server.udp_gateway' THEN REPLACE(`param_value`, ':8884', ':38884')
    WHEN 'server.mqtt_manager_api' THEN REPLACE(`param_value`, ':8007', ':38007')
END
WHERE `param_code` IN (
    'server.websocket', 'server.ota', 'server.http_port', 'server.internal_api',
    'server.vision_explain', 'server.mqtt_gateway', 'server.udp_gateway',
    'server.mqtt_manager_api'
);
