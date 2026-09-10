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

Bridge 网络模式下，MySQL、Redis 仅在 Compose 内网暴露，不占用宿主机自定义端口。信标位置服务在宿主机运行时，server 容器经 `host.docker.internal` 访问，可在 `KSZ/.env` 中配置 `BEACON_LOCATION_API`：

```bash
cp .env.example .env
```

`8000`、`8002`、`8003` 分别是 server WebSocket、智控台、server HTTP 端口，与上游全模块 Docker 一致。

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
- `8002`：智控台（Nginx）与 OTA 接口
- `8003`：manager-api Java（仅本机，由 `8002` 反代，勿当作 server HTTP）
- `8004`：xiaozhi-server HTTP / 视觉与内部回调（`server.internal_api`）
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
- 容器启动失败或端口无法监听：检查 `8000`、`8002`、`8003`、`8004`、`${MYSQL_PORT}`、`${REDIS_PORT}` 是否已被宿主机进程占用。
- `curl :8004/mcp/vision/explain` 报「MCP Vision 接口运行不正常」：`data/.config.yaml` 缺 `server.vision_explain` 或仍为上游默认 `8003`；见下文「Server 与智控台连接」。

版本变更、迁移背景与已知问题见 [CHANGELOG.md](CHANGELOG.md)（Keep a Changelog，自 `0.1.1` 起按版本记录）。

## 运行时配置

### Server 与智控台连接

全模块部署时，`data/.config.yaml` 只需保留连接智控台与 Host 网络端口相关项；模型密钥等在智控台【模型配置】维护。`secret` 与 `sys_params` 表中的 `server.secret` 保持一致。

Host 网络下 **不要沿用上游默认 `http_port: 8003`**：Java manager-api 已占用宿主机 `8003`，xiaozhi-server HTTP（视觉、内部回调）须用 **`8004`**。

推荐 `data/.config.yaml` 模板（将 `<HOST_IP>` 换为设备可达的局域网 IP 或公网 IP/域名）：

```yaml
server:
  ip: 0.0.0.0
  port: 8000
  http_port: 8004
  # 必填：GET /mcp/vision/explain 健康检查及设备下发均依赖此项
  # 内网可先留占位符，启动时自动探测本机 IP + http_port
  vision_explain: http://<HOST_IP>:8004/mcp/vision/explain

manager-api:
  url: http://127.0.0.1:8002/xiaozhi
  secret: <从 sys_params 的 server.secret 获取>

tool_feedback:
  enabled: true
  delay_ms: 500
  tools:
    self_camera_take_photo: "我看看。"

prompt_template: agent-base-prompt.txt
```

`tool_feedback` 在设备 MCP 工具执行超过设定时间时播放临时提示；工具快速完成、失败、超时、会话中断或连接关闭时取消尚未播放的提示。提示不经过 Agent，也不写入对话历史。

配置职责（避免与上游 bridge 默认混淆）：

| 配置项 | 写入位置 | KSZ 端口 / 地址 |
|--------|----------|-----------------|
| 连接智控台 | `.config.yaml` → `manager-api` | `http://127.0.0.1:8002/xiaozhi` |
| WebSocket（设备） | `sys_params.server.websocket` | `ws://<HOST_IP>:8000/xiaozhi/v1/` |
| OTA（设备） | `sys_params.server.ota` | `http://<HOST_IP>:8002/xiaozhi/ota/` |
| 视觉 HTTP | `.config.yaml` + `sys_params.server.vision_explain` | `http://<HOST_IP>:8004/mcp/vision/explain` |
| 内部回调 | `sys_params.server.internal_api` | `http://127.0.0.1:8004`（勿填 `8003`） |
| manager-api Java | 无需配置 | 宿主机 `8003`，由 `8002` 反代 |

> 全模块模式下，`.config.yaml` 的 `server` 段会覆盖 API 返回值。若未写 `vision_explain`，即使智控台已填 `server.vision_explain`，GET 检查仍会报「运行不正常」。

