# 小智服务端部署更改记录

## 更改日期: 2026-06-26

### 1. 修复 model.pt 目录问题

**文件**: `main/xiaozhi-server/models/SenseVoiceSmall/model.pt`

- **问题**: `model.pt` 被错误创建为目录而非文件
- **操作**: 删除目录，由 `run.sh` 自动创建占位文件

***

### 2. Docker 镜像源更改为国内源

**文件**: `main/xiaozhi-server/docker-compose_all.yml`

```yaml
# 原配置
xiaozhi-esp32-server-db:
  image: mysql:latest

xiaozhi-esp32-server-redis:
  image: redis:8.0

# 改为
xiaozhi-esp32-server-db:
  image: docker.m.daocloud.io/library/mysql:latest

xiaozhi-esp32-server-redis:
  image: docker.m.daocloud.io/library/redis:latest
```

***

### 3. 配置 server.secret

**文件**: `main/xiaozhi-server/data/.config.yaml`

```yaml
manager-api:
  url: http://xiaozhi-esp32-server-web:8002/xiaozhi
  secret: df7b3d50-ca02-4b39-8f2d-e64e08181a55  # 从数据库 sys_params 表获取
```

***

### 4. ASR 配置更改为本地 FunASR 服务

**位置**: MySQL 数据库 `xiaozhi_esp32_server`

#### 4.1 更新 ASR\_OpenaiASR 配置

**表**: `ai_model_config`

```sql
UPDATE ai_model_config 
SET config_json='{
  "type": "openai", 
  "api_key": "none", 
  "base_url": "http://192.168.1.71:15102/v1/audio/transcriptions", 
  "model_name": "fun-asr-nano", 
  "output_dir": "tmp/"
}' 
WHERE id='ASR_OpenaiASR';
```

#### 4.2 更新智能体模板使用 OpenAI ASR

**表**: `ai_agent_template`

```sql
UPDATE ai_agent_template SET asr_model_id='ASR_OpenaiASR';
```

#### 4.3 清除 Redis 缓存

```bash
docker exec xiaozhi-esp32-server-redis redis-cli FLUSHALL
```

***

## 如何更改 FunASR 服务地址

### 方法一：通过智控台（推荐）

1. 访问智控台: <http://192.168.1.71:8002/>
2. 登录超级管理员账号
3. 进入【模型配置】→【语音识别模型】
4. 找到「OpenAI语音识别」，点击【修改】
5. 修改以下字段：
   - `base_url`: 你的 FunASR 服务地址（如 `http://192.168.1.71:15102/v1/audio/transcriptions`）
   - `model_name`: 模型名称（如 `fun-asr-nano`）
6. 保存后重启服务

### 方法二：直接修改数据库

```bash
# 进入 MySQL
docker exec -it xiaozhi-esp32-server-db mysql -uroot -p123456 xiaozhi_esp32_server

# 查看当前配置
SELECT id, config_json FROM ai_model_config WHERE id='ASR_OpenaiASR';

# 更新配置（修改 base_url 和 model_name）
UPDATE ai_model_config 
SET config_json='{"type": "openai", "api_key": "none", "base_url": "http://你的IP:端口/v1/audio/transcriptions", "model_name": "你的模型名", "output_dir": "tmp/"}' 
WHERE id='ASR_OpenaiASR';

# 清除 Redis 缓存
docker exec xiaozhi-esp32-server-redis redis-cli FLUSHALL

# 重启服务
cd /home/jacob/Projects/xiaozhi-esp32-server/main/xiaozhi-server
docker compose -f docker-compose_all.yml restart xiaozhi-esp32-server
```

***

### 5. 配置 WebSocket 和 OTA 地址 (2026-06-26 21:32)

**表**: `sys_params`

```sql
UPDATE sys_params SET param_value='ws://192.168.1.71:8000/xiaozhi/v1/' WHERE param_code='server.websocket';
UPDATE sys_params SET param_value='http://192.168.1.71:8002/xiaozhi/ota/' WHERE param_code='server.ota';
```

***

### 6. 修正 FunASR 服务 IP 地址 (2026-06-26 23:26)

**表**: `ai_model_config`

