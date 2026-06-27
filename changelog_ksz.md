# 小智服务端部署更改记录

## 更改日期: 2026-06-26

### 1. 修复 model.pt 目录问题
**文件**: `main/xiaozhi-server/models/SenseVoiceSmall/model.pt`
- **问题**: `model.pt` 被错误创建为目录而非文件
- **操作**: 删除目录，由 `run.sh` 自动创建占位文件

---

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

---

### 3. 配置 server.secret
**文件**: `main/xiaozhi-server/data/.config.yaml`

```yaml
manager-api:
  url: http://xiaozhi-esp32-server-web:8002/xiaozhi
  secret: df7b3d50-ca02-4b39-8f2d-e64e08181a55  # 从数据库 sys_params 表获取
```

---

### 4. ASR 配置更改为本地 FunASR 服务
**位置**: MySQL 数据库 `xiaozhi_esp32_server`

#### 4.1 更新 ASR_OpenaiASR 配置
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

---

## 如何更改 FunASR 服务地址

### 方法一：通过智控台（推荐）
1. 访问智控台: http://192.168.1.71:8002/
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

---

### 5. 配置 WebSocket 和 OTA 地址 (2026-06-26 21:32)
**表**: `sys_params`

```sql
UPDATE sys_params SET param_value='ws://192.168.1.71:8000/xiaozhi/v1/' WHERE param_code='server.websocket';
UPDATE sys_params SET param_value='http://192.168.1.71:8002/xiaozhi/ota/' WHERE param_code='server.ota';
```

---

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

---

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

---

## 当前服务地址
| 服务 | 地址 |
|------|------|
| WebSocket | `ws://192.168.1.71:8000/xiaozhi/v1/` |
| 智控台 | http://192.168.1.71:8002/ |
| OTA 接口 | http://192.168.1.71:8002/xiaozhi/ota/ |
| 视觉接口 | http://192.168.1.71:8003/mcp/vision/explain |
| 本地 FunASR | http://192.168.1.56:15102/v1/audio/transcriptions |
| 本地 LLM | http://192.168.1.56:15000/v1/ (museum-guide-agent) |
