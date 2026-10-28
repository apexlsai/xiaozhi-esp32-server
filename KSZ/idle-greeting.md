# 固件触发主动招呼

固件判断空闲并发起请求，服务端沿用设备绑定角色、语言和现有对话，生成 1～2 句自然问候或游玩邀请，经原 TTS 音频链路播放。请求中的触发指令不进入用户对话历史，仅实际生成的助手文本保留供用户接话。请求无需提供文本、设备 MAC、角色或位置，服务端不采信请求指定的这些信息。

## 固件触发规则

- 默认在启动后空闲 120 秒，或用户对话/信标播报结束后空闲 120 秒，触发一次。
- 同一轮空闲仅触发一次。主动招呼结束、网络心跳、信标扫描上报和建链均不重新启用招呼；只有新的用户对话或信标播报结束才重新计时。
- 用户录音、对话生成、音频播放及视觉任务期间不触发。收到 `busy` 后结束本轮尝试，等待新的用户对话或信标播报周期，不定时重试。
- 固件负责空闲时长配置。后端 `.env` 的 `KSZ_IDLE_GREETING_MIN_INTERVAL_MS=120000` 仅限制同一语音会话内两次成功受理的最小间隔，不向固件下发计时参数。修改后重建 server 容器生效。

## 建链与请求

沿用 OTA 返回的已认证 WebSocket 或 MQTT 通道；不增加 HTTP 接口或新的认证凭据。

WebSocket：完成 `hello` 后发送请求，服务端 TTS/LLM 必须已初始化。

MQTT 待机：固件主动建立语音会话，按[现有协议](guide-protocol.md#待机唤醒与播放就绪)在 `hello.guide` 携带 `boot_id`、递增的 `session_attempt`，不带 `activation_id`。收到 hello 响应后，建立当前会话加密 UDP，主动发送有效首包，再发送同一会话的 `guide_control/ready`；收到该 ready 的 `accepted=true` 后发送招呼事件。需要已启用现有导览控制链路并完成策略同步；仅 MQTT 在线或仅 hello 成功均不足以播报。若建链期间已触发信标播报，本轮招呼取消。

### MQTT 握手关键字段

先复用导览协议的 `sync` 下载、校验并确认策略，保存服务端返回的 `policy_revision`；未接入该协议的固件须先实现[策略同步](guide-protocol.md#策略下载和确认)。`boot_id` 每次开机生成，同次启动内重连保持；`seq` 在本次启动内递增，`session_attempt` 在每次新语音建链时递增，普通对话和导览共用编号。

在原 hello 中保留既有音频、版本和 features 字段，增加以下顶层对象，不携带 `activation_id`：

```json
{
  "guide": {
    "boot_id": "boot001",
    "session_attempt": 3
  }
}
```

收到 hello 响应后，使用本次密钥和 nonce 发送一个有效加密 UDP 首包（可为静音帧）建立回程，再通过原 MQTT 上行主题发送：

```json
{
  "type": "guide_control",
  "version": 1,
  "action": "ready",
  "request_id": "boot001-ready-8",
  "boot_id": "boot001",
  "seq": 8,
  "policy_revision": "<同步所得当前策略版本>",
  "session_attempt": 3,
  "session_id": "<当前 hello 返回的 session_id>"
}
```

示例的 `seq`、`session_attempt` 必须替换为固件真实递增值。等待相同 `request_id` 的 `guide_control/result` 返回 `accepted=true`；`sync_required` 或 `policy_revision_mismatch` 按原导览协议重新同步。没有成功 ready 回执时不发招呼事件，重连后也不能复用旧 UDP 回程或旧 ready。

### 招呼事件

```json
{
  "type": "device_event",
  "event": "idle_greeting",
  "request_id": "boot001-greeting-1",
  "session_id": "<当前 hello 返回的 session_id>"
}
```

| 字段 | 约定 |
| --- | --- |
| `type` | 固定 `device_event` |
| `event` | 固定 `idle_greeting` |
| `request_id` | 必填，1～128 字符；同一会话内每次新请求使用新值，例如启动标识加递增计数 |
| `session_id` | 必填，当前 hello 返回的会话 ID，重连后使用新值 |

WebSocket 直接发送 JSON；MQTT 发布到原有设备上行主题，网关通过活动语音桥接透传。不要将事件作为 `listen`、识别文本或用户说出的“你好”发送。

## 响应与播放

响应通过原 JSON 下行通道返回：

```json
{
  "type": "device_event_result",
  "event": "idle_greeting",
  "request_id": "boot001-greeting-1",
  "session_id": "<当前会话 ID>",
  "accepted": true
}
```

`accepted=true` 仅表示受理生成任务，不表示模型生成成功或设备完成播放。随后沿用现有 `tts/start`、`tts/sentence_start`、音频和 `tts/stop` 链路；不会伪造用户 STT 消息。生成失败或内容为空时可能没有 TTS 消息，固件不得因此反复触发。用户开始对话时可按原协议发送 `listen` 或 `abort` 打断，断线及会话被替换后旧任务不再投递音频。

拒绝示例：

```json
{
  "type": "device_event_result",
  "event": "idle_greeting",
  "request_id": "boot001-greeting-2",
  "session_id": "<当前会话 ID>",
  "accepted": false,
  "reason": "busy"
}
```

| `reason` | 含义 |
| --- | --- |
| `invalid_request_id` | 缺少请求 ID、类型错误或长度超限 |
| `session_mismatch` | 请求未携带当前会话 ID；响应中的 ID 为服务端当前会话 |
| `session_not_ready` | hello 未完成、连接不可用、设备未绑定或 TTS/LLM 尚未初始化 |
| `session_not_current` | 请求来自已被替换的语音连接 |
| `audio_not_ready` | MQTT 当前语音会话尚未完成现有 ready/UDP 回程确认 |
| `busy` | 正在录音、对话、播报、调用工具或处理视觉任务，或有待播信标导览 |
| `cooldown` | 距离本会话上一次成功受理不足后端配置间隔 |

同一会话缓存最近 64 个请求结果，包括拒绝结果。网络重发使用相同 `request_id`，返回原受理结果而不重新生成；同一 ID 的重复请求不会因后来空闲而从拒绝变为成功。缓存淘汰后不再保证该 ID 去重，固件不得无限重发。断线后结束本轮尝试，不跨会话补发。

## 固件验收

1. 空闲两分钟只发起一次请求；持续无人回应不重复招呼。
2. 对话结束或信标播报结束重新计时；普通信标扫描和心跳不重置计时。
3. MQTT 待机能完成 hello、UDP 首包、ready，再申请播报；信标播报、用户对话优先。
4. 重发同一请求仅播放一次；拒绝后不持续重试。
5. 播报语言和角色与绑定配置一致，用户可自然接话；录音、打断和断线后不播放迟到问候。

本说明对应[固件接入飞书文档](https://my.feishu.cn/wiki/PBPKwcEKDiTkHLkSf9tcEEHJnJt)。
