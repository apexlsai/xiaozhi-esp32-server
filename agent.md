# KSZAI Agent 协作说明

> 本文件供 Cursor / AI Agent 在本 Fork 仓库中协作时使用。  
> 人类开发者请同时阅读 [`docs/kszai/DEVLOG.md`](docs/kszai/DEVLOG.md)。

---

## 项目身份

| 项 | 值 |
|----|-----|
| 项目名称 | KSZAI — 康硕展 AI 旅游机 |
| 上游 | [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) |
| 性质 | **Fork**，含 KSZ 定制部署与后续业务开发 |
| 联系 | jacob_ng@163.com |

本仓库 **不是** 上游官方仓库。修改时区分「可合并上游的通用修复」与「KSZ 专用定制」。

---

## 分支策略（必须遵守）

```
main                    # 发布 / 稳定（勿直接开发）
└── dev                 # KSZ 主开发线
    ├── dev/<topic>     # 子开发线，如 dev/docker、dev/api
    └── feature/ksz/<name>   # 功能分支，如 feature/ksz/tourism-guide
```

| 操作 | 分支 |
|------|------|
| 新功能、实验 | 从 `dev` 切 `feature/ksz/<name>` |
| 集成测试通过 | PR → `dev` |
| 发布 | `dev` → `main` |
| 同步上游 | merge/rebase `upstream/main` 到 `dev`，冲突在 `feature/ksz/*` 解决 |

**禁止**：在 `main` 上直接提交；将 KSZ 硬编码写入上游通用文档（`docs/Deployment*.md` 等）而不加 ksz 前缀路径。

---

## 部署与运行（当前标准）

### 推荐：Docker Compose（项目根目录）

```bash
docker compose up -d      # 启动
docker compose down     # 停止
docker compose logs -f  # 日志
```

| 文件 | 作用 |
|------|------|
| `docker-compose.yml` | 四服务编排（server / web / db / redis） |
| `.env` | 端口 + **宿主机数据路径** |

### 端口（`.env` 可调）

| 变量 | 默认 | 说明 |
|------|------|------|
| `PORT_WS` | 8000 | WebSocket |
| `PORT_HTTP` | 8001 | 文档/安全组提示 |
| `PORT_WEB` | 8002 | 智控台 |
| `PORT_VISION` | 8003 | 视觉分析 HTTP |

**Agent 注意**：

- 修改端口只改 `.env` 和文档，不要在多处硬编码。
- WSL2 + Cursor 开发时，`8002` 可能被 `Cursor.exe` 占用；若绑定失败，改用 `8080` 等并更新 `.env`。
- 容器内服务间通信端口固定（如 web 容器内 `8002`），与宿主机映射端口可以不同。

### 数据持久化（拉镜像不会清数据）

默认数据在 **`/opt/xiaozhi-server/`**（历史安装脚本写入 `.env`）：

```
/opt/xiaozhi-server/
├── data/.config.yaml          # Python 服务端配置 ← 改配置改这里
├── models/SenseVoiceSmall/model.pt
├── mysql/data/                # MySQL ← 智控台用户/设备/参数
├── uploadfile/                # 上传文件
└── backup/                    # 脚本升级备份
```

项目根 `./data` 默认为空；仅当 `.env` 改为相对路径时才使用。

**Agent 不要**假设 `docker compose pull` 会重置配置或数据库。

### 遗留脚本

| 脚本 | 状态 |
|------|------|
| `docker-setup-ksz.sh` | KSZ 一键安装，含「康硕展 AI 旅游机」文案；**仍引用** `/opt/.../docker-compose_all.yml`，与根目录 compose **未完全同步** |
| `docker-setup.sh` | 上游原版，非 KSZ 维护重点 |

修改部署逻辑时：**优先改 `docker-compose.yml` + `.env`**，再同步 `docker-setup-ksz.sh`。

---

## 服务架构速查

```
┌─────────────────────┐     ┌──────────────────────┐
│ xiaozhi-esp32-server│     │ xiaozhi-esp32-server-web │
│ Python :8000 WS     │     │ nginx :8002 + Java :8003 │
│        :8003 vision │     │ 智控台 / OTA API         │
└─────────┬───────────┘     └──────────┬───────────┘
          │                              │
          └──────────┬───────────────────┘
                     │
          ┌──────────┴──────────┐
          │ db (MySQL)  redis   │
          └─────────────────────┘
```

关键配置项（`.config.yaml`）：

```yaml
manager-api:
  url: http://xiaozhi-esp32-server-web:8002/xiaozhi   # Docker 网络内地址，端口 8002 固定
  secret: <智控台 参数字典 server.secret>
server:
  port: 8000
  http_port: 8003
```

---

## Agent 编码原则

1. **最小 diff** — KSZ 定制放 `feature/ksz/*`，避免污染上游通用模块。
2. **配置外置** — 端口、路径进 `.env`；不在脚本/HTML 中硬编码 `8002` 等。
3. **compose 优先** — 生命周期用 `docker compose`，避免单独 `docker restart` 导致网络孤立。
4. **文档同步** — 部署/排障变更写入 `docs/kszai/DEVLOG.md`。
5. **中文注释** — KSZ 面向文档与脚本说明使用简体中文。
6. **不提交密钥** — `.config.yaml` 中的 `secret`、API Key 勿入 git。

---

## 常见排障（Agent 可直接引用）

| 现象 | 检查 | 处理 |
|------|------|------|
| 8002 连不上 | `docker port xiaozhi-esp32-server-web`；Windows `netstat` | 绕过代理 curl；改端口或释放占用 |
| 仍是旧配置 | `ls -la /opt/xiaozhi-server/data/` 时间戳 | 编辑 `.config.yaml` 或清 MySQL（见 DEVLOG） |
| db 主机名解析失败 | 容器是否同一 compose 网络 | `docker compose down && up -d --force-recreate` |
| 容器名冲突 | 旧容器残留 | `docker rm -f xiaozhi-esp32-server-*` |
| curl 502 | `echo $http_proxy` | `curl --noproxy '*'` |

---

## 相关路径

| 路径 | 说明 |
|------|------|
| `docs/kszai/DEVLOG.md` | 开发日志（按日期记录） |
| `agent.md` | 本文件 |
| `docker-compose.yml` | 主编排文件 |
| `.env` | 环境变量（不提交敏感值） |
| `docker-setup-ksz.sh` | KSZ 安装脚本 |
| `main/xiaozhi-server/` | 上游 Python 服务端源码 |
| `main/manager-api/` | 上游 Java 智控台 API |
| `main/manager-web/` | 上游智控台前端 |

---

## 更新本文件

当部署方式、分支策略、数据路径发生变更时，Agent 应同时更新：

1. `agent.md`（本文件）
2. `docs/kszai/DEVLOG.md`（追加 dated 条目）
