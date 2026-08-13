UPDATE `ai_device_attribute`
SET `language` = CASE `language`
    WHEN 'zh-CN-test' THEN 'zh-CN'
    WHEN 'en-test' THEN 'en'
    WHEN 'ja-test' THEN 'ja'
    WHEN 'ko-test' THEN 'ko'
    WHEN 'zh-CN-yue-test' THEN 'zh-CN-yue'
    WHEN 'zh-CN-sichuan-test' THEN 'zh-CN-sichuan'
    WHEN 'zh-CN-sichuan-te' THEN 'zh-CN-sichuan'
    WHEN 'zh-CN-shanghai-test' THEN 'zh-CN-shanghai'
    WHEN 'zh-CN-shanghai-t' THEN 'zh-CN-shanghai'
    WHEN 'zh-CN-minnan-test' THEN 'zh-CN-minnan'
    WHEN 'zh-CN-minnan-tes' THEN 'zh-CN-minnan'
    WHEN 'zh-CN-shanxi-test' THEN 'zh-CN-shanxi'
    WHEN 'zh-CN-shanxi-tes' THEN 'zh-CN-shanxi'
    ELSE `language`
END
WHERE `language` IN (
    'zh-CN-test',
    'en-test',
    'ja-test',
    'ko-test',
    'zh-CN-yue-test',
    'zh-CN-sichuan-test',
    'zh-CN-sichuan-te',
    'zh-CN-shanghai-test',
    'zh-CN-shanghai-t',
    'zh-CN-minnan-test',
    'zh-CN-minnan-tes',
    'zh-CN-shanxi-test',
    'zh-CN-shanxi-tes'
);
