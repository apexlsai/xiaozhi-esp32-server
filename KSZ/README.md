# KSZ 本地构建部署

本目录是 KSZ 定制开发、部署与运维的唯一入口。`compose.yml` 从项目根目录构建本地 server 和 web 镜像，不依赖不存在的 `xiaozhi-esp32-server:*_local` 远程镜像。上游通用部署请使用项目根目录的 `docker-compose.yml`。

## 前置条件

- 已安装 Docker Engine 和 Docker Compose v2。
- 在项目根目录中保留 `Dockerfile-server`、`Dockerfile-web` 和 `main/` 源码目录。
- 确保 Docker 可以拉取基础镜像，以及 MySQL、Redis 镜像。
- 默认使用远程 OpenAI 兼容 ASR，无需下载或挂载本地 SenseVoice 模型。

## 首次构建并启动

```bash
cd /apps/xiaozhi-esp32-server/KSZ

# 可单独构建指定镜像
docker compose build xiaozhi-esp32-server-web
docker compose build xiaozhi-esp32-server

# 构建并启动全部服务
docker compose up -d --build
```

Compose 会在 `KSZ/` 下创建并使用以下默认持久化目录：

- `data/`：server 配置和运行数据
- `uploadfile/`：智控台上传文件
- `mysql/data/`：MySQL 数据

如需将数据保存到其他位置，可在启动前设置环境变量：

```bash
export DATA_DIR=/srv/xiaozhi/data
export UPLOAD_DIR=/srv/xiaozhi/uploadfile
export MYSQL_DATA_DIR=/srv/xiaozhi/mysql
docker compose up -d --build
```

## 依赖服务端口

Host 网络模式下，web 容器通过宿主机端口连接 MySQL 和 Redis。使用 `KSZ/.env` 配置这两个依赖端口；首次部署默认配置为 MySQL `3307`、Redis `6379`。如需重新生成配置：

```bash
cp .env.example .env
```

修改 `MYSQL_PORT` 或 `REDIS_PORT` 后，需重新创建整套服务，使 MySQL、Redis 和 web 使用同一组端口：

```bash
docker compose down
docker compose up -d --build
```

`8000`、`8002`、`8003` 分别是 server 与 web 的对外服务端口，保持固定，不在 `.env` 中配置。

## 常用操作

```bash
# 查看容器状态和日志
docker compose ps
docker compose logs -f xiaozhi-esp32-server
docker compose logs -f xiaozhi-esp32-server-web

# 仅重建并更新一个服务
docker compose up -d --build xiaozhi-esp32-server-web

# 停止服务（保留数据目录）
docker compose down

# 清除服务端配置缓存后重启 server
docker compose exec xiaozhi-esp32-server-redis redis-cli DEL server:config
docker compose restart xiaozhi-esp32-server
```

## 服务端口

配置采用 Host 网络模式，服务直接占用宿主机端口：

- `8000`：WebSocket 服务
- `8002`：智控台与 OTA 接口
- `8003`：视觉/HTTP 接口
- `${MYSQL_PORT}`：MySQL（默认 `3307`）
- `${REDIS_PORT}`：Redis（默认 `6379`）

启动前请确认这些端口没有被其他进程占用。

## 配置与维护

### 配置模型服务

优先在智控台的【模型配置】中修改 ASR、LLM 或 SLM 地址；保存后清除 Redis 配置缓存并重启 server：

```bash
docker compose exec xiaozhi-esp32-server-redis redis-cli FLUSHALL
docker compose restart xiaozhi-esp32-server
```

也可直接进入数据库：

```bash
docker compose exec xiaozhi-esp32-server-db \
  mysql -uroot -p123456 xiaozhi_esp32_server
```

默认账号和密码仅用于首次本地部署；生产环境应通过环境变量修改 `MYSQL_ROOT_PASSWORD`、`SPRING_DATASOURCE_DRUID_PASSWORD`，并同步更新已有数据库连接配置。

### 更新代码后的部署

修改 `main/manager-api/`、`main/manager-web/` 后：

