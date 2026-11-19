-- 新增腾讯元宝搜索插件
INSERT INTO ai_model_provider (id, model_type, provider_code, name, fields,
                               sort, creator, create_date, updater, update_date)
SELECT 'SYSTEM_PLUGIN_YUANBAO_SEARCH',
       'Plugin',
       'yuanbao_search',
       '元宝搜索',
       JSON_ARRAY(
               JSON_OBJECT(
                       'key', 'secret_id',
                       'type', 'string',
                       'label', '腾讯云 Secret ID',
                       'default', ''
               ),
               JSON_OBJECT(
                       'key', 'secret_key',
                       'type', 'string',
                       'label', '腾讯云 Secret Key',
                       'default', ''
               ),
               JSON_OBJECT(
                       'key', 'token',
                       'type', 'string',
                       'label', '临时安全令牌（可选）',
                       'default', ''
               ),
               JSON_OBJECT(
                       'key', 'max_results',
                       'type', 'number',
                       'label', '返回数量',
                       'default', 5
               ),
               JSON_OBJECT(
                       'key', 'description',
                       'type', 'string',
                       'label', '工具描述',
                       'default', '使用腾讯元宝搜索检索互联网信息，适用于需要实时核实的问题。'
               )
       ),
       45, 0, NOW(), 0, NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM ai_model_provider WHERE id = 'SYSTEM_PLUGIN_YUANBAO_SEARCH'
);
