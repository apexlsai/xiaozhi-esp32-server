# 导览控制协议 v1

本协议用于 management 与 xiaozhi 联动版本，须部署服务并开启 `KSZ_GUIDE_ENABLED=1`。固件由独立团队实现；本文定义接入契约，不表示设备或线上服务已经升级。现有语言、MCP、视觉与音频协议保持原约定。

## 连接与身份

MQTT 完成认证和订阅后可发送导览控制消息，不需要先建立语音会话。主题仍为 OTA 返回的发布及订阅主题。WebSocket 降级通过已有认证连接发送同类消息。固件不能直接访问 management、控制 HTTP 接口或持有服务间令牌。

设备连接 MAC、信标 MAC、管理端业务编号是不同字段。设备身份由认证通道确定；新报文只在 `beacon.mac` 填信标地址。SN 本期不填写、不生成。旧 `beacon_mac.beacon_id` 的兼容含义仍是信标 MAC。

所有上行导览消息使用：

```json
{
  "type": "guide_control",
  "version": 1,
  "action": "sync",
  "request_id": "boot-001-1",
  "boot_id": "boot-001",
  "seq": 1,
  "chunk_index": 0
}
```

`boot_id` 每次固件启动生成新值，同一次启动重连保持；`seq` 在该启动内单调递增，范围0至9007199254740991。`request_id` 和 `boot_id` 均为1–128字符。相同请求重试沿用原编号，新的动作使用新序号。收到 `sync_required` 必须先同步策略。

## 策略下载和确认

`sync` 可选携带 `device` 对象中的 `model`、`hardware_version`、`firmware_version`。接入同步可补齐型号和硬件信息、更新固件版本；型号或硬件冲突会被拒绝。模式、归属和导览权限由后台决定。

服务端返回 `action=policy`，结构如下，尖括号内容取实际响应：

```json
{
  "type": "guide_control",
  "version": 1,
  "action": "policy",
  "policy_revision": "<版本>",
  "policy": {
    "device_mac": "AA:BB:CC:DD:EE:01",
    "mode": "venue",
    "enabled": true,
    "expires_at": 1789158060,
    "auto_announce": true,
    "current_venue_id": "<场馆ID>",
    "filter": {
      "rssi_enter": -75,
      "rssi_advantage": 8,
      "stable_ms": 3000,
      "window_ms": 2000,
      "lost_ms": 8000,
      "min_announce_interval_ms": 15000
    }
  },
  "manifest": {
    "sha256": "<整版摘要>",
    "chunk_count": 1,
    "beacon_count": 1,
    "chunk_size": 128,
    "manifest_json": "<服务端提供的原始清单字符串>"
  },
  "chunk_index": 0,
  "beacons": [{ "mac": "DA:01:16:00:08:57" }],
  "chunk_json": "<服务端提供的当前块原始字符串>"
}
```

此示例省略部分策略及信标字段，固件必须保留实际响应完整对象用于校验。每块最多128枚；通过新的 `sync` 消息携带 `chunk_index` 和上一块的 `policy_sha256` 获取后续块。版本或摘要改变时从第0块重新下载，不混用两版。

摘要计算：对下面的字节序列计算SHA-256，得到十六进制小写值：

```text
UTF8(manifest.manifest_json + "\n" + chunk_json[0] + "\n" + ... + chunk_json[N-1] + "\n")
```

`manifest_json`、`chunk_json`均由服务端直接提供。固件先JSON解码取得字符串，再按原顺序以UTF-8喂入SHA；不要重新序列化这些字符串，不改变键、数组顺序、空格或Unicode形式。每段之间以及最后都有一个LF字节。空白名单仍为一块，`chunk_json`为`[]`。服务端采用递归键排序、紧凑JSON、UTF-8非ASCII原文；`manifest_json`排除易变的`expires_at`及重复的`beacon_allowlist`，含`current_venue_id`。解析使用的policy/beacons必须与对应JSON字符串匹配。

下载及摘要校验通过后原子替换本地策略，再发送：

```json
{
  "type": "guide_control", "version": 1, "action": "policy_ack",
  "request_id": "boot-001-2", "boot_id": "boot-001", "seq": 2,
  "policy_revision": "<版本>",
  "policy_sha256": "<manifest.sha256>", "chunk_count": 1
}
```

`enabled=false` 时停止自动导览，不降级成自由扫描。策略有效期以 `expires_at`（Unix秒）为准，设备应同步时钟；续期不会改变配置内容摘要。服务端还会自行核验授权，不依赖固件承诺。

场馆机只使用所归属场馆白名单；工程机范围是指定场馆与单独信标的并集。C端允许平台登记信标的候选发现，后台确认场馆后可下发当地清单；不要把“尚未下载到MAC”理解成“所有信标都合法”。MAC前缀只能粗筛，不能证明资产归属。

## 扫描、防抖与位置更新