```bash
docker compose up -d --build xiaozhi-esp32-server-web
docker compose exec xiaozhi-esp32-server-redis redis-cli DEL server:config
docker compose restart xiaozhi-esp32-server
```

修改 `main/xiaozhi-server/` 后：

```bash
docker compose up -d --build xiaozhi-esp32-server
docker compose logs --tail=50 xiaozhi-esp32-server
```

### 验证设备属性接口

```text
PUT /xiaozhi/device/attribute/{deviceId}/language
Body: en

PUT /xiaozhi/device/attribute/{deviceId}/last_beacon_id
Body: beacon-abc-123
```

`language` 仅支持 `en` 或 `zh-cn`。验证 LLM 收到的设备上下文：

```bash
docker compose logs -f xiaozhi-esp32-server | grep "发送给LLM的请求"
```

### 常见问题

- `pull access denied for xiaozhi-esp32-server`：确认命令在 `KSZ/` 执行，并使用 `docker compose up -d --build`；本地镜像必须由 `build` 段生成。
- server 报缺少 `TTS` 或设备无法识别语音：重建 web，清 Redis 的 `server:config`，再重启 server。
- 数据库已迁移但 web 报 `Unknown column 'attr_key'`：web 镜像与数据库结构不一致，按“更新代码后的部署”重建 web。
- 容器启动失败或端口无法监听：检查 `8000`、`8002`、`8003`、`${MYSQL_PORT}`、`${REDIS_PORT}` 是否已被宿主机进程占用。

具体变更背景、数据库迁移和故障根因请参阅 [CHANGELOG.md](CHANGELOG.md)。所有 KSZ 定制开发记录均维护在该文件中。

## 运行时配置

### Server 与智控台连接

`data/.config.yaml` 中的 `manager-api.url` 必须指向智控台 API；`secret` 与数据库 `sys_params` 表的 `server.secret` 保持一致。Host 网络模式下可使用宿主机地址或 `127.0.0.1`：

```yaml
manager-api:
  url: http://127.0.0.1:8002/xiaozhi
  secret: <从 sys_params 的 server.secret 获取>
server:
  port: 8000
  http_port: 8003
```

服务对外地址在 `sys_params` 中维护。将 `<HOST_IP>` 替换为实际 IP 或域名：

```bash
docker compose exec xiaozhi-esp32-server-db \
  mysql -uroot -p123456 xiaozhi_esp32_server -e "
  UPDATE sys_params SET param_value='ws://<HOST_IP>:8000/xiaozhi/v1/'
  WHERE param_code='server.websocket';
  UPDATE sys_params SET param_value='http://<HOST_IP>:8002/xiaozhi/ota/'
  WHERE param_code='server.ota';"
```

修改后清除配置缓存并重启 server。

### 配置 FunASR

推荐通过智控台【模型配置】→【语音识别模型】修改「OpenAI 语音识别」的 `base_url`、`model_name`。例如，转写接口通常为 `http://<FUNASR_HOST>:15102/v1/audio/transcriptions`。

需要直接修改数据库时，执行：

```bash
docker compose exec xiaozhi-esp32-server-db \
  mysql -uroot -p123456 xiaozhi_esp32_server -e "
  UPDATE ai_model_config
  SET config_json='{
    \"type\":\"openai\",
    \"api_key\":\"none\",
    \"base_url\":\"http://<FUNASR_HOST>:15102/v1/audio/transcriptions\",
    \"model_name\":\"fun-asr-nano\",
    \"output_dir\":\"tmp/\"
  }'
  WHERE id='ASR_OpenaiASR';
  UPDATE ai_agent_template SET asr_model_id='ASR_OpenaiASR';"
docker compose exec xiaozhi-esp32-server-redis redis-cli FLUSHALL
docker compose restart xiaozhi-esp32-server
```

### 配置 OpenAI 兼容 LLM / SLM

在智控台中配置优先。直接写数据库时，用实际值替换 `<API_KEY>`、`<LLM_HOST>` 和 `<MODEL_NAME>`；不要将 API Key 提交到 Git：

