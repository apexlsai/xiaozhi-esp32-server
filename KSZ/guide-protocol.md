# 导览控制协议 v1

本协议用于 management 与 xiaozhi 联动版本，须部署服务并开启 `KSZ_GUIDE_ENABLED=1`。固件由独立团队实现；本文定义接入契约，不表示设备或线上服务已经升级。现有语言、MCP、视觉与音频协议保持原约定。

讲解被用户打断后的播放确认使用独立的 `hello.features.playback_progress` 与 `playback_progress` 消息，段号和确认时机见[讲解模式与播放进度](narration-playback.md#固件播放回执)。未声明能力时沿用原有 TTS 消息。

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

每5秒发送 `action=heartbeat`，其他信封字段一致，并携带当前仍稳定观测到的 `beacon`。仅设备在线但未观测到信标时，不得补报历史信标。确认丢失发送 `action=lost`（不带beacon）；服务端默认15秒未获得有效位置确认也会失位，可通过 `KSZ_GUIDE_POSITION_TTL_MS` 调整。

management 目录提供 `place_id`、`place_name`、`place_beacon_count`（同位置启用信标总数，独立于分页和设备授权子集）。数量仅作资产统计，不决定播报条件；一个位置绑定多枚信标时，任意一枚有效上报即可识别并触发讲解，无需双信标、成员跳变或确认窗口。同位置持续上报和成员切换不重播，其他位置已开始讲解后才允许返回重新触发，并受默认60秒同位置冷却限制。未绑定位置的旧资产继续按独立信标处理；位置变化不修改设备固定归属。

默认允许信标变化打断正在生成或播放的讲解，但必须满足15秒最小讲解间隔、同位置重复讲解间隔及用户交互优先等条件。冷却期间继续旧讲解，只保留最新有效位置的待播任务；切换不会重置冷却，到期时任务仍须有效。固件的稳定判断和自身冷却保持原实现，旧报文同样获得服务端防重复保护，不要求新增扫描候选字段。

新协议策略中的 `auto_announce` 同时受设备授权与 `KSZ_GUIDE_AUTO_ANNOUNCE_ENABLED` 约束；`filter.min_announce_interval_ms` 为服务端 `KSZ_GUIDE_MIN_ANNOUNCE_INTERVAL_MS` 的有效值，参与策略摘要。其他服务器端讲解规则见[配置说明](guide-management.md#信标讲解规则)，RSSI 与扫描判定仍按原策略执行。

结果消息使用 `action=result`，含 `request_id`、`seq`、`accepted` 和 `reason`。未知、跨馆、停用或冲突信标不更新位置、不触发语音。`policy_revision_mismatch` 时同步新策略；`resolve_rate_limited` 时按正常稳定观测节奏重试，不能快速循环请求。

完整策略确认后，成功结果可携带轻量续期信息：`lease: {policy_revision, policy_sha256, expires_at}`。固件仅在这三个字段有效，且版本与摘要均匹配本地已原子应用的策略时更新到期时间；没有匹配的lease就不续期。无需重复下载相同清单。拒绝、未完成ACK或授权过期的结果不提供续期；重复请求也重新核验当前租期。无位置信标时仍每5秒发送不带beacon的heartbeat维持策略同步，但不延长位置有效期。

## 活动会话的位置显示

设备在 `hello.features` 中声明 `"location_updates": true` 后，服务端先完成 `hello` 响应，再通过同一活动连接发送当前位置快照。WS 直连直接接收 JSON；MQTT+UDP 会话由网关将此 JSON 原样转发至设备订阅主题，音频仍走原链路。未声明能力的设备不接收此消息。

```json
{
  "type": "guide_control",
  "version": 1,
  "action": "location",
  "session_id": "<hello响应session_id>",
  "location_revision": 1,
  "status": "located",
  "expires_in_ms": 15000,
  "location": {
    "beacon_id": "<管理端业务编号>",
    "beacon_mac": "DA:01:16:00:08:57",
    "venue_id": "<场馆ID>",
    "floor": "1F",
    "area": "青铜器展区",
    "location_description": "展柜旁"
  }
}
```

`status` 为 `located`、`unknown` 或 `lost`。未定位或已失位时 `location=null`、`expires_in_ms=0`，设备立即清空当前位置。楼层、区域或位置描述可为空，但至少一项有内容才作为可显示位置下发；位置字段来自管理端登记资产，不采用设备上报的显示文本。

首次有效定位、有效位置切换、同位置有效上报续期、策略撤销和位置过期均更新显示，不受自动讲解开关或播报冷却限制。同位置任意一枚有效信标均可确认或续期。旧 `beacon_change` 与新 `observation` / 带信标的 `heartbeat` 共用服务端校验；无效、跨馆、未授权上报不能覆盖有效位置。位置通知不进入 Agent、对话历史、TTS 或音频队列，发送异常不阻塞位置处理和讲解。

`location_revision` 是当前 `session_id` 内从 1 开始递增的通知序号，续期也递增；它独立于导览运行时的信标变化序号。设备只接受当前连接、当前会话且序号严格递增的消息。断线或换绑时清空显示和计时器；新会话重新计数并接收快照，旧连接迟到消息不得恢复位置。

`expires_in_ms` 为发送时剩余位置有效期，取 `KSZ_GUIDE_POSITION_TTL_MS` 和当前授权租期的较小剩余值。每次有效续期重设本地过期计时；超时即清屏，即使没有收到 `lost` 也不继续显示旧位置。重连快照只下发剩余时长，不重新获得完整有效期。模拟器的“切换信标”是单次上报，线上配置为 15000ms 时约 15 秒失效，需要再次上报真实观测才续期。

本功能只覆盖已有活动会话，不因显示位置单独唤醒语音连接；MQTT 待机无会话的位置显示推送不在此能力范围内。

## 待机唤醒与播放就绪

固件也可在空闲时主动建链，完成本节 hello/UDP/ready 握手后发送 `device_event/idle_greeting`，由后端生成角色问候；请求、回执及固件计时规则见[主动招呼接口](idle-greeting.md)。

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

收到hello响应后按既有密钥、nonce建立UDP并发送有效首包建立NAT回程。

首包必须在握手后主动发送，不等待用户说话；可发送符合当前会话加密、序号和音频格式的静音帧。每次新语音会话都要重新建立回程，不能复用旧会话的就绪状态。MQTT在线和hello成功本身不表示音频可播放。随后发送：

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

待播任务默认有效期15秒，可通过 `KSZ_GUIDE_PENDING_TTL_MS` 调整，设备以 `request_hello.expires_in_ms` 为准。失位、策略失效、有效位置变化和建链超时会取消旧任务；同位置成员切换保留任务且不延长期限。用户正在交互时不插播，过期任务不会在稍后突然补播。`ready`成功只表示协议接受，不保证立刻有音频。

## 旧固件兼容

已有 `device_event/beacon_change` 的两种MAC报文仍可在活动语音会话使用，服务端进行同样的资产准入校验。旧报文无法提供持续扫描和ACK，因此不支持新待机唤醒和位置心跳保证，也不能标记固件已应用策略。

旧固件若等到首次录音才发送UDP，会出现此前自动讲解已有文字但无声、普通对话后才有声音的现象。需要在固件握手流程中补齐UDP首包；先普通对话只能临时建立本次会话的回程。

启用管理联动后，位置服务失败不会保存未知信标或播“已进入新区域”。文字和视觉都只使用尚有效的校验位置；未经确认的旧 `last_beacon_id` 不再作为当前位置。

信标自动讲解的第一句由服务端按语言模板直接生成，中文默认“您现在位于{location}。”，后续为模型生成的介绍。两部分属于同一播报轮次，固件继续按现有 `tts/start`、`tts/sentence_start` 和音频处理，无需增加消息类型；内部位置提示仍不回传为 `stt`。

## 固件验收

- 待配置、禁用、报废设备不启用导览；场馆机不能识别外馆信标。
- 完整下载多块策略后才确认；漏块、摘要不符、旧版本不替换当前有效策略。
- 边界RSSI抖动不反复上报；丢失信标后停止位置心跳并发lost。
- MQTT待机可同步策略和上报位置，只有授权唤醒任务才建立语音会话。
- 重复hello不生成双会话；UDP就绪前不开始播音；降级后旧会话不得重复播报。
- 任意位置默认首次有效信标即触发，即使绑定多枚也无需第二枚确认。同位置重复或成员切换不重播，不同位置可独立触发，仍需策略有效、自动讲解开启并满足冷却。