```sql
-- 原配置 IP 错误：192.168.1.71
-- 修正为实际 FunASR 服务地址：192.168.1.199
UPDATE ai_model_config 
SET config_json='{
  "type": "openai", 
  "api_key": "none", 
  "base_url": "http://192.168.1.199:15102/v1/audio/transcriptions", 
  "model_name": "fun-asr-nano", 
  "output_dir": "tmp/"
}' 
WHERE id='ASR_OpenaiASR';
```

***

### 7. 接入本地 LLM 服务 (2026-06-27 00:35)

**表**: `ai_model_config`

#### 7.1 一键配置 LLM 和 SLM

```sql
-- 同时更新 LLM 和 SLM 配置（两者指向同一服务）
UPDATE ai_model_config 
SET config_json='{
  "type": "openai", 
  "api_key": "your_api_gateway_key_here", 
  "base_url": "http://192.168.1.199:15000/v1/", 
  "model_name": "museum-guide-agent"
}' 
WHERE id='LLM_ChatGLMLLM' OR id='SLM_ChatGLMLLM';
```

#### 7.2 清除 Redis 缓存并重启 server

```bash
docker exec xiaozhi-esp32-server-redis redis-cli FLUSHALL
cd /home/jacob/Projects/xiaozhi-esp32-server
docker compose -f docker-compose-ksz.yml restart xiaozhi-esp32-server
```

#### 7.3 验证配置是否生效

```bash
# 查看数据库配置
docker exec xiaozhi-esp32-server-db mysql -uroot -p123456 -D xiaozhi_esp32_server -e "SELECT id, config_json FROM ai_model_config WHERE id='LLM_ChatGLMLLM';"

# 查看 server 启动日志
docker logs --tail 20 xiaozhi-esp32-server
```

#### 7.4 测试 LLM 接口连通性

```bash
curl --request POST \
  --url http://192.168.1.199:15000/v1/chat/completions \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "museum-guide-agent",
    "messages": [{"role": "user", "content": "展馆里有食品卖吗？"}],
    "stream": false,
    "extra_body": {
      "device_id": "98:88:e0:6c:29:1c",
      "language": "en",
      "last_beacon_id": "beacon-abc-123"
    }
  }'
```

#### 7.5 查看发送给 LLM 的请求日志

触发设备语音对话后，查看日志中的请求参数：

```bash
docker logs -f xiaozhi-esp32-server | grep "发送给LLM的请求"
```

日志会打印完整请求，包含 `extra_body` 中的 `device_id`、`language`、`last_beacon_id`。

***

## 当前服务地址

| 服务        | 地址                                                    |
| --------- | ----------------------------------------------------- |
| WebSocket | `ws://192.168.1.71:8000/xiaozhi/v1/`                  |
| 智控台       | <http://192.168.1.71:8002/>                           |
| OTA 接口    | <http://192.168.1.71:8002/xiaozhi/ota/>               |
| 视觉接口      | <http://192.168.1.71:8003/mcp/vision/explain>         |
| 本地 FunASR | <http://192.168.1.199:15102/v1/audio/transcriptions>  |
| 本地 LLM    | <http://192.168.1.199:15000/v1/> (museum-guide-agent) |

<br />

## 最简单的方法：直接从数据库拿用户 token

你已经有数据库权限，直接查 sys\_user\_token 表：

```Shell
docker exec -i xiaozhi-esp32-server-db mysql -uroot -p123456 xiaozhi_esp32_server -e "SELECT user_id, token, expire_date FROM sys_user_token WHERE user_id=(SELECT id FROM sys_user WHERE username='Kszai');"
```

临时绕过token过期

```SQL
UPDATE sys_user_token SET expire_date = DATE_ADD(NOW(), INTERVAL 12 HOUR) WHERE token = '766b843574eb733476bd8d892aa4868d';
```

***

## 8. 设备属性 language 校验与 LLM 请求日志 (2026-06-30)

### 8.1 设备语言属性写入校验

**文件**: `main/manager-api/src/main/java/xiaozhi/modules/device/service/impl/DeviceAttributeServiceImpl.java`

- 限制 `language` 属性仅允许 `en` 或 `zh-cn`
- 非法值返回错误码 `10250`，提示 `Language type only supports en or zh-cn`
- 同步更新 i18n 消息文件

### 8.2 设备属性变更后自动同步到 LLM 请求

**文件**: `main/xiaozhi-server/core/connection.py`

- 每次调用 LLM 前，自动从 `manager-api` 刷新设备扩展属性
- 缓存间隔 5 秒，避免频繁请求
- 解决通过智控台/接口修改语言后需重启设备才生效的问题

