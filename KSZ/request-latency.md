# 单次请求延时诊断

入口为 `scripts/request_latency.py`。以下命令均在仓库根目录执行。无需重建镜像或修改服务配置。

可整行复制的 uv 命令、2026-10-14 连续两次天气请求实测、缓存来源和验收规则见 [KSZ 测试篇](testing.md)。

## 分析已经完成的请求

在 Docker 宿主机执行，默认读取最近 30 分钟，选择最后一次匹配的问题，标点差异会被忽略：

```bash
python KSZ/scripts/request_latency.py logs --query '今天天气怎么样？'
```

分析更早的请求并保存结构化结果：

```bash
python KSZ/scripts/request_latency.py logs \
  --query '今天天气怎么样？' \
  --since '2026-10-13T13:59:00+08:00' \
  --device 2e:b8:76:52:f4:60 \
  --json-out /tmp/weather-latency.json
```

此模式只用 Python 标准库。默认容器名为 `xiaozhi-esp32-server`、`ksz-xiaozhi-mqtt-gateway-1`、`docker-museum-guide-agent-1`；可用 `--server-container`、`--mqtt-container`、`--agent-container` 覆盖。需要读取 Docker 日志的权限。

也可读取离线日志；日志须通过 `docker logs --timestamps` 导出，不能用只有秒级时间的浏览器日志替代：

```bash
python KSZ/scripts/request_latency.py logs \
  --query '给我说一声谢谢' \
  --server-log /tmp/server.log \
  --mqtt-log /tmp/mqtt.log \
  --agent-log /tmp/agent.log
```

## 发起一次原生 MQTT+UDP 请求

推荐使用 `uv run`。脚本内已声明 Python 版本和依赖，首次运行自动安装到隔离环境，不影响当前 Conda `(base)`。音频模式另外需要带 `libopus` 编码器的 `ffmpeg`：

```bash
uv run KSZ/scripts/request_latency.py --help
```

使用已经绑定智能体的专用测试 MAC。同一个 MQTT 身份的新连接可能替换原设备连接。脚本从 OTA 获取凭据，不打印密码、UDP key/nonce 或视觉 token，不修改智能体绑定。

普通文本请求，默认不声明设备 MCP 能力：

```bash
uv run KSZ/scripts/request_latency.py request \
  --ota http://127.0.0.1:8002/xiaozhi/ota/ \
  --device aa:bb:cc:dd:ee:ff \
  --text '给我说一声谢谢' \
  --with-logs --json-out /tmp/chat-latency.json
```

天气场景，要求实际观察到天气工具调用；默认提示词为“今天深圳天气怎么样？”，可用 `--text` 指定城市：

```bash
uv run KSZ/scripts/request_latency.py request \
  --ota http://127.0.0.1:8002/xiaozhi/ota/ \
  --device aa:bb:cc:dd:ee:ff \
  --scenario weather --expect-tool get_weather --with-logs
```

设备 MCP 场景：只上报只读 `self.get_device_status` 工具，返回模拟音量 50、亮度 80，不操作真实设备。未调用预期工具会返回失败：

```bash
uv run KSZ/scripts/request_latency.py request \
  --ota http://127.0.0.1:8002/xiaozhi/ota/ \
  --device aa:bb:cc:dd:ee:ff \
  --mcp --scenario mcp --expect-tool self.get_device_status --with-logs
```

音频输入覆盖采集文件之后的完整链路：

```bash
uv run KSZ/scripts/request_latency.py request \
  --ota http://127.0.0.1:8002/xiaozhi/ota/ \
  --device aa:bb:cc:dd:ee:ff \
  --audio /tmp/question.wav --with-logs --json-out /tmp/audio-latency.json
```

脚本转成 16kHz/单声道/60ms Opus，按实时速度上传，并在 `listen stop` 声明末序列。编码末帧可能补齐；报告输入时长按发送帧数计算。文本模式用 `listen detect` 跳过采集、上行音频和 ASR，适合与同一句录音对比。