设备与对外地址写入 `sys_params`：

```bash
docker compose exec xiaozhi-esp32-server-db \
  mysql -uroot -p"${MYSQL_ROOT_PASSWORD:-123456}" xiaozhi_esp32_server -e "
  UPDATE sys_params SET param_value='ws://<HOST_IP>:8000/xiaozhi/v1/'
  WHERE param_code='server.websocket';
  UPDATE sys_params SET param_value='http://<HOST_IP>:8002/xiaozhi/ota/'
  WHERE param_code='server.ota';
  UPDATE sys_params SET param_value='8004'
  WHERE param_code='server.http_port';
  UPDATE sys_params SET param_value='http://127.0.0.1:8004'
  WHERE param_code='server.internal_api';
  UPDATE sys_params SET param_value='http://<HOST_IP>:8004/mcp/vision/explain'
  WHERE param_code='server.vision_explain';"
```

修改后清除配置缓存并重启 server：

```bash
docker compose exec xiaozhi-esp32-server-redis redis-cli DEL server:config
docker compose restart xiaozhi-esp32-server
```

验证视觉接口（公网部署须放行安全组/防火墙 **8004**）：

```bash
curl http://<HOST_IP>:8004/mcp/vision/explain
# 正常：MCP Vision 接口运行正常，视觉解释接口地址是：http://...
```

视觉模型密钥与智能体绑定见下文「视觉模型（VLLM）」；上游 [`mcp-vision-integration.md`](../docs/mcp-vision-integration.md) 中的 **`8003` 端口不适用 KSZ Host 部署**，一律改为 **`8004`**。

### 视觉模型（VLLM）

地址与端口按上文「Server 与智控台连接」配好 `vision_explain` 并通过 `curl` 健康检查后再启用识图。

Museum Guide Agent 接入使用智控台现有的 OpenAI VLLM，无需新增 Provider 或数据库迁移：

1. 智控台【模型配置】→【视觉大语言模型】新增模型，接口类型选择 `OpenAI接口`。
2. `base_url` 填写 `https://<MUSEUM_AGENT_HOST>/v1`，`model_name` 填写 `museum-guide-vision`，`api_key` 填写 Museum Agent 当前有效密钥。
3. 在目标智能体【配置角色】中，将「视觉大语言模型(VLLM)」选为该模型并保存。
4. 清除 Redis 配置缓存并重启 server（见上文命令）。
5. 设备固件 ≥ 1.6.6，唤醒后说「请打开摄像头，说你看到了什么」，并查看 server 日志是否有 VLLM 报错。

`server.vision_explain` 始终填写本 xiaozhi-server 的 `/mcp/vision/explain`，不能填写 Museum Agent 地址。小智会把图片、问题、`device_id`、规范语言码和最近 Beacon ID 转发给 `museum-guide-vision`，Museum Agent 返回最终可播报文本。

KSZ 保留上游完整返回协议：MCP `initialize` 继续下发 `vision.url` 与 `vision.token`，官方固件通过 `file` 字段上传，服务端返回 `success`、`action` 与 `response`；同时兼容旧客户端的 `image` 字段。旧端无需修改即可继续使用完整返回，启用逐句播报需要下述可选适配。

兼容模式的手动拍照由 KSZ 向原在线连接直推播报，顺序为 `tts:start → sentence_start → 音频帧 → tts:stop`。服务端先完成启动通知并设置播放状态，再提交语音；识别或启动期间换轮、打断、断线，或者出现待处理 MCP 调用时不追加旧结果。MCP 拍照仍沿用已有工具播报链路，不重复直推。此修复无需修改固件上传接口，也不要求启用视觉流式能力。

若“测试网页有声、固件按键无声”，先核对固件收到音频前是否有 `tts:start`；旧版 KSZ 手动直推缺少此通知，应重建并更新 server 后验证。HTTP `success=true` 仅说明识别成功，不保证音频已经送达设备；可使用测试器的严格播放模式检查启停顺序，再进行实机联调。