### 8.3 打印发送给 LLM 的请求参数

**文件**: `main/xiaozhi-server/core/providers/llm/openai/openai.py`

- 在 `response` 和 `response_with_functions` 中打印完整请求参数
- 日志关键字：`发送给LLM的请求:`

### 8.4 新增 KSZ 本地构建版 Docker Compose

**文件**: `docker-compose-ksz.yml`、`main/xiaozhi-server/docker-compose-ksz.yml`

- 新增 `docker-compose-ksz.yml`（全模块 Host 网络模式）
- 新增 `main/xiaozhi-server/docker-compose-ksz.yml`（单 Server 模块）
- KSZ 版使用本地构建镜像：
  - `server` 使用 `xiaozhi-esp32-server:server_local`
  - `web` 使用 `xiaozhi-esp32-server:web_local`
- 原版 `docker-compose.yml` 保持官方远程镜像不变

<br />

### 8.5构建命令

```bash
cd /home/jacob/Projects/xiaozhi-esp32-server
docker compose -f docker-compose-ksz.yml up -d
```

### 8.6 测试命令

```bash
# 非法语言值
PUT /xiaozhi/device/attribute/{deviceId}/language
Body: cn
# 返回 {"code":10250,"msg":"Language type only supports en or zh-cn","data":null}

# 合法语言值
PUT /xiaozhi/device/attribute/{deviceId}/language
Body: en
# 返回 {"code":0,"msg":"success","data":null}

# 查看 LLM 请求日志
docker logs -f xiaozhi-esp32-server
```

mysql连接方法

```
Host: 192.168.1.71
Port: 3306
User: root
Password: 123456
Database: xiaozhi_esp32_server
```

***

## 9. 设备属性表结构重构 (2026-07-10)

### 9.1 表结构变更

**文件**: `main/manager-api/src/main/resources/db/changelog/202607101600.sql`

将 `ai_device_attribute` 表从 key-value 模式改为直接字段模式：

| 旧表结构                          | 新表结构                      |
| ----------------------------- | ------------------------- |
| `attr_key` (属性名)              | `language` (语言字段)         |
| `attr_value` (属性值)            | `last_beacon_id` (信标ID字段) |
| 联合唯一键 `(device_id, attr_key)` | 唯一键 `(device_id)`         |

### 9.2 手动执行数据库迁移

```bash
# 进入 MySQL
docker exec -it xiaozhi-esp32-server-db mysql -uroot -p123456 xiaozhi_esp32_server

# 执行迁移脚本
SOURCE /path/to/202607101600.sql;

# 或者直接执行以下 SQL：
```

```sql
-- 1. 备份旧表
CREATE TABLE IF NOT EXISTS `ai_device_attribute_backup` AS SELECT * FROM `ai_device_attribute`;

-- 2. 删除旧表
DROP TABLE IF EXISTS `ai_device_attribute`;

-- 3. 创建新表
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

-- 4. 迁移数据
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
```

### 9.3 Java 代码修改

**修改的文件**：

| 文件                                | 修改内容                                                 |
| --------------------------------- | ---------------------------------------------------- |
| `DeviceAttributeEntity.java`      | 将 `attrKey`、`attrValue` 改为 `language`、`lastBeaconId` |
| `DeviceAttributeService.java`     | 新增 `updateLanguage()`、`updateLastBeaconId()` 方法      |
| `DeviceAttributeServiceImpl.java` | 实现新接口，保留兼容方法 `saveOrUpdateAttribute()`               |
| `DeviceAttributeController.java`  | 新增 `/language`、`/last_beacon_id` 端点                  |
| `DeviceController.java`           | `reportDeviceEvent()` 使用新方法                          |

### 9.4 API 接口变更

**新接口**：

```bash
# 获取设备属性实体
GET /xiaozhi/device/attribute/{deviceId}/entity

# 更新设备语言
PUT /xiaozhi/device/attribute/{deviceId}/language
Body: en

# 更新设备蓝牙信标
PUT /xiaozhi/device/attribute/{deviceId}/last_beacon_id
Body: beacon-abc-123
```

**兼容旧接口**（仍可使用）：

```bash
# 获取设备所有属性（返回 Map）
GET /xiaozhi/device/attribute/{deviceId}

# 更新属性（通用）
PUT /xiaozhi/device/attribute/{deviceId}/{attrKey}
Body: value
```

