# 场馆、设备与信标联动

资产配置统一在 `museum_guide_management` 的 `/admin/` 管理。小智保留设备激活、用户和智能体绑定，通过 management 的集成 API 获取导览策略。

## 部署

先升级 management、执行迁移、构建管理页面并创建管理员，按其 README 操作。既有信标的 MAC 必须是有效蓝牙地址，迁移会拒绝非法值或规范化冲突，不会猜测或删除资产。

在 `KSZ/.env` 配置：

```dotenv
KSZ_GUIDE_ENABLED=1
KSZ_GUIDE_PRESENCE_ENABLED=1
KSZ_MANAGEMENT_API_URL=http://127.0.0.1:14000/api/v1/integration
KSZ_MANAGEMENT_API_TOKEN=<与management的INTEGRATION_API_KEY一致>
KSZ_GUIDE_CONTROL_URL=http://host.docker.internal:8004/internal/ksz/guide/control
KSZ_GUIDE_CONTROL_TOKEN=<单独生成的至少32字符令牌>
```

两个令牌使用不同的随机值。management 的服务凭据不能供浏览器或固件使用。控制令牌只用于 MQTT 网关调用小智 HTTP 服务。KSZ Host 部署的 HTTP 端口为 `8004`，`8003` 是 Java manager-api。

```bash
docker compose --env-file .env --profile mqtt up -d --build xiaozhi-esp32-server xiaozhi-mqtt-gateway
```

此命令是部署操作，会重启对应服务。仓库默认 `KSZ_GUIDE_ENABLED=0`，便于先完成资产登记及固件联调后再启用。启用时不得关闭现有设备接入鉴权。源码运行时将 `KSZ/server` 加入 `PYTHONPATH`；专用 Dockerfile 已打包 `ksz_guide`。

仅同步设备在线状态时，设置 `KSZ_GUIDE_PRESENCE_ENABLED=1`、`KSZ_GUIDE_ENABLED=0`。开启导览会同时开启在线同步。两种模式均需配置上述服务地址和令牌；management 还需设置 `REDIS_URL`。管理端在容器中访问宿主机 Redis 时使用 `host.docker.internal` 和实际 Redis 端口，例如 `redis://host.docker.internal:6380/2`。

更新源码后必须重新构建 server 和 MQTT 网关镜像；单独 `restart` 不会更新代码或容器环境变量。management 修改环境变量后也需要通过其 Compose 重新创建 API 容器。

## 信标讲解规则

服务端规则集中在 `KSZ/.env`，模板见 [.env.example](.env.example)。布尔开关使用 `0/1`，时间单位为毫秒；默认值如下：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `KSZ_GUIDE_ENABLED` | `0` | management 导览联动总开关，启用前须配置服务地址和令牌 |
| `KSZ_GUIDE_PRESENCE_ENABLED` | `0` | 导览关闭时仍可独立同步设备在线状态 |
| `KSZ_GUIDE_AUTO_ANNOUNCE_ENABLED` | `1` | 自动讲解总开关；设为 `0` 时仍更新可信位置，可供文字和视觉使用 |
| `KSZ_GUIDE_ANNOUNCE_ON_FIRST_BEACON` | `0` | 首次有效信标是否讲解；`0` 只建立基准，`1` 也触发讲解 |
| `KSZ_GUIDE_INTERRUPT_ON_BEACON_CHANGE` | `1` | 切换信标时中止旧信标讲解并跳过本轮冷却；`0` 等待旧讲解结束 |
| `KSZ_GUIDE_MIN_ANNOUNCE_INTERVAL_MS` | `15000` | 两次自动讲解的最小间隔，`0` 关闭冷却 |
| `KSZ_GUIDE_POSITION_TTL_MS` | `15000` | 最后一次有效信标确认后的可信位置有效期，必须大于 `0` |
| `KSZ_GUIDE_PENDING_TTL_MS` | `15000` | 信标变化后待播任务的有效期，必须大于 `0` |
| `KSZ_GUIDE_USER_QUIET_MS` | `15000` | `listen stop/detect` 后的额外静默时间，`0` 关闭额外静默 |
| `KSZ_GUIDE_LOCATION_TEMPLATE_ZH_CN` | `您现在位于{location}。` | 中文及中文方言的位置开场 |
| `KSZ_GUIDE_LOCATION_TEMPLATE_EN` | `You are now at {location}.` | 英语位置开场 |
| `KSZ_GUIDE_LOCATION_TEMPLATE_JA` | `現在、{location}にいます。` | 日语位置开场 |
| `KSZ_GUIDE_LOCATION_TEMPLATE_KO` | `현재 {location}에 계십니다.` | 韩语位置开场 |

自动讲解需同时满足总开关、设备策略 `auto_announce` 和资产授权。环境变量不能放开禁用设备或越过场馆范围；用户正在录音、对话或使用视觉时仍优先处理用户交互。全局自动讲解开关和冷却时间会同步到新协议的有效策略及校验摘要，不改写 management 原始策略。

触发依据始终为有效信标 MAC 变化；同一信标的重复上报不重播。首次播报开关也适用于服务重启或策略重置后重新建立基准。位置有效期应覆盖固件位置心跳间隔；待播任务即使因冷却或用户交互等待，也不会延长到期时间。时间参数最大为 `86400000`（24 小时），非法开关、负数或不合法的有效期会在启动时明确报错。

RSSI 门槛、扫描窗口、稳定判定、蓝牙发射功率及固件自身冷却仍由 management 下发策略和固件实现控制；以上服务端变量不会改变旧固件的扫描或上报行为。

