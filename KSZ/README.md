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
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" xiaozhi_esp32_server
```

首次部署前，复制 `.env.example` 为 `.env` 并按需修改 `MYSQL_ROOT_PASSWORD` 与 `SPRING_DATASOURCE_DRUID_PASSWORD`。默认账号和密码仅用于首次本地部署；若数据库已初始化，修改密码后需同步更新 MySQL root 密码及已有连接配置。

若后端启动时报 `Public Key Retrieval is not allowed` 或 SSL 相关错误，在 `.env` 中设置：

```bash
SPRING_DATASOURCE_DRUID_EXTRA_ARGS=allowPublicKeyRetrieval=true&useSSL=false
```

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

`language` 须使用下文「语言码约定」中的规范写法。该接口只写入属性，不切换智能体；切换语言请用 `language_change` 事件（见下文“语言切换”）。验证 LLM 收到的设备上下文：

```bash
docker compose logs -f xiaozhi-esp32-server | grep "发送给LLM的请求"
```

### 常见问题

- `pull access denied for xiaozhi-esp32-server`：确认命令在 `KSZ/` 执行，并使用 `docker compose up -d --build`；本地镜像必须由 `build` 段生成。
- server 报缺少 `TTS` 或设备无法识别语音：重建 web，清 Redis 的 `server:config`，再重启 server。
- 数据库已迁移但 web 报 `Unknown column 'attr_key'`：web 镜像与数据库结构不一致，按“更新代码后的部署”重建 web。
- 容器启动失败或端口无法监听：检查 `8000`、`8002`、`8003`、`${MYSQL_PORT}`、`${REDIS_PORT}` 是否已被宿主机进程占用。

版本变更、迁移背景与已知问题见 [CHANGELOG.md](CHANGELOG.md)（Keep a Changelog，自 `0.1.1` 起按版本记录）。

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
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" xiaozhi_esp32_server -e "
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
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" xiaozhi_esp32_server -e "
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
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" xiaozhi_esp32_server -e "
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
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" xiaozhi_esp32_server -e "
  SELECT u.id, u.username, t.token, t.expire_date
  FROM sys_user_token t
  JOIN sys_user u ON u.id = t.user_id
  WHERE u.username='<USERNAME>';"
```

临时延长指定令牌的有效期：

```bash
docker compose exec xiaozhi-esp32-server-db \
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" xiaozhi_esp32_server -e "
  UPDATE sys_user_token
  SET expire_date=DATE_ADD(NOW(), INTERVAL 12 HOUR)
  WHERE token='<TOKEN>';"
```

### 设备属性迁移

`ai_device_attribute` 已从 `attr_key`/`attr_value` 迁移为每设备一行：`language`、`last_beacon_id`、`agent_name`。相关脚本已登记到 Liquibase 主清单：

- `202607101600.sql`：列模式重建（`language`、`last_beacon_id`）
- `202607311534.sql`：新增 `agent_name` 并按 `ai_device.agent_id` 回填

正常部署由 manager-api 启动时自动执行。若旧库仍为 key-value 结构且需手动迁移，先备份再执行：

```bash
mkdir -p backup
docker compose exec -T xiaozhi-esp32-server-db \
  mysqldump -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" xiaozhi_esp32_server ai_device_attribute \
  > backup/ai_device_attribute-$(date +%F-%H%M%S).sql

docker compose exec -T xiaozhi-esp32-server-db \
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" xiaozhi_esp32_server \
  < ../main/manager-api/src/main/resources/db/changelog/202607101600.sql

docker compose exec -T xiaozhi-esp32-server-db \
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" xiaozhi_esp32_server \
  < ../main/manager-api/src/main/resources/db/changelog/202607311534.sql
```

迁移后重建 web 并检查表结构：

```bash
docker compose up -d --build xiaozhi-esp32-server-web
docker compose exec xiaozhi-esp32-server-db \
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" -D xiaozhi_esp32_server \
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

### 记忆模式选择

本场景下设备端为公用硬件，用户选定智能体（语言）后通常会持续使用同一智能体完成一次完整服务，服务结束后再由后端归档对话记录。因此推荐以下配置：

```yaml
selected_module:
  Memory: nomem