生产环境必须使用内网或 HTTPS 连接 Museum Agent。不要通过公网明文 HTTP 传输游客图片和 Bearer Token；曾出现在命令、日志或聊天记录中的密钥应立即轮换。

#### 视觉流式播报与客户端适配

流式链路为「图片 → VLM 正文增量 → 按句 TTS → 对应音频与字幕」，不再交给聊天 LLM 改写。服务端按 `。！？!?；;`、换行和英文句末句点分句，保留小数点，流结束时提交最后一段。字幕随对应音频发送，不按 HTTP 完整回答一次刷屏。

这不是 `.env` 中的 `stream` 开关。OpenAI 兼容 VLLM 流式请求使用 `stream:true`，目标 `museum-guide-vision` 或其他视觉服务必须支持 SSE 正文增量；如果目标服务仍缓存完整回答才输出，小智侧无法提前取得首句。首句延迟需在实际模型、网络与设备上验证。

仓库数字人页面已支持下列协议，并在摄像头预览增加「拍照识别」按钮。外部测试器、固件需同步适配后才能启用流式：

1. WebSocket `hello.features` 增加 `"vision_stream":true`，保留既有 `mcp` 等能力。
2. MCP 相机调用读取 `payload.params._meta.vision_request_id`；上传到 `vision.url` 时，将该值作为 multipart `request_id`，并设置 `delivery_mode=mcp`。原有 `question`、`file`/`image`、`Device-Id`、`Client-Id` 和 Bearer Token 保持不变。
3. 手动拍照每次生成新的 UUID，上传 `request_id` 和 `delivery_mode=push`。先发起新 HTTP 请求，由服务端取消旧识别；不要只中止旧上传而不发送新请求。
4. 上传请求最终仍返回 JSON，设备无需解析 SSE。保留返回的 `request_id` 与 `delivery`，MCP 调用仍按原 JSON-RPC `id` 回传结果；不能把完整 `response` 再送聊天 LLM 或本地 TTS。字幕、音频从现有设备 WebSocket 接收。

| `delivery_mode` | 用途 | 结果交付 |
| --- | --- | --- |
| `mcp` | 已关联的 MCP 拍照调用 | HTTP 未完成时即可逐句播报，最终 MCP 回执由服务端登记去重 |
| `push` | 手动拍照，使用新的请求 UUID | 向在线设备逐句推送字幕与音频 |
| `return` | 仅取完整结果 | 只返回 HTTP JSON，不推送 TTS |

`delivery:"streamed"` 表示结果已由服务端接管播报，失败提示也可能使用此标记；`delivery:"cancelled"` 表示请求已取消，回传对应 MCP 回执后静默结束。未声明能力、未携带新增参数的旧端继续走原完整返回路径；成功纯文本、顶层 JSON 与嵌套 `vision_analysis` 都可兼容。不要把整张图片的 Base64 再塞进 MCP 回执。

带请求编号的新链路中，新照片替换同设备旧照片；打断、断线会取消旧识别，迟到正文和回执不能恢复旧播报。视觉请求与 MCP 工具调用共用从请求创建起算的 **120 秒**截止时间，外层线程另留 2 秒用于取消清理，不增加模型生成预算。SSE 连续无数据仍有 30 秒读取超时，不等于整轮只能运行 30 秒。

首句前服务异常统一提示「视觉服务暂时不可用，请稍后再试。」；已有正文下发时仅补一次「识别中断了，请再拍一次。」。取消时不播失败提示，不自动重试 VLM；原始错误仅写服务端日志。

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
    \"model_name\":\"<MODEL_NAME>\",
    \"enable_thinking\":false
  }'
  WHERE id IN ('LLM_ChatGLMLLM', 'SLM_ChatGLMLLM');"