自动讲解先由服务端填充并播报固定位置句，再接模型生成的简短介绍。例如：“您现在位于3F，当代萍乡规划展，出口。”开场不经过模型改写，也不等待模型首字。`{location}` 由已校验信标的楼层、区域和位置说明组成，空字段跳过；地点名称保留登记值，语言按本轮绑定语言选择。

模板必须且只能包含一个 `{location}`，不支持其他字段、格式说明或转换符；空值采用默认模板。模型仅生成后续介绍，导览专用流式过滤会移除开头的问候、自我介绍和重复位置句；正文中的正常引用不做全局替换。普通对话的人设和回复方式保持原样。

固定开场与正文共用本轮字幕、音频和取消状态；切换信标或用户打断时一起取消，迟到内容不会恢复播放。模型失败、没有正文或只返回被过滤的开场时，结束当前播报，不补造介绍。最终记录与实际提交到 TTS 的文本一致。

修改 `.env` 后，在 `KSZ/` 执行以下命令重建并更新 server；仅修改已有镜像支持的环境变量时可去掉 `--build`，不能只执行 `restart`：

```bash
docker compose --env-file .env -f compose.yml up -d --build --no-deps xiaozhi-esp32-server
```

## 管理操作

1. 创建场馆及导览区域，登记信标的业务编号、完整 MAC 和区域。多枚信标可以覆盖同一区域。
2. 设备可手工登记，也可在认证连接后出现在待配置列表。连接 MAC 是本期设备运行身份，SN 只读预留，不生成、不必填。
3. 选择设备模式：场馆机必须指定一个场馆；工程机选择测试场馆或单独信标；C 端机自由识别平台有效场馆。工程范围取并集，空范围不放行。
4. 场馆机与 C 端机默认自动讲解，工程机默认关闭。资产禁用、报废、场馆停用都会阻止导览。
5. 查看设备详情的服务端策略版本、固件确认版本和更新时间。旧固件显示兼容模式，不代表已经下载白名单。

设备资产归属与当前所在场馆不同。C 端跨馆、工程测试都不会修改设备的固定归属。场馆不能在仍有引用时删除，日常退出使用停用。

## 生效与故障语义

management 的 PostgreSQL 保存资产和操作记录。Redis 仅缓存目录和运行态，使用独立键前缀；小智不直接读取该 Redis。

设备只连接 xiaozhi。MQTT 网关和 WebSocket 服务根据已认证连接同步在线状态，旧固件无需新增报文；MQTT 待机连接也会显示在线。在线状态、语音会话、当前位置和策略确认分别记录，连接保活不会延长位置有效期。连接变化合并上报，网关每10秒更新存活快照；状态服务暂时失败后自动重试。

小智每30秒批量校验策略，授权硬有效期最多60秒；同场馆共享目录缓存，未知候选有负缓存及限速。变更不会被设备自报场馆或模式覆盖。配置页应根据实际服务端版本与固件 ACK 判断同步状态。

导览控制独立于语音会话。MQTT 待机也能上报稳定位置，有效信标 MAC 变化且允许播报时下发 `request_hello`；开启首次播报时首次有效信标也可唤醒。固件建链并建立 UDP 回程后发送 `ready`；服务端核验会话、策略、位置和 TTS 就绪后播报。具体协议见 [固件协议](guide-protocol.md)。

默认首次有效位置只建立基准；之后有效信标 MAC 变化触发讲解，同一区域内切换信标也会触发。同一信标重复上报不重复讲解，切换到其他信标后再返回原信标会重新触发。新固件每5秒确认当前稳定信标；默认15秒没有有效位置确认即失位，连接心跳本身不延长位置。默认播报冷却15秒，用户交互期间不插播；过期任务会被取消。首次播报、冷却及超时可通过上述环境变量调整。

自动讲解的内部位置提示仅用于模型请求，不作为用户识别文本下发设备；设备仍接收正常的TTS播放控制、讲解字幕和音频。首次自动讲解无声、普通对话后恢复时，检查固件是否在hello之后主动发送UDP首包，不能以MQTT在线代替音频就绪。

management 超时不会沿用旧逻辑播报“进入新区域”。授权过期后停止自动导览，文字与视觉请求均移除未确认的位置，普通对话继续。Redis 不可用时目录受限回源数据库、管理页实时状态显示未知；Redis 中的历史位置不能恢复成当前导览位置。

## 验证

```bash
PYTHONPATH=KSZ/server:main/xiaozhi-server python -m pytest KSZ/server/tests
node --test KSZ/mqtt/tests/guide-control.test.cjs
GATEWAY_DIR=<已应用KSZ补丁的网关目录> node --test KSZ/mqtt/tests/gateway.test.cjs
```

规模脚本 `KSZ/server/benchmarks/management_load.py` 使用独立本地测试数据库，默认1000台设备、10个场馆、5秒心跳12轮。脚本会重建专用 `guide_benchmark` 库内的表，内置连接校验禁止使用其他数据库；依赖 management 开发依赖及 aiohttp。结果统计真实 HTTP/SQL 请求数、控制处理延迟和进程最大内存，不包含真实无线电、语音合成或硬件响应耗时。

本地隔离验证记录见 [result.json](server/benchmarks/result.json)：1000设备、10场馆、12000次心跳，共61.64秒（首次同步和完整ACK占4.61秒）。预热后产生10次批量策略查询、10次运行态报告及每馆1次目录周期刷新，合计110条SELECT；控制处理P95为12.886ms，进程峰值内存111.79MiB。该结果为本机模拟控制链测试，不代表硬件无线链路或真实语音服务容量。

本次验证：管理端24项测试、管理页面19项测试、构建与浏览器真实API流程、小智新增35项及原链路55项、MQTT网关20项均通过。固件接入契约已同步到[现有联调文档](https://my.feishu.cn/wiki/PBPKwcEKDiTkHLkSf9tcEEHJnJt)第2章。
