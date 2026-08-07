# 更新日志

本文件仅记录 KSZ Fork 相对上游 `xiaozhi-esp32-server` 的版本变更。部署、配置、接口调用及排障命令统一见 [README.md](README.md)。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。自 `0.1.1` 起按版本记录，不标注日期；未发布变更写入 `[Unreleased]`，发版时归入对应版本节。

## [Unreleased]

## [0.1.2]

### 新增

- 设备智能体换绑：设备经 WebSocket `device_event` / `agent_rebind` 由 xiaozhi-server 以 `server.secret` 调用 `POST /device/rebind`；按用户范围内精确 `agent_name` 定位，事务内核对旧绑定、条件更新、回读确认后返回成功，并主动断开连接促使设备重连加载新智能体。
- `ai_device_attribute` 增加冗余字段 `agent_name`（权威绑定仍为 `ai_device.agent_id`）；属性首次创建、换绑与智能体改名时同步。
- 接入小米 MiMo TTS（`mimo-v2.5-tts`）：新增 `core/providers/tts/mimo.py`，采用 chat/completions 形式合成、`api-key` 鉴权、返回 base64 音频；provider 解码为 bytes 后复用 base 类切帧流式发送，`config.yaml` 增 `MimoTTS` 示例块。
- `language_change` 事件上报可通过 `server.internal_api` 回调 xiaozhi-server，为在线设备下发 WS 切换命令；按 `<名称>-汉语` / `<名称>-英语` 推导目标智能体，设备回传后复用换绑流程。

### 变更

- 将 `202607101600.sql` 与 `202607311534.sql` 登记到 Liquibase 主清单，自动完成设备属性列模式与 `agent_name` 迁移。
- 移除 `agent-base-prompt.txt` 的 `output_language_directive` 强制翻译段；回复语言改由各智能体自身 `base_prompt` 决定，不再按 `device_language` 强制翻译。
- `language_change` 事件改为触发智能体换绑：目标智能体可由 `<名称>-汉语` / `<名称>-英语` 自动推导，复用 `agent_rebind` 流程换绑并断开重连；`device_language` 仅作元信息保留，不再用于翻译。
- `language_change` 统一使用大小写敏感的规范语言码，支持 `zh-CN`、`en`、`ja`、`ko`、`zh-CN-yue` 及四川话、上海话、闽南语、陕西话方言码；按对应智能体后缀自动推导换绑目标，并将原码持久化后通过 `extra_body.language` 透传。
- `extra_body.language` 优先用设备属性规范码；缺失或非规范时按智能体名后缀反查（如 `小硕-日语` → `ja`）。
- 补偿写入 `server.internal_api`：旧迁移误用 `id=107`（与 `server.ota` 冲突）导致参数缺失；新迁移按 `param_code` 幂等插入，默认 `http://127.0.0.1:8004`（避开 web Java 占用的 `8003`）。
- `POST /device/event/report` 的 `language_change` 经 `server.internal_api` 回调后，由 xiaozhi-server **直接换绑并断开重连**，不再等待设备回传 `server_command`。

### 已知问题

- 同用户下若存在重名智能体，换绑会拒绝；需保证 `agent_name` 在用户范围内唯一。

## [0.1.1]

汇总预发布阶段 `ksz/v0.1.1dev1`（国内镜像与部署底座）、`ksz/v0.1.1dev2`（本地构建 Compose）、`ksz/v0.1.1dev3`（定向语音开发通道）及后续完善，作为 KSZ 首个正式版本。

### 新增

- 初始化 KSZ Docker 配置、Agent 协作说明与 `KSZ/` 文档基地；约定 `dev` 主线及 `feature/ksz/<name>`、`dev/<topic>` 分支策略。
- 全模块本地构建入口 `KSZ/compose.yml`（由根目录 `docker-compose-ksz.yml` 迁入），为 server / web 补充本地 `build`，镜像标签 `server_local` / `web_local`。
- `KSZ/.env` / `.env.example`：参数化 Host 网络下 MySQL、Redis 端口（默认 MySQL `3307`），并将数据库密码、MySQL JDBC 额外参数纳入环境变量。
- 设备语言与蓝牙信标上下文：`manager-api` 持久化；`xiaozhi-server` 支持 `device_event` 上报与私有配置读取；通过 OpenAI 兼容请求 `extra_body` 透传给下游网关。
- 将 `ai_device_attribute` 重构为每设备一行的 `language`、`last_beacon_id`，并提供语言 / 信标专用更新接口。
- 信标位置语音导览：信标变更查询本地位置服务，复用 LLM / TTS 播报当前位置与附近展品；将室内位置注入 Agent 提示词。
- `device_event` 的 `beacon_change` 兼容顶层 `beacon_mac.beacon_id` 及 RSSI 等字段。
- 无鉴权 `/dev/ws` 开发控制通道：查询在线设备，按 MAC 或临时对端地址注入语音问题（仅限受控内网）。
- 语言属性校验限定为 `en`、`zh-cn`；设备属性短缓存后自动同步到 LLM 请求；OpenAI LLM provider 增加请求日志。

### 变更

- 默认改用远程 OpenAI 兼容 ASR，移除本地 `model.pt` 挂载。
- MySQL、Redis 改用国内镜像源；`.dockerignore` 排除运行时数据目录。
- 删除独立 `DEVLOG.md`，开发记录集中到本文件；使用与维护说明集中到 `README.md`。
- 精简 `AGENTS.md`，明确 KSZ 专用内容均以 `KSZ/` 为基地。
- 合并 `upstream/main`，保留 KSZ 设备属性、语言与蓝牙信标定制。

### 修复

- 修复 `model.pt` 被错误创建为目录的问题。
- 修复本地镜像不存在时被 Compose 错误拉取的问题。
- 诊断旧 web JAR 与新表结构不匹配导致 `/config/agent-models` 500、进而使 server 无法消费 ASR 音频的问题；在配置加载与连接初始化中补充异常日志。
- 位置服务无响应、404 或无效数据时保留信标持久化，仅提示进入新区域，避免编造位置或展品。

### 已知问题

- `202607101600.sql` 尚未登记到 `db.changelog-master.yaml`；已有环境执行该迁移前必须备份数据库。

[Unreleased]: https://github.com/apexlsai/xiaozhi-esp32-server/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/apexlsai/xiaozhi-esp32-server/releases/tag/v0.1.2
[0.1.1]: https://github.com/apexlsai/xiaozhi-esp32-server/releases/tag/ksz/v0.1.1