```

理由：

- `nomem` 不在本地保存跨会话记忆，避免不同用户、不同智能体之间的上下文污染。
- 当前会话的 LLM 上下文由 `Dialogue` 自身维护，只要连接未断开，当前智能体的多轮对话即可连续。
- 服务结束后，由后端统一持久化完整对话 JSON，而不是让设备端或服务端维护长期记忆。

#### 聊天记录上报（默认进 MySQL）

当 `read_config_from_api` 启用且智控台开启聊天记录时，server 会在每轮 ASR/TTS 后自动把单条消息上报到 manager-api：

```text
POST /xiaozhi/agent/chat-history/report
```

数据最终落入 `ai_agent_chat_history`（MySQL）。`chat_history_conf` 控制上报粒度：

- `0`：不上报
- `1`：上报文本
- `2`：上报文本 + 音频 WAV

该配置通过智控台或数据库 `ai_agent_template.chat_history_conf` 维护。

#### 完整对话 JSON 归档到 PostgreSQL

如果需要将一整轮服务的完整对话以 JSON 形式写入外部 PG 库（例如 `127.0.0.1:5432`），不建议改动现有逐条上报表，而是扩展一个独立归档流程：

1. 在 `core/connection.py` 的 `_save_and_close` 中，连接关闭前将 `self.dialogue.dialogue` 序列化为 JSON。
2. 使用 `asyncpg` 或同步 `psycopg2` 写入 PG 业务库：

```python
import json
import asyncpg

async def archive_dialogue_to_pg(device_id, session_id, agent_id, dialogue):
    records = [
        {"role": m.role, "content": m.content}
        for m in dialogue
        if m.role in ("user", "assistant", "system")
    ]
    conn = await asyncpg.connect("postgresql://user:pass@127.0.0.1:5432/db")
    await conn.execute(
        """
        INSERT INTO chat_archive (device_id, session_id, agent_id, dialogue_json, created_at)
        VALUES ($1, $2, $3, $4, now())
        """,
        device_id, session_id, agent_id, json.dumps(records, ensure_ascii=False)
    )
    await conn.close()
