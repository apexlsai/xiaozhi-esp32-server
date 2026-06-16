# KSZAI 项目开发日志

> 康硕展 AI 旅游机 — 基于 [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 的 Fork 专用记录  
> 维护分支：`dev`、`dev/...`、`feature/ksz/...`

---

## 2026-06-16 — Docker 部署体系梳理与环境排障

### 背景

Fork 后需在本地/WSL2 环境用 Docker 快速拉起全模块（Python 服务端 + Java 智控台 + MySQL + Redis），并与上游仓库保持可合并性。

### 完成事项

#### 1. 端口配置集中化（`docker-setup-ksz.sh`）

- 在脚本开头定义端口变量，去除硬编码：
  - `PORT_WS=8000` — WebSocket
  - `PORT_HTTP=8001` — HTTP（安全组提示用）
  - `PORT_WEB=8002` — 智控台 / OTA
  - `PORT_VISION=8003` — 视觉分析
- 脚本内对话框、Python 配置写入、安装完成提示均改为引用上述变量。

#### 2. 部署方式统一为项目根目录 `docker compose`

- 新增/完善根目录 **`docker-compose.yml`**（由原 `main/xiaozhi-server/docker-compose_all.yml` 演化）。
- 新增 **`.env`** 管理端口与数据目录。
- **推荐日常启动方式**（不再依赖远程下载 compose 文件）：

```bash
cd ~/Project/xiaozhi-esp32-server
docker compose up -d
docker compose down
docker compose logs -f
```

- **`docker-setup-ksz.sh`** 仍保留「康硕展 AI 旅游机」一键安装能力，但内部仍指向旧路径 `/opt/xiaozhi-server/docker-compose_all.yml`，与当前 compose 流程**尚未完全同步**（见「待办」）。

#### 3. 数据目录与持久化（重要）

**重新拉取 Docker 镜像不会清除业务数据。** 配置、数据库、模型均通过 volume 挂载在宿主机。

当前 `.env` 默认指向历史安装目录：

| 变量 | 路径 | 内容 |
|------|------|------|
| `DATA_DIR` | `/opt/xiaozhi-server/data` | `.config.yaml` 服务端配置 |
| `MODEL_PATH` | `/opt/xiaozhi-server/models/SenseVoiceSmall/model.pt` | ASR 模型 |
| `UPLOAD_DIR` | `/opt/xiaozhi-server/uploadfile` | 智控台上传文件 |
| `MYSQL_DATA_DIR` | `/opt/xiaozhi-server/mysql/data` | MySQL 数据（用户、设备、参数等） |

首次安装时间：**2026-06-03**（文件时间戳可验证）。  
项目根 `./data` 目录为空，**当前 compose 未使用**。

容器内挂载关系：

```
xiaozhi-esp32-server     → /opt/xiaozhi-server/data → /opt/xiaozhi-esp32-server/data
xiaozhi-esp32-server     → model.pt 单文件挂载
xiaozhi-esp32-server-web → /opt/xiaozhi-server/uploadfile → /uploadfile
xiaozhi-esp32-server-db  → /opt/xiaozhi-server/mysql/data → /var/lib/mysql
```

#### 4. 服务端口与访问地址

| 服务 | 默认端口 | 访问示例 |
|------|----------|----------|
| 智控台 | 8002 | `http://127.0.0.1:8002/` |
| WebSocket | 8000 | `ws://127.0.0.1:8000/xiaozhi/v1/` |
| 视觉分析 | 8003 | `http://127.0.0.1:8003/mcp/vision/explain` |
| OTA | 8002 | `http://127.0.0.1:8002/xiaozhi/ota/` |

容器间 `manager-api` 地址应使用 Docker 网络主机名（**容器内端口固定 8002**）：

```yaml
manager-api:
  url: http://xiaozhi-esp32-server-web:8002/xiaozhi
```

#### 5. 排障记录

##### 5.1 智控台启动卡在「正在检查服务启动状态」

- 脚本轮询 `xiaozhi-esp32-server-web` 日志中的 `Started AdminApplication in`。
- Spring Boot 首次启动约 30s～2min，属正常现象。

##### 5.2 `UnknownHostException: xiaozhi-esp32-server-db`

- 原因：容器未在同一 compose 网络（分批启动 / 手动 `docker restart`）。
- 处理：`docker compose down --remove-orphans && docker compose up -d --force-recreate`。

##### 5.3 容器名冲突

```
The container name "/xiaozhi-esp32-server-db" is already in use
```

- 原因：旧安装残留容器。
- 处理：`docker rm -f xiaozhi-esp32-server xiaozhi-esp32-server-web xiaozhi-esp32-server-db xiaozhi-esp32-server-redis` 后重新 `up`。

##### 5.4 8002 curl 无响应 / 端口未发布

排查结论（按优先级）：

1. **curl 走代理**：环境变量 `http_proxy=127.0.0.1:7897` 会导致本机请求 502。  
   测试时使用：`curl --noproxy '*' http://127.0.0.1:8002/`

2. **Windows 端口占用（WSL2）**：
   - `8000` — `svchost.exe` 监听
   - `8002` — **`Cursor.exe` 占用**（在 Cursor 中开发时可能与智控台默认端口冲突）
   - 若绑定失败，可临时改 `.env` 为 `PORT_WEB=8080`、`PORT_WS=8001` 等可用端口

3. **Windows 保留端口**：经 `netsh interface ipv4 show excludedportrange protocol=tcp` 验证，8000～8002 **不在**保留范围内，排除该原因。

4. **镜像更新不影响数据**：若感觉「仍是旧配置」，检查 `/opt/xiaozhi-server/data/.config.yaml` 与 MySQL 数据目录，而非镜像版本。

##### 5.5 当前运行时配置样例（2026-06-16）

路径：`/opt/xiaozhi-server/data/.config.yaml`

```yaml
manager-api:
  secret: <已配置>
  url: http://xiaozhi-esp32-server-web:8002/xiaozhi
server:
  port: 8000
  http_port: 8003
  vision_explain: "http://你的ip或者域名:端口号/mcp/vision/explain"  # 仍为占位符，需替换
```

### 仓库内 KSZ 相关文件

| 文件 | 说明 |
|------|------|
| `docker-compose.yml` | **当前推荐** 全模块 compose |
| `.env` | 端口 + 数据目录 |
| `docker-setup-ksz.sh` | KSZ 定制一键安装脚本（含「康硕展 AI 旅游机」 branding） |
| `docker-setup.sh` | 上游原版安装脚本（勿与 KSZ 流程混淆） |
| `docs/kszai/DEVLOG.md` | 本文件 |
| `agent.md` | Agent / AI 协作说明 |

### 待办（后续 dev / feature/ksz 分支处理）

- [ ] 同步 `docker-setup-ksz.sh`：改用根目录 `docker-compose.yml` + `run_compose` 封装，去除 `docker-compose_all.yml` 远程下载
- [ ] 更新 `/opt/xiaozhi-server/data/.config.yaml` 中 `vision_explain` 占位符
- [ ] 评估是否将 `.env` 默认 `DATA_DIR` 改为 `./data`（与 `/opt` 历史安装解耦）
- [ ] 文档化 Cursor/WSL2 下 8002 端口冲突的规避方案

---

## 分支约定（2026-06-16 起）

| 分支类型 | 命名 | 用途 |
|----------|------|------|
| 主开发线 | `dev` | KSZ Fork 稳定集成分支 |
| 子开发线 | `dev/<topic>` | 并行开发（如 `dev/docker`、`dev/config`） |
| 功能分支 | `feature/ksz/<name>` | 单功能 / 实验（如 `feature/ksz/tourism-ui`） |
| 上游同步 | `upstream/main` 或定期 merge | 合并 xinnan-tech 上游更新 |

**原则**：KSZ 定制提交走 `feature/ksz/*` → 合并 `dev`；避免直接在 `main` 上开发。

---

## 修订历史

| 日期 | 作者 | 摘要 |
|------|------|------|
| 2026-06-16 | jacob | 初版：Docker compose 迁移、数据目录、端口排障、分支约定 |