docker compose exec xiaozhi-esp32-server-redis redis-cli FLUSHALL
docker compose restart xiaozhi-esp32-server
```

智控台 OpenAI 模型配置提供“允许思考模式”开关，默认关闭。关闭时，Museum Guide Agent 请求会携带顶层 `enable_thinking: false`；阿里、DeepSeek、智谱、Moonshot 和火山等已知兼容服务使用各自的禁用参数；未知 OpenAI 兼容服务不附加厂商字段，避免 HTTP 400。打开开关仅取消 Xiaozhi 的禁用参数，由模型服务采用自身默认行为。

Xiaozhi 默认不再向真实会话注入“讲故事/拜拜”工具调用示例。只有兼容极小旧模型时，才在 `KSZ/data/.config.yaml` 中显式开启：

```yaml
tool_call_fewshot_enabled: true
```

`direct_answer` 虚拟工具仍然保留，用于降低支持函数调用的小模型误触发真实工具的概率。

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
- `202608131200.sql`：将旧 `*-test` 及 `VARCHAR(16)` 截断值清理为基础规范语言码

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
| `ko` | 韩语 | 韩语 |
| `zh-CN-yue` | 粤语 | 粤语 |
| `zh-CN-sichuan` | 四川话 | 方言示例 |
| `zh-CN-shanghai` | 上海话 | 方言示例 |
| `zh-CN-minnan` | 闽南语 | 方言示例 |
| `zh-CN-shanxi` | 陕西话 | 方言示例 |

规则：

- `language` 始终使用表中的规范码，不允许追加 `-test`；测试智能体由同级布尔参数 `dev` 选择。
- `dev=true` 切换到 `<基名>-<语言后缀>-测试`，缺省或 `false` 切换到生产智能体。例如 `language=zh-CN-yue, dev=true` 对应 `小硕-粤语-测试`。
- 语种：`zh-CN` / `en` / `ja` / `ko`（语言小写或 `zh`，地区大写；与 BCP 47 常见写法一致）。
- 汉语方言：`zh-CN-{pinyin_of_region}`，地区拼音全小写、无声调，如 `zh-CN-sichuan`。
- 粤语固定为 `zh-CN-yue`（不写 `zh-HK` / `yue`）。
- 智能体命名必须为 `<基名>-<后缀>`，例如 `小硕-汉语`、`小硕-英语`、`小硕-粤语`、`小硕-日语`、`小硕-韩语`；换绑按后缀推导，同一基名下各语言智能体并存。

服务会根据当前智能体的任一已知语言后缀和目标语言码推导目标智能体，例如 `小硕-粤语` + `ja` → `小硕-日语`。
生产与测试智能体可双向切换，例如 `小硕-粤语-测试` + `language=en, dev=true` → `小硕-英语-测试`，`小硕-粤语-测试` + `language=en, dev=false` → `小硕-英语`。

### 语言切换（智能体切换路线）

KSZ 不走“按 `device_language` 强制翻译”路线（`agent-base-prompt.txt` 的 `output_language_directive` 段已移除），回复语言完全由当前绑定智能体自身的 `base_prompt` 决定。因此**语言切换 = 切换到对应语言的智能体**，复用上面的换绑流程。

设备希望首次 WebSocket 连接即使用目标智能体时，可把原有事件结构直接作为 OTA 请求体发送到 `POST /xiaozhi/ota/`。manager-api 会在生成 WebSocket 地址和 JWT 前完成换绑；此时尚未建立 WebSocket，因此无需先连接再断开：

```json
{
  "deviceId": "{{DEVICE_ID}}",
  "event": "language_change",
  "payload": { "language": "zh-CN", "dev": true },
  "timestamp": {{$timestamp}}
}
```

`Device-Id` 请求头仍为设备身份的权威值；请求体携带 `deviceId` 时必须与该请求头一致。OTA 的普通固件字段可与上述字段并存。重复提交同一目标是幂等的。若目标智能体不存在、重名或参数错误，OTA 响应包含 `error` 且不返回 WebSocket 配置，设备应修正配置后重试。

外部系统调用事件上报接口后，manager-api 先校验语言与目标智能体，再回调 xiaozhi-server 的 `/internal/device/language-change-v2`；server 调用 `/device/rebind`，在同一事务内完成智能体换绑及基础 `language` 持久化，随后断开设备连接。v2 端点用于在滚动升级时拒绝旧 server，避免 `dev` 被忽略后误绑生产智能体；旧内部端点仅保留用于兼容旧 manager-api，并会在新 server 内把历史 `*-test` 入参规范化为基础语言码。滚动发布须先升级 manager-api、再升级 xiaozhi-server：中间阶段 v2 回调会安全失败且不改库，待 server 升级后恢复：

```json
{
  "deviceId": "{{DEVICE_ID}}",
  "event": "language_change",
  "payload": { "language": "zh-CN-yue", "dev": true },
  "timestamp": {{$timestamp}}
}
```

HTTP 层恒为 `200`，业务成败看 JSON 的 `code`（`0` 成功，非 `0` 失败）。这是 manager-api 的 `Result` 约定，不是传输异常。`code=0` 表示语言已保存且换绑已在 server 侧执行（设备须在线）。

`server.internal_api`（参数字典）**必须**指向 xiaozhi-server 内部 HTTP，Host 网络 Docker 部署下为 `http://127.0.0.1:8004`（不要填 `8003`，那是 manager-api Java）；回调使用 `server.secret` 的 Bearer 鉴权。未配置或换绑失败时返回非零业务码，语言属性与设备绑定均保持原值。