### 9.5 重新构建并部署

```bash
cd /home/jacob/Projects/xiaozhi-esp32-server

# 重新构建 manager-api
docker compose -f docker-compose-ksz.yml build xiaozhi-esp32-server-web

# 重启服务
docker compose -f docker-compose-ksz.yml up -d
```

### 9.6 验证

```bash
# 查看新表结构
docker exec xiaozhi-esp32-server-db mysql -uroot -p123456 -D xiaozhi_esp32_server -e "DESC ai_device_attribute;"

# 查看迁移后的数据
docker exec xiaozhi-esp32-server-db mysql -uroot -p123456 -D xiaozhi_esp32_server -e "SELECT * FROM ai_device_attribute;"
```

***

## 10. 修复设备连不上/说话不识别 + Python 报错信息优化 (2026-07-14)

### 10.1 问题现象

设备能连上、能收到 `listen`/`mcp` 消息，但**说话始终不被识别**（单独测 ASR 服务正常）。server 日志只有一句含糊的 `实例化组件失败: 'TTS'`。

### 10.2 根因（一条因果链）

1. 运行中的 `web`(manager-api) 是旧 jar（7·1 构建），仍按旧结构查询 `ai_device_attribute.attr_key`；
2. 但该表已被手动迁移成新结构（见第 9 节，`language`/`last_beacon_id`）→ `/config/agent-models` 抛 `Unknown column 'attr_key'` → **HTTP 500**；
3. Python 端 `get_private_config_from_api` 用 `asyncio.gather(return_exceptions=True)` 把该异常**静默吞成空配置**；
4. 空配置导致连接的 `selected_module` 缺少 `TTS`，`initialize_tts()` 执行 `config["selected_module"]["TTS"]` 抛 `KeyError`；
5. 该异常发生在 `_initialize_components()` 最前面，一抛异常就跳过后面的 `asr.open_audio_channels()`；
6. 结果：音频入队 `asr_audio_queue` 但**无消费者** → 说话不被识别。

### 10.3 处置一：重建 web 镜像（对齐 jar 与已迁移的 DB）

```bash
cd /home/jacob/Projects/xiaozhi-esp32-server
docker build -t xiaozhi-esp32-server:web_local -f Dockerfile-web .
docker rm -f xiaozhi-esp32-server-web
docker compose -f docker-compose-ksz.yml up -d xiaozhi-esp32-server-web
docker exec xiaozhi-esp32-server-redis redis-cli del "server:config"   # 清服务端配置缓存
docker restart xiaozhi-esp32-server
```

当前源码的 `DeviceAttributeEntity` 已使用新字段（`language`/`lastBeaconId`），重建后 jar 与 DB 结构一致，`/config/agent-models` 恢复 `code:0`，`selected_module` 正常带回 `TTS_EdgeTTS`。

### 10.4 处置二：Python 后端报错信息优化（防止同类问题再次“查不出原因”）

> 本次仅优化报错信息，未加重试/兜底、未改数据库。

**文件**: `main/xiaozhi-server/config/config_loader.py`

- `get_private_config_from_api`：接口非业务异常（500/超时/连接失败）此前被静默吞成空配置，现改为**显式打印**设备号、异常类型、完整堆栈，并提示排查 manager-api 与 jar/DB 一致性；替换词失败单独告警。
- 用**函数内惰性导入** `setup_logging`，避免与 `config.logger` 形成模块级循环依赖。

**文件**: `main/xiaozhi-server/core/connection.py`

- `_initialize_private_config_async`：差异化配置只剩 `delete_audio`（等于没拿到任何模块）时，不再误报“获取成功”，改为 **error 告警**并点明“后续会因缺少 selected\_module(如 TTS) 初始化失败”。
- `_initialize_components`：新增 `KeyError` 专门分支，把裸的 `实例化组件失败: 'TTS'` 换成——缺哪个键、当前 `selected_module`、大概率原因、完整堆栈；其余异常统一带类型名 + 堆栈。
- `initialize_modules` 并行初始化的吞错点也补上类型名 + 堆栈。

### 10.5 重建 server 镜像并部署

