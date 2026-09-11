# 场馆、设备与信标联动

资产配置统一在 `museum_guide_management` 的 `/admin/` 管理。小智保留设备激活、用户和智能体绑定，通过 management 的集成 API 获取导览策略。

## 部署

先升级 management、执行迁移、构建管理页面并创建管理员，按其 README 操作。既有信标的 MAC 必须是有效蓝牙地址，迁移会拒绝非法值或规范化冲突，不会猜测或删除资产。

在 `KSZ/.env` 配置：

```dotenv
KSZ_GUIDE_ENABLED=1
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

## 管理操作

1. 创建场馆及导览区域，登记信标的业务编号、完整 MAC 和区域。多枚信标可以覆盖同一区域。
2. 设备可手工登记，也可在认证连接后出现在待配置列表。连接 MAC 是本期设备运行身份，SN 只读预留，不生成、不必填。
3. 选择设备模式：场馆机必须指定一个场馆；工程机选择测试场馆或单独信标；C 端机自由识别平台有效场馆。工程范围取并集，空范围不放行。
4. 场馆机与 C 端机默认自动讲解，工程机默认关闭。资产禁用、报废、场馆停用都会阻止导览。
5. 查看设备详情的服务端策略版本、固件确认版本和更新时间。旧固件显示兼容模式，不代表已经下载白名单。

设备资产归属与当前所在场馆不同。C 端跨馆、工程测试都不会修改设备的固定归属。场馆不能在仍有引用时删除，日常退出使用停用。

## 生效与故障语义

management 的 PostgreSQL 保存资产和操作记录。Redis 仅缓存目录和运行态，使用独立键前缀；小智不直接读取该 Redis。

小智每30秒批量校验策略，授权硬有效期最多60秒；同场馆共享目录缓存，未知候选有负缓存及限速。变更不会被设备自报场馆或模式覆盖。配置页应根据实际服务端版本与固件 ACK 判断同步状态。

导览控制独立于语音会话。MQTT 待机也能上报稳定位置，确有区域变化且允许播报时才下发 `request_hello`。固件建链并建立 UDP 回程后发送 `ready`；服务端核验会话、策略、位置和 TTS 就绪后播报。具体协议见 [固件协议](guide-protocol.md)。

首次有效位置只建立基准，同区域不同信标不重复讲解。新固件每5秒确认当前稳定信标；15秒没有有效位置确认即失位，连接心跳本身不延长位置。播报冷却15秒，用户交互期间不插播；过期任务会被取消。

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
