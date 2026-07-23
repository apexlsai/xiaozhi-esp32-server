# KSZ 开发变更记录

本文件仅记录 KSZ Fork 相对上游 `xiaozhi-esp32-server` 的开发历史、设计决策和已知风险。部署、配置、接口调用及排障命令统一见 [README.md](README.md)。

## 2026-07-23 — KSZ 文档与本地构建入口统一

- 全模块本地构建 Compose 由项目根目录 `docker-compose-ksz.yml` 迁移并改名为 `KSZ/compose.yml`。
- 为 server、web 服务补充本地 `build` 定义，分别使用根目录 `Dockerfile-server` 和 `Dockerfile-web`，修复本地镜像不存在时被错误拉取的问题。
- 删除独立 `DEVLOG.md`，将历史集中到本文件；使用和维护说明集中到 `README.md`。
- 重构本 CHANGELOG 为纯时间线：移除命令、SQL、接口示例和配置代码块，补齐 2026-06-16 至 2026-07-22 的 KSZ 开发、迁移、排障与上游同步节点。
- 精简 `AGENTS.md`，明确所有 KSZ 专用开发、部署、配置和文档均以 `KSZ/` 为基地。

## 2026-07-22 — 同步上游

- 将 `upstream/main` 合并到 KSZ `dev` 开发线。
- 保留 KSZ 的设备属性、语言和蓝牙信标上下文定制。

## 2026-07-17 — MCP 集成说明

- 梳理统一工具处理器支持的 `SERVER_PLUGIN`、`SERVER_MCP`、`DEVICE_IOT`、`DEVICE_MCP` 和 `MCP_ENDPOINT` 五类工具。
- 记录内置插件函数、外部 MCP 服务配置位置及相关项目文档。

## 2026-07-14 至 2026-07-16 — 设备属性结构化与配置诊断

- 将 `ai_device_attribute` 从 key-value 结构迁移为每设备一行的 `language`、`last_beacon_id` 字段，并提供兼容接口。
- 补充语言、蓝牙信标的专用更新接口及迁移验证流程。
- 诊断出旧 web JAR 查询 `attr_key` 与新表结构不匹配时，会使 `/config/agent-models` 返回 500，进而令 Python server 因缺少 TTS 配置而无法消费 ASR 音频。
- 在 `config_loader.py`、`connection.py` 增加配置获取、模块初始化和并发初始化的详细异常日志，避免错误被静默吞没。
- 已知风险：`202607101600.sql` 尚未登记到 `db.changelog-master.yaml`；为已有环境执行该迁移前必须备份数据库。

## 2026-07-01 — KSZ 本地镜像部署

- 新增全模块及单 server 的 KSZ Compose 配置。
- 本地镜像标签定为 `xiaozhi-esp32-server:server_local` 和 `xiaozhi-esp32-server:web_local`，上游远程镜像配置保持不变。
- 语言属性校验限定为 `en`、`zh-cn`；设备属性改动会在短缓存周期后自动同步到 LLM 请求。
- OpenAI LLM provider 增加请求日志，便于确认 `device_id`、`language`、`last_beacon_id` 的透传结果。

## 2026-06-28 至 2026-06-30 — 设备上下文接入 LLM

- `manager-api` 持久化设备语言和蓝牙信标事件；`xiaozhi-server` 支持 `device_event` 上报和私有配置读取。
- 将设备上下文写入 OpenAI 兼容请求的 `extra_body`，由下游网关负责语言和位置感知处理。
- 先后尝试系统提示词语言约束与会话内实时指令，最终保留上下文透传方案，避免服务端自行翻译。
- 增加用户 token 查询与临时续期的运维记录，具体操作已迁移至 README。

## 2026-06-27 — KSZ Docker 与模型服务接入

- `.dockerignore` 排除运行时数据目录，避免本地构建将 MySQL、上传文件和配置带入镜像。
- 为 KSZ 本地构建使用 `web_local`、`server_local` 镜像标签，不向上游提交该部署定制。
- 接入本地 FunASR 和 OpenAI 兼容 LLM/SLM 服务，并记录模型配置缓存清理要求。
- 配置 WebSocket、OTA 地址和 `server.secret` 的运行时参数。

## 2026-06-26 — 初始部署修复

- 修复 `model.pt` 被错误创建为目录的问题，改由启动脚本创建模型文件占位。
- 调整 MySQL、Redis 镜像源以适应国内网络环境。
- 建立数据库模型配置、Redis 缓存刷新及服务地址维护流程。

## 2026-06-16 — KSZ Fork 初始化

- 初始化 KSZ Docker 配置、Agent 协作说明和部署变更记录。
- 建立 `dev` 主开发线、`dev/<topic>` 与 `feature/ksz/<name>` 分支约定；KSZ 定制与可回馈上游的通用修改分离。
- 整理数据持久化、端口冲突、容器名称冲突和代理导致的本地访问故障。