`--scenario news` 选择新闻提示词；`--mcp/--no-mcp` 只控制设备 MCP 能力。天气、新闻属于智能体已配置的服务端工具，场景参数选择问题，不强制模型调用或关闭这些工具；`--expect-tool` 验证实际发生的调用。需要测试完全禁用服务端工具时，使用预先配置好的专用测试智能体。

每次运行默认把本次简要结果写入 `KSZ/logs`；只想看终端输出时加 `--no-log`。需要完整结构化数据时使用 `--json-out`。`--with-logs` 在请求结束后读取宿主机日志，补充内部阶段；远程执行时省略。`--mqtt-host 127.0.0.1 --udp-host 127.0.0.1` 可在宿主机绕过 OTA/hello 公网主机进行本机对比，端口沿用服务配置。OTA 使用同一入口，不会被这两个参数覆盖。默认 90 秒总预算，可用 `--timeout` 修改；预算包含 OTA、握手、上传和回复。

## 指标口径

| 指标 | 含义 |
| --- | --- |
| `submit_to_audio_ms` | 文本提交或音频 `listen stop` 发出，到收到并解密首个 UDP 音频包 |
| `submit_to_sentence_ms` | 提交到首个 `tts:sentence_start`，用于和音频首包比较 |
| `submit_to_stop_ms` | 提交到 `tts:stop`，包含完整播报发送时长 |
| `asr_ms` | 服务端 `ASR输入音频` 到 `识别文本`，包含 ASR 前后处理 |
| `rag_ms` | Agent 检索耗时，包括请求中的检索处理 |
| `llm_ttft_ms` | Agent 调用上游模型到首个流式块；首块可能只是角色/工具增量，不保证已有正文 |
| `llm_ms` | 该轮模型流全程；天气/新闻会出现工具前后两轮 |
| `execution_and_resume_ms_estimate` | 服务端工具执行开始到下一轮 LLM 请求，包含结果处理与调度，非纯 HTTP 时间 |
| `last_llm_first_chunk_to_sentence_ms_estimate` | 根据 Agent 完成时间和内部指标估算末轮首块到首句发送埋点；含正文生成、分句、TTS 建连/处理与调度，不代表音频首包 |
| `gateway_forward_to_asr_input_ms` | 网关上行统计埋点到服务端 ASR 输入；不包含 UDP 尾包等待和公网传输 |

客户端指标用同一个 `monotonic` 时钟；内部日志用服务端时间。不要把浏览器秒级时间与容器时间相减。模型流与 TTS 可以重叠，阶段全程不能直接相加。UDP 收包不代表解码和扬声器已经播放；脚本绕过浏览器及 Python 本地桥接，无法测量它们的缓冲延时。

历史日志的“发送第一段语音”可能携带空音频：当前 `alibl_stream` 收到句子开始事件后先提交 `(SentenceType.FIRST, [], text, sentence_id)`，实际 Opus 随后通过 `MIDDLE` 消息发送。因此离线模式只报告 `chat_to_first_sentence_ms` / `asr_input_to_first_sentence_ms`，真实首音频必须看请求模式的 `submit_to_audio_ms`。

全链路目前缺少统一 request_id。Agent 记录按实际问题、时间窗及自身 request_id 匹配、去重；ASR 和工具的公共埋点按相邻时间关联。检测到其他会话插入时跳过无法归属的指标。问题被 Agent 日志截断、日志缺失或版本不同会显示未知；繁忙环境应隔离测试流量。脚本输出实测与估算，不将未知值填成零。

退出码 `0` 表示本轮收到 TTS 启停和 UDP 音频，且满足指定工具断言；`1` 表示超时、连接/协议失败、缺音频、预期工具未调用或请求的内部日志分析失败。JSON 保存当前指标与提示。UDP 音频先于 MQTT 启动通知到达只报告跨通道时序现象，不能独自证明服务端发送顺序错误。

