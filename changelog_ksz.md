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
-- 修正为实际 FunASR 服务地址：192.168.1.56
UPDATE ai_model_config 
SET config_json='{
  "type": "openai", 
  "api_key": "none", 
  "base_url": "http://192.168.1.56:15102/v1/audio/transcriptions", 
  "model_name": "fun-asr-nano", 
  "output_dir": "tmp/"
}' 
WHERE id='ASR_OpenaiASR';
```

***

### 7. 接入本地 LLM 服务 (2026-06-27 00:35)

**表**: `ai_model_config`

```sql
UPDATE ai_model_config 
SET config_json='{
  "type": "openai", 
  "api_key": "your_api_gateway_key_here", 
  "base_url": "http://192.168.1.56:15000/v1/", 
  "model_name": "museum-guide-agent"
}' 
WHERE id='LLM_ChatGLMLLM';
```

***

## 当前服务地址

| 服务        | 地址                                                   |
| --------- | ---------------------------------------------------- |
| WebSocket | `ws://192.168.1.71:8000/xiaozhi/v1/`                 |
| 智控台       | <http://192.168.1.71:8002/>                          |
| OTA 接口    | <http://192.168.1.71:8002/xiaozhi/ota/>              |
| 视觉接口      | <http://192.168.1.71:8003/mcp/vision/explain>        |
| 本地 FunASR | <http://192.168.1.56:15102/v1/audio/transcriptions>  |
| 本地 LLM    | <http://192.168.1.56:15000/v1/> (museum-guide-agent) |

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

### 8.4 Docker 镜像切换为本地构建

**文件**: `docker-compose.yml`、`main/xiaozhi-server/docker-compose.yml`

- `server` 镜像改为 `xiaozhi-esp32-server:server_local`
- `web` 镜像改为 `xiaozhi-esp32-server:web_local`

### 8.5 测试命令

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