设备也可直接通过 WebSocket 上报 `device_event` / `language_change`，效果相同。目标智能体始终由 `language` 与 `dev` 推导，设备传入的 `target_agent_name` 不会覆盖该规则：

```json
{
  "type": "device_event",
  "event": "language_change",
  "request_id": "lang-001",
  "payload": {
    "language": "zh-CN-yue",
    "dev": true,
    "current_agent_name": "小硕-英语"
  }
}
```

- 当前智能体名后缀须在「语言码约定」表中，否则无法推导目标智能体（例如 `小硕-英语` + `zh-CN-yue` → `小硕-粤语`）。
- `language` 持久化到设备属性；无论 `dev` 取值如何，数据库与下游 `extra_body.language` 都只使用原规范码，不写入 `-test`。
- 成功后 server 调用 `POST /xiaozhi/device/rebind` 换绑并主动断开，设备重连即加载新语言智能体。
- 失败（无法推导目标、智能体不存在/重名、设备离线、未配置 `server.internal_api`、未启用 manager-api 等）时业务 `code≠0` 或 WS `success=false`。
- manager-api 会在回调前校验同用户下的目标智能体。若上报 `language=zh-CN-yue, dev=true` 但 `小硕-粤语-测试` 不存在，返回 `code=10253`、`msg=需要名为小硕-粤语-测试的智能体`，语言属性和设备绑定保持不变。

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

### 多段表情与 MiMo 动态语气

普通短回答仍只使用一个句首 Emoji；较长回答允许在新情绪句段开头使用最多三个白名单 Emoji。服务端会移除朗读文本中的 Emoji，并在对应音频分句开始前向设备发送表情消息。双流式或不支持情绪上下文的 TTS 保持原合成参数，表情消息采用兼容回退路径。

MiMo 可把页面下发的基础风格与情绪描述组合为 user message，无需额外调用 LLM：

```yaml
TTS:
  MimoTTS:
    type: mimo
    style: "声音干练、清晰、专业，具有博物馆资深导览员的自信与从容。"
    emotion_style_enabled: true
    emotion_styles:
      joy: "声音轻快，带明显笑意，节奏活泼。"
      sad: "声音温和低沉，语速稍缓，避免夸张哭腔。"
      "😆": "笑意更明显，节奏更活泼。"
```

`emotion_styles` 支持两级覆盖：具体 Emoji 优先于情绪类别，未配置时回退内置描述。可用类别为 `neutral`、`warm`、`joy`、`sad`、`sleepy`、`angry`、`surprise`、`thinking`、`confident`。