```bash
docker compose exec xiaozhi-esp32-server-db \
  mysql -uroot -p123456 xiaozhi_esp32_server -e "
  UPDATE ai_model_config
  SET config_json='{
    \"type\":\"openai\",
    \"api_key\":\"<API_KEY>\",
    \"base_url\":\"http://<LLM_HOST>:15000/v1/\",
    \"model_name\":\"<MODEL_NAME>\"
  }'
  WHERE id IN ('LLM_ChatGLMLLM', 'SLM_ChatGLMLLM');"
docker compose exec xiaozhi-esp32-server-redis redis-cli FLUSHALL
docker compose restart xiaozhi-esp32-server
```

测试模型网关连通性：

```bash
curl --request POST http://<LLM_HOST>:15000/v1/chat/completions \
  --header 'Content-Type: application/json' \
  --header "Authorization: Bearer <API_KEY>" \
  --data '{"model":"<MODEL_NAME>","messages":[{"role":"user","content":"连通性测试"}],"stream":false}'
```

## 数据库与设备属性

### 查询与维护用户令牌

查询用户令牌：

```bash
docker compose exec xiaozhi-esp32-server-db \
  mysql -uroot -p123456 xiaozhi_esp32_server -e "
  SELECT u.id, u.username, t.token, t.expire_date
  FROM sys_user_token t
  JOIN sys_user u ON u.id = t.user_id
  WHERE u.username='<USERNAME>';"
```

临时延长指定令牌的有效期：

```bash
docker compose exec xiaozhi-esp32-server-db \
  mysql -uroot -p123456 xiaozhi_esp32_server -e "
  UPDATE sys_user_token
  SET expire_date=DATE_ADD(NOW(), INTERVAL 12 HOUR)
  WHERE token='<TOKEN>';"
```

### 设备属性迁移

`ai_device_attribute` 已从 `attr_key`/`attr_value` 迁移为 `language`、`last_beacon_id` 两列。迁移脚本为 `../main/manager-api/src/main/resources/db/changelog/202607101600.sql`，但尚未登记到 Liquibase 主清单。

该脚本会重建表；仅在备份后、确认数据库仍是旧结构时执行：

```bash
mkdir -p backup
docker compose exec -T xiaozhi-esp32-server-db \
  mysqldump -uroot -p123456 xiaozhi_esp32_server ai_device_attribute \
  > backup/ai_device_attribute-$(date +%F-%H%M%S).sql

docker compose exec -T xiaozhi-esp32-server-db \
  mysql -uroot -p123456 xiaozhi_esp32_server \
  < ../main/manager-api/src/main/resources/db/changelog/202607101600.sql
```

迁移后重建 web 并检查表结构：

```bash
docker compose up -d --build xiaozhi-esp32-server-web
docker compose exec xiaozhi-esp32-server-db \
  mysql -uroot -p123456 -D xiaozhi_esp32_server \
  -e "DESC ai_device_attribute;"
```

### 设备属性 API

```text
GET /xiaozhi/device/attribute/{deviceId}/entity
PUT /xiaozhi/device/attribute/{deviceId}/language
Body: en
PUT /xiaozhi/device/attribute/{deviceId}/last_beacon_id
Body: beacon-abc-123
```

兼容的通用接口仍可用：`GET /xiaozhi/device/attribute/{deviceId}` 与 `PUT /xiaozhi/device/attribute/{deviceId}/{attrKey}`。

## MCP 配置

外部 MCP 服务配置文件为 `data/.mcp_server_settings.json`；示例见 `../main/xiaozhi-server/mcp_server_settings.json`。支持 `stdio`、`sse`、`streamable-http` 三种传输方式。修改后重启 server：

```bash
docker compose restart xiaozhi-esp32-server
```

项目参考文档：

- [MCP 接入点部署](../docs/mcp-endpoint-enable.md)
- [MCP 接入点使用](../docs/mcp-endpoint-integration.md)
- [视觉模型 MCP 集成](../docs/mcp-vision-integration.md)
- [通过 MCP 获取设备信息](../docs/mcp-get-device-info.md)