## 本次测试结果（2026-10-13）

下表来自 14:00–14:04 测试对应的容器高精度日志。单位为毫秒，末列从服务端提交 ASR 输入到首句发送埋点，不含客户端采集、公网上行及下行播放，也不证明首音频已经发送。只统计已关联的 10 个语音请求。

| 请求 | ASR | RAG | 模型首包（每轮） | 工具执行/衔接估算 | 末轮首包→首句埋点估算 | ASR 输入→首句埋点 |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| 笑话 | 842 | 1026 | 1010 | — | 858 | 3762 |
| 汽车品牌 | 521 | 279 | 1070 | — | 678 | 2575 |
| 谢谢 | 378 | 361 | 1066 | — | 428 | 2260 |
| 看看石像 | 407 | 1056 | 1475 | — | 796 | 3761 |
| 厕所位置 | 399 | 462 | 1006 | — | 749 | 2650 |
| 天气 | 312 | 0 | 914 / 708 | 724 | 729 | 3655 |
| 动物园 | 414 | 303 | 1127 | — | 319 | 2206 |
| 最新新闻 | 436 | 0 | 978 / 2301 | 340 | 747 | 5022 |
| 娱乐新闻 | 426 | 0 | 1046 / 671 | 297 | 741 | 3516 |
| AI 动态 | 436 | 274 | 1057 | — | 941 | 2740 |

10 轮网关上行均记录 `缺失=0`，收到帧数与设备声明一致。网关结束转发到 ASR 输入为约 0.6–0.9ms，现有证据不支持优先优化该转发阶段；尚未测出公网传输、尾包等待及播放缓冲的独立耗时。

建议按以下顺序做独立对比实验，再决定实施：

1. **简单问答的 RAG 路由**：“谢谢”“汽车品牌”和“笑话”都走 `route=rag`。检索分别增加 361、279、1026ms；可测试将这类通用问题路由到直接回答的效果。馆藏、设施位置问题继续需要可信检索。只依据这些样本估算可减少对应检索时间，实际收益需对照请求验证。
2. **模型首包和工具后二次生成**：一般首包约 1.0–1.5 秒。最新新闻的工具阶段只有 340ms，第二轮模型首包却有 2301ms，优先对比模型/推理入口、提示词大小、上下文和缓存命中；不要先认定新闻接口慢。天气还有 724ms 的工具阶段可独立排查。
3. **首句生成与 TTS**：末轮模型首包到首句发送埋点为 319–941ms。要定位纯 TTS 瓶颈，还需增加“正文首字、首句提交、TTS 建连完成、供应商首音频、编码完成”埋点，并用请求模式测真实 UDP 首包；目前不能把这段全算成 TTS 服务延时。先对比已有流式 Provider 的冷/热连接。
4. **ASR**：本次 312–842ms，小于主要识别后等待。保留同一句音频作对照，再比较已配置的 ASR 供应商、连接复用和冷/热请求，避免更换识别器后只比较不同长度录音。

MCP 本次只出现初始化/工具发现，没有实际 `tools/call`，无法推断其调用延时。`llm:🙂` 是表情通知，不是模型首字计时点。长回复的 `tts stop` 包含播报时长，例如 AI 回复较长，应与“首音频等待”分别看。打断后的重复 `tts stop`、残留音频属于另一个协议问题，不应混入正常请求的性能样本。

## 隔离验证

```bash
uv run --with-requirements KSZ/scripts/latency-requirements.txt python -m unittest discover \
  -s KSZ/scripts -p test_request_latency.py -v
```

覆盖重复问题取最后一轮、Agent 去重、多轮工具调用、并发归属保护、日志缺失、Ogg 校验，以及本机模拟 OTA/MQTT/加密 UDP 的文本、MCP、音频上行和缺下行音频失败。模拟测试不调用真实模型，也不代表真实设备播放效果。