以 `😆` 片段为例，MiMo 最终收到：

```json
[
  {"role":"user","content":"声音干练、清晰、专业，具有博物馆资深导览员的自信与从容。 当前片段的表达方式：笑意更明显，节奏更活泼。"},
  {"role":"assistant","content":"真的假的啦，这件事也太好笑了。"}
]
```

代码中的 `emotion_style_enabled` 默认值为关闭；管理后台迁移后的 MiMo 默认模型会显式开启。只有声明支持描述词的 Provider 才会消费动态情绪上下文，其他 TTS 会忽略该能力并继续使用原接口。

### 阿里百炼 Workspace TTS

智控台【模型配置】→【语音合成】→【阿里百炼（流式）】提供 `ws_url`。旧公有地址无需修改；业务空间按地域填写完整地址，其中 `<WorkspaceId>` 替换为真实业务空间 ID：

```text
wss://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference
wss://<WorkspaceId>.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/inference
```

该配置不读取 `.env`。保存后清除 Redis 配置缓存并重启 server。为避免 API Key 泄露，Provider 仅接受阿里云官方域名、`wss` 协议和 `/api-ws/v1/inference` 路径。

百炼合成任务在实际文本到达后才启动。「我看看。」使用独立短任务，文本发完即结束合成，视觉正文回来后再开新任务；两者仍属于同一设备对话轮次，提示结束不会提前发送整轮 `tts stop`。这可避免等待识图时把提示语任务空挂至供应商超时。

流式视觉正文也按完整句子分别结束百炼任务，句间等待与等待 MCP 回执时不保留活动合成任务；只有整轮 `LAST` 才触发设备停止。普通聊天的增量文本仍使用原连续合成路径。

若百炼返回 `task-failed`、1011 或连接关闭，服务端会使旧任务与连接失效，防止后续正文继续写入已关闭的连接。正文合成失败不自动重播已发送内容。此修改仅针对百炼任务生命周期，其他 TTS Provider 沿用各自接口。

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

## MQTT 首期接入

可选 `mqtt` profile 将设备的 MQTT 控制消息和 UDP 音频桥接到现有 xiaozhi-server WebSocket。首期覆盖设备唤醒并建立会话后的语音、字幕表情、打断、语言换绑、信标导览、MCP 拍照、视觉流式及按 MAC 定向播报；不提供休眠自动唤醒、离线事件补发和设备互通电话功能。

固件须支持上游 MQTT+UDP 应用协议 v3、QoS 0，并能在 MQTT 通道发送既有 KSZ 事件。语言换绑后需处理 `goodbye`，重新发送 `hello` 建立会话。视觉流式还需实现本文的 `vision_stream`、`_meta.vision_request_id` 与 HTTP 上传约定；未适配的旧端继续使用原视觉返回方式。

### 构建与启动

在 `KSZ/.env` 中补齐 `.env.example` 的 MQTT 配置：

| 环境变量 | 填写要求 |
| --- | --- |
| `MQTT_PUBLIC_HOST` | 设备可访问的服务器 IPv4 或域名，不带协议和端口 |
| `MQTT_PORT` | MQTT TCP 端口，默认 `1883` |
| `MQTT_UDP_PORT` | 音频 UDP 端口，默认 `8884` |
| `MQTT_API_PORT` | 管理 API 宿主机端口，默认 `8007`，只监听宿主机回环地址 |
| `MQTT_SIGNATURE_KEY` | 与智控台 `server.mqtt_signature_key` 一致 |
| `MQTT_SERVER_SECRET` | 与智控台 `server.secret` 一致 |
| `MQTT_CHAT_SERVER` | 默认 `ws://host.docker.internal:8000/xiaozhi/v1/?from=mqtt_gateway` |