先过滤候选，再进行RSSI平滑和位置切换判断。参数来自 `policy.filter`，默认2秒窗口、−75dBm进入门槛、新候选强8dB并持续3秒、8秒丢失；参数必须现场标定。

仅稳定切换才发 `observation`：

```json
{
  "type": "guide_control", "version": 1, "action": "observation",
  "request_id": "boot-001-3", "boot_id": "boot-001", "seq": 3,
  "policy_revision": "<版本>",
  "beacon": { "mac": "DA:01:16:00:08:57", "rssi": -55 }
}
```

可选广播身份使用 `beacon.uuid`、`beacon.major`、`beacon.minor`，必须全填或全空。Major/Minor是0–65535整数。若管理端登记了广播三元组，新协议必须完整携带且与登记值匹配；旧协议仅按完整MAC兼容校验。

每5秒发送 `action=heartbeat`，其他信封字段一致，并携带当前仍稳定观测到的 `beacon`。仅设备在线但未观测到信标时，不得补报历史信标。确认丢失发送 `action=lost`（不带beacon）；服务端15秒未获得有效位置确认也会失位。

首次有效位置只建立基准；后续导览区域变化且自动讲解开启时触发。多枚信标覆盖同一区域不重复播报，重连不算移动。位置变化不会修改设备固定归属。

结果消息使用 `action=result`，含 `request_id`、`seq`、`accepted` 和 `reason`。未知、跨馆、停用或冲突信标不更新位置、不触发语音。`policy_revision_mismatch` 时同步新策略；`resolve_rate_limited` 时按正常稳定观测节奏重试，不能快速循环请求。

完整策略确认后，成功结果可携带轻量续期信息：`lease: {policy_revision, policy_sha256, expires_at}`。固件仅在这三个字段有效，且版本与摘要均匹配本地已原子应用的策略时更新到期时间；没有匹配的lease就不续期。无需重复下载相同清单。拒绝、未完成ACK或授权过期的结果不提供续期；重复请求也重新核验当前租期。无位置信标时仍每5秒发送不带beacon的heartbeat维持策略同步，但不延长位置有效期。

## 待机唤醒与播放就绪

服务端决定需要待机播报时发送：

```json
{
  "type": "guide_control", "version": 1, "action": "request_hello",
  "activation_id": "<本次导览任务>", "session_attempt": 1
}
```

固件先按现有MQTT协议发送 `hello`，保留原音频和features字段，增加顶层：

```json
{
  "guide": {
    "activation_id": "<任务ID>",
    "session_attempt": 1,
    "boot_id": "boot-001"
  }
}
```

网关注入连接身份并透传，固件不自行生成 `connection_id`。同一任务与attempt重试hello不会重建语音会话或重置UDP密钥；不得通过重复hello当作播放重试。

新固件主动对话的hello也携带 `guide.boot_id` 与递增的 `guide.session_attempt`，不带 `activation_id`；完成建链后同样发送不带任务ID的 `ready`。普通对话与待机导览共用这套会话编号，避免旧连接就绪消息覆盖新会话。

收到hello响应后按既有密钥、nonce建立UDP并发送有效首包建立NAT回程。随后发送：

```json
{
  "type": "guide_control", "version": 1, "action": "ready",
  "request_id": "boot-001-4", "boot_id": "boot-001", "seq": 4,
  "policy_revision": "<版本>",
  "activation_id": "<任务ID>", "session_attempt": 1,
  "session_id": "<hello响应session_id>"
}
```

服务端核验当前会话、任务、UDP回程及TTS初始化就绪后才消费待播任务。MQTT降级到WebSocket时使用相同任务ID及递增attempt，并沿用原WebSocket鉴权；旧连接迟到消息不能覆盖新会话。

待播任务有效期15秒。失位、策略失效、区域变化和建链超时会取消旧任务；只保留最新有效区域。用户正在交互时不插播，过期任务不会在稍后突然补播。`ready`成功只表示协议接受，不保证立刻有音频。

## 旧固件兼容

已有 `device_event/beacon_change` 的两种MAC报文仍可在活动语音会话使用，服务端进行同样的资产准入校验。旧报文无法提供持续扫描和ACK，因此不支持新待机唤醒和位置心跳保证，也不能标记固件已应用策略。

启用管理联动后，位置服务失败不会保存未知信标或播“已进入新区域”。文字和视觉都只使用尚有效的校验位置；未经确认的旧 `last_beacon_id` 不再作为当前位置。

## 固件验收

- 待配置、禁用、报废设备不启用导览；场馆机不能识别外馆信标。
- 完整下载多块策略后才确认；漏块、摘要不符、旧版本不替换当前有效策略。
- 边界RSSI抖动不反复上报；丢失信标后停止位置心跳并发lost。
- MQTT待机可同步策略和上报位置，只有授权唤醒任务才建立语音会话。
- 重复hello不生成双会话；UDP就绪前不开始播音；降级后旧会话不得重复播报。
- 首次只建基准，同区域不重播；新区域仅在策略有效且自动讲解开启时播报。