```

3. PG 表示例：

```sql
CREATE TABLE chat_archive (
    id BIGSERIAL PRIMARY KEY,
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    dialogue_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_chat_archive_session ON chat_archive(session_id);
CREATE INDEX idx_chat_archive_device ON chat_archive(device_id);
```

注意：

- 不要在 `nomem` 模式下启用 `mem_local_short` 或 `mem0ai`，否则记忆会按 `device_id` 共享，导致公用设备上的上下文串扰。
- 切换智能体（`agent_rebind`）会断开连接、清空当前 `Dialogue`，这是预期行为；PG 归档应在断连前完成。
- 如果希望服务端也按智能体维度区分，归档时务必带上 `agent_id`。

### 设备智能体换绑

设备不直连 manager-api。设备经 WebSocket 上报 `device_event` / `agent_rebind`，由 `xiaozhi-server` 使用 `server.secret` 调用：

```text
POST /xiaozhi/device/rebind
Authorization: Bearer <server.secret>
```

设备请求示例：

```json
{
  "type": "device_event",
  "event": "agent_rebind",
  "request_id": "req-001",
  "payload": {
    "current_agent_name": "导游A",
    "target_agent_name": "导游B",
    "confirm": true
  }
}
```

约束：

- `deviceId` 由 server 取当前连接 MAC，忽略设备自报身份。
- `current_agent_name` / `target_agent_name` 在设备所属用户范围内精确匹配 `ai_agent.agent_name`；目标不存在或重名均失败。
- `confirm` 必须为 `true`。
- 事务内按 `device_id + old_agent_id` 条件更新 `ai_device.agent_id`，同步 `ai_device_attribute.agent_name`，并回读三处确认后才返回成功。

成功响应（server → 设备）：

```json
{
  "type": "device_event_result",
  "event": "agent_rebind",
  "request_id": "req-001",
  "success": true,
  "data": {
    "deviceId": "aa:bb:cc:dd:ee:ff",
    "previousAgentName": "导游A",
    "agentName": "导游B",
    "confirmed": true,
    "reconnectRequired": true
  }
}
```

成功消息发送后 server 主动关闭 WebSocket；设备自动重连即可加载新智能体配置，无需关机重启。失败时 `success=false` 且附带 `error`，连接保持。

### 语言码约定

`payload.language` **大小写固定**，禁止 `zh-cn` / `ZH-CN` 等变体；manager-api、内部 HTTP 与 WebSocket 事件均按下表规范码精确校验并原样持久化、透传。

| 规范码 | 智能体名后缀 | 说明 |
|--------|--------------|------|
| `zh-CN` | 汉语 | 普通话 |
| `en` | 英语 | 英语 |
| `ja` | 日语 | 日语 |
| `zh-CN-yue` | 粤语 | 粤语 |
| `zh-CN-sichuan` | 四川话 | 方言示例 |
| `zh-CN-shanghai` | 上海话 | 方言示例 |
| `zh-CN-minnan` | 闽南语 | 方言示例 |
| `zh-CN-shanxi` | 陕西话 | 方言示例 |

规则：

- 语种：`zh-CN` / `en` / `ja`（语言小写或 `zh`，地区大写；与 BCP 47 常见写法一致）。
- 汉语方言：`zh-CN-{pinyin_of_region}`，地区拼音全小写、无声调，如 `zh-CN-sichuan`。
- 粤语固定为 `zh-CN-yue`（不写 `zh-HK` / `yue`）。
- 智能体命名必须为 `<基名>-<后缀>`，例如 `小硕-汉语`、`小硕-英语`、`小硕-粤语`、`小硕-日语`；换绑按后缀推导，同一基名下各语言智能体并存。

服务会根据当前智能体的任一已知语言后缀和目标语言码推导目标智能体，例如 `小硕-粤语` + `ja` → `小硕-日语`。

### 语言切换（智能体切换路线）

KSZ 不走“按 `device_language` 强制翻译”路线（`agent-base-prompt.txt` 的 `output_language_directive` 段已移除），回复语言完全由当前绑定智能体自身的 `base_prompt` 决定。因此**语言切换 = 切换到对应语言的智能体**，复用上面的换绑流程。

外部系统可先调用事件上报接口，服务按当前智能体的命名规则推导目标智能体，并经内部 HTTP 回调向在线设备下发 WS 切换命令：

```json
{
  "deviceId": "{{DEVICE_ID}}",
  "event": "language_change",
  "payload": { "language": "zh-CN" },
  "timestamp": {{$timestamp}}
}
```

HTTP 层恒为 `200`，业务成败看 JSON 的 `code`（`0` 成功，非 `0` 失败）。这是 manager-api 的 `Result` 约定，不是传输异常。

`server.internal_api`（参数字典）**必须**指向 xiaozhi-server 内部 HTTP，例如 Host 网络下 `http://127.0.0.1:8003`；回调使用 `server.secret` 的 Bearer 鉴权。未配置时会出现：语言属性已写入，但 `code=500`，msg 含「下发设备语言切换命令失败」/ `未配置 server.internal_api`。

设备收到以下 `server_command` 后，原样作为 `device_event/language_change` 上报：

```json
{
  "type": "server_command",
  "event": "language_change",
  "payload": {
    "language": "en",
    "current_agent_name": "小硕-汉语",
    "target_agent_name": "小硕-英语"
  }
}
```

设备也可直接通过 WebSocket 上报 `device_event` / `language_change`。`target_agent_name` 缺省时，server 使用同一命名规则推导：

```json
{
  "type": "device_event",
  "event": "language_change",
  "request_id": "lang-001",
  "payload": {
    "language": "en",
    "target_agent_name": "小硕-英语",
    "current_agent_name": "小硕-汉语"
  }
}
```

- 当前智能体名后缀须在「语言码约定」表中，否则无法推导目标智能体。
- `language` 持久化到设备属性；重连后及后续对话会继续作为 `extra_body.language` 发送给下游 LLM。
- 成功后 server 调用 `POST /xiaozhi/device/rebind` 换绑并主动断开，设备重连即加载新语言智能体；响应 `event` 为 `language_change`。
- 失败（缺 `target_agent_name`、智能体不存在/重名、设备离线、未配置 `server.internal_api`、未启用 manager-api 等）时业务 `code≠0` 或 WS `success=false`，连接在 WS 失败路径下保持。

`PUT /xiaozhi/device/attribute/{deviceId}/language` 仍可用，但仅写入 `language` 属性，**不会切换智能体也不会触发翻译**；切换语言请用上面的 `language_change` 事件或直接调用 `/device/rebind`。

### 信标位置语音导览

设备应通过 WebSocket 上报 `device_event` 的 `beacon_change`。信标以 MAC 地址作为 `beacon_mac.beacon_id` 传递，server 会使用该 MAC 查询固定位置接口：

```json
{
  "type": "device_event",
  "event": "beacon_change",
  "beacon_mac": {
    "beacon_id": "DA:01:16:00:08:87",
    "rssi": -65
  },
  "timestamp": 1785223540
}
```

- `beacon_mac.beacon_id`：必填，蓝牙信标 MAC 地址，也是位置服务查询参数。
- `beacon_mac.rssi`：可选，随事件持久化，当前不参与服务端导览触发判断。
- `timestamp`：可选事件时间戳，用于事件记录。

首个有效 MAC 仅建立当前位置上下文；同一设备后续上报不同 MAC 时，才会触发当前位置和附近展品的自动语音导览。原有 `payload.beacon_id` 报文仍兼容，但新接入设备应统一使用 `beacon_mac.beacon_id`。

位置查询接口：

```text
GET http://127.0.0.1:14000/api/v1/beacons/by-beacon-id/{beaconId}
```

接口响应至少应包含匹配的 `beacon_id`，以及 `floor`、`area`、`location_description` 中的一项。例如：

```json
{
  "beacon_id": "jx-pxm-003",
  "floor": "2F",
  "area": "古代文明展厅",
  "location_description": "北墙 A12 展柜旁"
}
```

KSZ Compose 使用 Host 网络，故 `127.0.0.1:14000` 指向宿主机的位置服务。首个信标仅建立当前位置；后续实际变更会自动发起“当前位置与附近展品”的对话，并沿用现有 TTS 输出。用户随后询问当前位置或附近展品时，会使用当前信标位置上下文。

位置服务在 2 秒内未响应、返回 404 或响应无效时，设备属性仍会持久化；自动播报仅提示已进入新区域，不会编造具体位置或展品。每个连接对成功结果缓存 60 秒，失败结果 5 秒后允许重试。

验证指定蓝牙 MAC 的位置接口：

```bash
curl --request GET \
  --url http://127.0.0.1:14000/api/v1/beacons/by-beacon-id/DA%3A01%3A16%3A00%3A08%3A87 \
  --header 'accept: application/json'
```

### 定向语音开发 WebSocket

开发控制端连接 `ws://<SERVER_HOST>:8000/dev/ws` 后，可查询在线设备，并向指定真实设备注入文本；目标设备会沿用原有 LLM、TTS 和 Opus 音频下发流程。

查询在线设备：

```json
{"action":"list_connections"}
```

按稳定的设备 MAC 播报：

```json
{
  "action":"speak",
  "target":{"device_id":"98:88:e0:6c:29:1c"},
  "text":"请介绍你当前附近的展品。"
}
```

按临时 TCP 对端地址播报：

```json
{
  "action":"speak",
  "target":{"peer_address":"113.87.144.31:61157"},
  "text":"请介绍你当前附近的展品。"
}
```

`peer_address` 的端口会在设备重连后变化，应优先使用 `device_id`。`/dev/ws` 不鉴权，仅可在受控内网使用；若服务暴露到公网，任何连接者都可向在线设备发起语音播报。

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