签名密钥至少 8 位，包含大小写字母，不能包含上游禁止的弱密码片段 `test`、`1234`、`admin`、`password`、`qwerty`、`xiaozhi`。两种密钥各有用途，不要互相替代。配置缺失时网关会在监听端口前退出；普通 Compose 启动不要求配置 MQTT。

先确保现有 server/web 已正常运行，再启动网关：

```bash
cd /apps/xiaozhi-esp32-server/KSZ
docker compose --profile mqtt build xiaozhi-mqtt-gateway
docker compose --profile mqtt up -d xiaozhi-mqtt-gateway
docker compose --profile mqtt ps xiaozhi-mqtt-gateway
docker compose --profile mqtt logs --tail=100 xiaozhi-mqtt-gateway
```

网关独立使用 Bridge 网络，其余服务继续使用 Host 网络。网关通过 Docker 的 `host-gateway` 访问宿主机 `8000`；宿主机防火墙须允许该容器访问服务端。设备网络须可达 MQTT TCP 和音频 UDP 端口；仅 HTTP 反向代理不能承载这两个入口。管理 API 映射为 `127.0.0.1:<MQTT_API_PORT>`，供 Host 网络的 manager-api 访问。

镜像固定使用上游 [xiaozhi-mqtt-gateway](https://github.com/xinnan-tech/xiaozhi-mqtt-gateway/tree/93f026ad3a9a5572c9cf6ce651dd8fcf46fd2419) 提交 `93f026ad3a9a5572c9cf6ce651dd8fcf46fd2419`，校验源码归档 SHA256，通过 `npm ci` 使用提交的依赖锁。`qs` 使用兼容修复版本覆盖。构建文件、依赖及补丁均位于 `mqtt/`；升级网关时须同步更新提交、校验和、补丁及锁文件，并重新运行协议测试。

若本机 Docker Hub 镜像代理不可用，可通过构建参数指定可访问的 Node 镜像仓库，例如：

```bash
docker compose --profile mqtt build \
  --build-arg NODE_REGISTRY=public.ecr.aws/docker/library xiaozhi-mqtt-gateway
```

### 智控台参数与 OTA

`.env` 只配置网关，不会自动写入数据库。通过智控台【参数管理】设置：

| 系统参数 | 值 |
| --- | --- |
| `server.mqtt_gateway` | `<MQTT_PUBLIC_HOST>:<MQTT_PORT>` |
| `server.mqtt_signature_key` | 与网关 `MQTT_SIGNATURE_KEY` 完全一致 |
| `server.udp_gateway` | `<MQTT_PUBLIC_HOST>:<MQTT_UDP_PORT>` |
| `server.mqtt_manager_api` | `127.0.0.1:<MQTT_API_PORT>` |

保留 `server.websocket` 和 `server.ota`；`server.internal_api` 仍为 KSZ 的 `http://127.0.0.1:8004`。视觉 `vision.url` 必须可被设备访问，不能使用网关内部地址 `host.docker.internal`；图片仍直接上传 HTTP 视觉接口，音频和字幕经网关返回。

这些 OTA 参数是全局配置，首期不提供按设备灰度。应先在测试环境启用，记录原参数值。保存后清理配置缓存、重启 server，再让测试设备重新获取 OTA。下面清缓存命令适用于默认 Redis 端口 `6379`；如果 `.env` 配置了 `REDIS_PORT=6380`，需使用 `redis-cli -p 6380 DEL server:config`。Compose 会读取 `.env`，直接执行的 `redis-cli` 不会自动读取其中的端口：

```bash
docker compose exec xiaozhi-esp32-server-redis redis-cli DEL server:config
docker compose restart xiaozhi-esp32-server
```

可用 Node.js 22 执行 OTA 检查，指定专用测试 MAC：

```bash
node mqtt/verify-ota.cjs http://<HOST_IP>:8002/xiaozhi/ota/ aa:bb:cc:dd:ee:ff
```

该命令会提交真实 OTA 请求，可能更新测试设备连接信息或产生激活码；不会切换语言、连接 MQTT 或播放语音。检查设备身份、主题、凭据及 WebSocket 回退地址，只输出检查结果，不输出密码。若进程环境提供 `MQTT_SIGNATURE_KEY`，还会校验 OTA 密码签名；脚本不自动读取 `.env`。

### 兼容行为与验收

`mqtt/ksz.patch` 让 MCP 初始化、工具发现和调用直接经过 KSZ 会话，避免网关提前缓存尚未配置视觉能力的工具列表；保留服务端下发的设备专属 `vision.url/token`。补丁还限定使用现有 OTA 生成的签名身份，认证完成才返回成功，并在 MQTT 断开时关闭后端会话。重新建立会话后，设备需重新发送 UDP 数据建立音频回程地址。

每次设备发送 `listen stop` 时，网关日志输出 `上行音频统计`。`收到` 是本轮停止前到达网关并转发的帧数，`序列跨度` 是设备 UDP 序列号前进量，`缺失` 是跨度内未到达的帧数。60 ms 音频一秒约 16.7 帧；若设备端已发送帧数明显更高，且网关 `缺失` 大于零，丢包发生在设备至网关的公网 UDP 路径。

仅保持 MQTT 连接时，不代表存在 KSZ 活动会话。没有会话时信标事件不转发、定向播报不可用；首期不缓存这些事件。按 MAC 定位设备，服务端显示的 TCP 对端地址属于网关。

构建后运行隔离协议测试，无需模型、数据库或真实设备，不发布宿主机端口：

```bash
docker run --rm --network none \
  --mount type=bind,source="$PWD/mqtt/tests",target=/app/tests,readonly \
  --entrypoint node xiaozhi-esp32-server:mqtt_local \
  --test tests/gateway.test.cjs
```

该测试使用真实网关进程、模拟设备和模拟后端，验证鉴权、MCP 视觉能力与工具列表、事件回执、双向 UDP 音频、会话重建和断线清理。健康检查只确认 MQTT/管理 API 进程可用，不证明设备端 UDP 或模型链路可用。

真机上线前须完成以下验收，不能用模拟测试代替：

- 普通对话、连续播报、字幕表情同步、语音打断；拔网线或重启网关后恢复。
- 信标变化后属性落库、位置上下文进入 Agent，并只播报一次。
- 切换语言及 `dev` 测试智能体后收到结果和 `goodbye`，重新 `hello` 后使用新智能体；目标不存在时保留原绑定。
- MCP 拍照及手动上传均正常识别、流式播报；重复拍照、打断、断线后的旧结果不再播放。
- 活动会话中按 MAC 定向播报；未唤醒时不误报可播报；UDP 被阻断时不以 MQTT 连接成功判断语音成功。
- 原 WebSocket 设备连接和 OTA 连接前语言切换仍正常。

回退时先恢复原 MQTT 系统参数（原本未配置则恢复字符串 `null`，包括管理 API 参数），清除 `server:config` 缓存并重启 server，让设备重新获取 OTA 并确认改回 WebSocket，再停止网关：

```bash
docker compose --profile mqtt stop xiaozhi-mqtt-gateway
```

如果固件持久化 MQTT 配置，需按固件流程清除旧配置或切换协议；仅停止网关不构成设备侧回退。

## MCP 配置

外部 MCP 服务配置文件为 `data/.mcp_server_settings.json`；示例见 `../main/xiaozhi-server/mcp_server_settings.json`。支持 `stdio`、`sse`、`streamable-http` 三种传输方式。修改后重启 server：

```bash
docker compose restart xiaozhi-esp32-server
```

项目参考文档：

- [MCP 接入点部署](../docs/mcp-endpoint-enable.md)
- [MCP 接入点使用](../docs/mcp-endpoint-integration.md)
- [视觉模型 MCP 集成](../docs/mcp-vision-integration.md)（KSZ Host 部署请将文档中 **8003** 改为 **8004**）
- [通过 MCP 获取设备信息](../docs/mcp-get-device-info.md)
