# 图片会话关联

KSZ 向 Museum Guide Agent 的拍照和语音请求传递同一个会话标识及当前图片引用。原图、检索候选和核验结果由 Agent 缓存；小智继续管理聊天文字、设备工具与播报。实现位于 `server/ksz_guide/visual_context.py`，HTTP 接入由 `patches/vision-context.patch` 在构建时应用。

## 请求字段

下列字段均位于 Agent `/v1/chat/completions` 的 JSON 顶层。文字 Provider 通过 OpenAI SDK 的 `extra_body` 展开；视觉 Provider 直接写入 JSON。

| 字段 | 含义 |
| --- | --- |
| `device_id` | 已认证的设备 MAC，沿用现有字段 |
| `session_id` | 小智当前连接的会话 ID，语音和视觉共用，重连改变 |
| `image_id` | 服务端生成的图片 UUID，与设备的视觉 `request_id` 分开 |
| `image_seq` | 本连接内从 1 开始递增，在已认证的上传请求开始时分配；无效请求可能占用序号 |
| `captured_at` | Unix 秒；旧设备未提供可信拍摄时间，以服务端开始接收上传的时间代替 |

新会话的普通语音请求只有 `device_id` 和 `session_id`，没有图片字段。收到图片后，视觉请求增加后三项字段；图片仍以最后一条 user 消息的 `image_url` 传递。视觉请求前置最近最多 6 条普通 user/assistant 文字消息，总计不超过 4000 字符、单条不超过 2000 字符；排除工具调用、工具结果、临时示例和历史图片，并去掉末尾重复的本次问题。这里不把旧回答升级为事实或馆藏身份。

成功完成的图片在后续语音请求中引用相同的 `image_id`、`image_seq` 和 `captured_at`，不重复传 Base64，不把 Agent 的结构化证据加入口播。新照片替换旧照片，旧上传即使更晚完成也不能重新激活图片或返回成功的旧内容。

## 生命周期与失效

默认开启，独立于信标导览开关：

```dotenv
KSZ_VISION_CONTEXT_ENABLED=1
KSZ_VISION_CONTEXT_TTL_SECONDS=180
```

时间从上传请求开始计算，不因追问刷新；必须为正有限数。Agent 的 `VISION_CONTEXT_MAX_AGE` 应与此值一致，默认 180 秒。相关追问最多 3 轮、话题判断和是否重拍由 Agent 负责，KSZ 不重复计算工具递归或追问轮数。

- 上传中、失败、被取消或已过期：后续请求使用该次上传独立的无效引用 UUID，不再携带可命中原图的 ID。该 UUID 从不作为图片上传给 Agent。
- 无效引用仍带当前 `session_id`、序号与时间。Agent 必须严格按请求 `image_id` 匹配：查不到时返回无图片上下文，不能回退到“设备最近一张”或“会话最近一张”。直接省略 ID 会触发现有 Agent 的最近图片回退，所以不能用省略字段表示失效。
- 视觉正文完整返回后，仅打断 TTS 不清除成功图片；识别尚未完成就打断时，不启用半截结果。
- 断开连接清除小智引用；Agent 旧缓存由其 TTL 回收，新连接使用新的 `session_id`，不跨会话恢复。
- 上传读取、配置读取、生成期间发生换图、换轮或重连时，旧请求不能提交图片引用。Agent 同时须用 `image_seq` 保护自己的缓存，拒绝旧序号覆盖新图片。

## Agent 接入与兼容

Agent 以 `device_id + session_id` 隔离缓存，使用 `image_seq` 仲裁并发，严格匹配 `image_id`。根据同一图片的核验状态、候选 ID 和用户追问决定重新看图、限定展品检索或重新拍照。失效图片不用于识别眼前物品；已明确名称的作品仍可作为文字话题查询。

Agent 需将视觉 `messages` 中的历史实际用于指代解析；仅解析后丢弃 `_history` 无法解决拍照请求中的“刚才那个”等表达。小智只负责传递，不代替 Agent 改写问题或确认身份。

客户端上传接口无需新增字段，支持 MCP、手动 push、旧端完整返回及 return 模式；无在线连接的 HTTP 请求沿用无会话处理。客户端不能自行指定服务端的 `session_id`、`image_id` 或序号。

旧 Museum Agent 忽略扩展字段时，原拍照与文字流程继续工作，但不会获得跨轮看图能力。其他不接受扩展字段的模型端可用 `KSZ_VISION_CONTEXT_ENABLED=0` 回退。部署需同步升级 Agent 与 KSZ server，并重新创建服务容器；本次源码改动不自动部署。

## 验证

`server/tests/test_visual_context.py` 覆盖实际 HTTP 处理与 Provider 载荷、历史裁剪、换图和慢上传乱序、失败与取消、已完成识别后的语音打断、TTL、跨设备和重连隔离。须先在临时源码副本中按 `Dockerfile-server` 的顺序应用全部构建补丁，再运行该文件及现有视觉、导览回归。

联调时检查：首次拍照与后续“这是馆里的什么展品”使用同一会话和图片 ID；“重拍”使序号递增；失败、过期与新会话不会读取旧图。馆藏命名是否正确仍需使用实际《兰石图》样本验证 Agent 的核验、检索与回答策略。