```bash
cd /home/jacob/Projects/xiaozhi-esp32-server
docker build -t xiaozhi-esp32-server:server_local -f Dockerfile-server .
docker rm -f xiaozhi-esp32-server
docker compose -f docker-compose-ksz.yml up -d xiaozhi-esp32-server
docker logs --tail 10 xiaozhi-esp32-server   # 确认无循环导入、VAD/ASR 初始化正常
```

### 10.6 遗留隐患（暂未处理，建议后续跟进）

- 迁移脚本 `202607101600.sql` **未登记**进 `db.changelog-master.yaml`，本次是手动执行的，`DATABASECHANGELOG` 也无记录。全新部署时该表会停在旧结构，新 jar 会再次报 `attr_key` 错。
- 若要补登记，需改成幂等写法/加 `preConditions`，否则重跑会 `DROP` 表并从旧备份还原，存在丢数据风险。

***

## 11. MCP (Model Context Protocol) 集成说明 (2026-07-17)

### 11.1 工具类型架构

小智服务端支持 **5 种工具类型**，通过统一的工具处理器协调：

| 工具类型            | 执行器                  | 来源           |
| --------------- | -------------------- | ------------ |
| `SERVER_PLUGIN` | ServerPluginExecutor | 内置插件函数       |
| `SERVER_MCP`    | ServerMCPExecutor    | 外部 MCP 服务配置  |
| `DEVICE_IOT`    | DeviceIoTExecutor    | 设备端 IoT 动态注册 |
| `DEVICE_MCP`    | DeviceMCPExecutor    | 设备端 MCP 服务   |
| `MCP_ENDPOINT`  | MCPEndpointExecutor  | 远程 MCP 接入点   |

### 11.2 内置插件函数

位置：`main/xiaozhi-server/plugins_func/functions/`

| 插件                        | 功能                  |
| ------------------------- | ------------------- |
| `get_weather`             | 获取天气                |
| `get_time`                | 获取时间                |
| `web_search`              | 网页搜索                |
| `play_music`              | 播放音乐                |
| `change_role`             | 切换角色                |
| `handle_exit_intent`      | 退出意图处理              |
| `call_device`             | 调用设备                |
| `get_news_from_newsnow`   | 获取新闻 (NewsNow)      |
| `get_news_from_chinanews` | 获取新闻 (中国新闻网)        |
| `search_from_ragflow`     | RAGFlow 知识库搜索       |
| `hass_get_state`          | Home Assistant 获取状态 |
| `hass_set_state`          | Home Assistant 设置状态 |
| `hass_play_music`         | Home Assistant 播放音乐 |
| `hass_init`               | Home Assistant 初始化  |

### 11.3 外部 MCP 服务配置

配置文件：`data/.mcp_server_settings.json`

支持用户自定义添加任意 MCP 服务，示例配置见：`main/xiaozhi-server/mcp_server_settings.json`

支持的传输模式：

- **stdio**：本地命令行工具（通过 `command` + `args` 配置）
- **sse**：Server-Sent Events（通过 `url` + `headers` 配置）
- **streamable-http**：流式 HTTP（通过 `url` + `"transport": "streamable-http"` 配置）

### 11.4 相关代码位置

| 文件                                                | 说明         |
| ------------------------------------------------- | ---------- |
| `core/providers/tools/unified_tool_handler.py`    | 统一工具处理器    |
| `core/providers/tools/server_mcp/mcp_manager.py`  | MCP 服务管理器  |
| `core/providers/tools/server_mcp/mcp_client.py`   | MCP 客户端实现  |
| `core/providers/tools/server_mcp/mcp_executor.py` | MCP 工具执行器  |
| `core/providers/tools/device_iot/iot_executor.py` | 设备 IoT 执行器 |

### 11.5 项目 MCP 文档

- [mcp-endpoint-enable.md](file:///home/jacob/Projects/xiaozhi-esp32-server/docs/mcp-endpoint-enable.md) - MCP 接入点部署指南
- [mcp-endpoint-integration.md](file:///home/jacob/Projects/xiaozhi-esp32-server/docs/mcp-endpoint-integration.md) - MCP 接入点使用指南（接入计算器示例）
- [mcp-vision-integration.md](file:///home/jacob/Projects/xiaozhi-esp32-server/docs/mcp-vision-integration.md) - 视觉模型 MCP 集成指南
- [mcp-get-device-info.md](file:///home/jacob/Projects/xiaozhi-esp32-server/docs/mcp-get-device-info.md) - MCP 方法获取设备信息

