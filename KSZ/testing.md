# KSZ 测试篇

以下命令在仓库根目录 `/apps/xiaozhi-esp32-server` 执行。推荐使用 `uv`，当前 Conda `(base)` 无需退出；依赖在隔离环境安装。

## 单次请求延时测试

入口为 `scripts/request_latency.py`，两种模式分别为：

| 模式 | 行为 | 用途 |
| --- | --- | --- |
| `logs` | 读取已有服务端、MQTT 网关和 Agent 日志 | 回看已完成的请求，不产生新请求 |
| `request` | 从 OTA 获取配置，连接原生 MQTT+UDP 并发送一次请求 | 测量实际控制消息和 UDP 音频首包，可结合内部日志 |

详细参数、指标口径及 2026-10-13 的 10 轮语音测量见[请求延时诊断](request-latency.md)。

### 日志分析

默认读取最近 30 分钟，选择最后一次匹配的问题：

```bash
uv run KSZ/scripts/request_latency.py logs --query '今天天气怎么样？'
```

回看指定时间之后的请求：

```bash
uv run KSZ/scripts/request_latency.py logs --query '今天天气怎么样？' --since '2026-10-13T13:59:00+08:00' --json-out /tmp/weather-history.json
```

直接用 `python` 执行 `logs` 也可以，此模式本身只需标准库。容器日志读取要求当前用户具有 Docker 权限；离线分析可指定 `--server-log`、`--mqtt-log`、`--agent-log`。

### 发送一次请求

将示例 MAC 换成已绑定智能体的专用测试设备。新连接可能替换同一身份下的原模拟器/设备连接。`--with-logs` 适用于 Docker 宿主机；远程运行时省略。

天气：

```bash
uv run KSZ/scripts/request_latency.py request --ota http://127.0.0.1:8002/xiaozhi/ota/ --device aa:bb:cc:dd:ee:ff --scenario weather --expect-tool get_weather --with-logs --json-out /tmp/weather-latency.json
```

普通问答：

```bash
uv run KSZ/scripts/request_latency.py request --ota http://127.0.0.1:8002/xiaozhi/ota/ --device aa:bb:cc:dd:ee:ff --text '给我说一声谢谢' --with-logs --json-out /tmp/chat-latency.json
```

其他测试使用相同入口，更换输入或场景参数：

| 测试 | 参数 | 说明 |
| --- | --- | --- |
| 新闻查询 | `--scenario news --expect-tool get_news_from_chinanews` | 验证实际发生新闻工具调用 |
| 设备 MCP | `--mcp --scenario mcp --expect-tool self.get_device_status` | 上报只读模拟状态工具，返回音量 50、亮度 80 |
| 音频输入 | `--audio /tmp/question.wav` | 经过上行音频和 ASR，需要带 `libopus` 编码器的 `ffmpeg` |

文本模式跳过采集、上行音频和 ASR。`--mcp/--no-mcp` 控制设备 MCP 能力，天气和新闻是智能体配置的服务端工具；场景参数选择提示词，不强制模型调用工具。`--expect-tool` 在没有观察到指定调用时返回失败。

重复测试时使用不同的 `--json-out` 文件名，否则后一轮会覆盖前一轮。Shell 中 `>` 表示输出重定向，不能用来续行；上述命令可以整行复制，多行命令应在行末用 `\`，不用复制终端显示的 `>` 提示符。

### 验收与统计口径

- 实际出现预期工具调用、收到 `tts:start`、UDP 音频及 `tts:stop`，并检查退出码；只收到控制消息不代表音频送达。
- 主要体验指标为 `submit_to_audio_ms`，即请求提交到 UDP 音频首包。`submit_to_stop_ms` 包含完整播报时长，两者分别统计。
- `llm_ttft_ms` 是模型首个流式块，可能是角色或工具增量；`llm:🙂` 是表情通知，两者都不能当作正文首字。
- 工具执行/衔接和模型首包到首句埋点为估算。历史“发送第一段语音”可能携带空音频字幕，不能据此宣称音频已发送。
- 当前脚本绕过浏览器桥接，不含解码与扬声器播放。内部公共日志缺少全链路请求标识，繁忙环境应隔离测试流量。
- 冷查询和缓存命中分别报告；不要用两条样本推断 P95。对相同输入收集多轮，再比较中位数、尾部延时和失败率。

## 天气请求实测（2026-10-14）

以下为用户在宿主机连续执行两次 `request --scenario weather --expect-tool get_weather --with-logs` 的结果。输入均为文本“今天深圳天气怎么样？”，两次都实际调用了天气工具，收到完整 TTS 启停及 UDP 音频。

| 指标 | 第一次 | 第二次 |
| --- | ---: | ---: |
| OTA | 14.66ms | 14.04ms |
| MQTT 建连/鉴权 | 1.63ms | 1.44ms |
| hello 握手 | 32.81ms | 20.09ms |
| 提交→首字幕 | 4926.83ms | 2474.15ms |
| 提交→首 UDP 音频 | **5002.56ms** | **2533.50ms** |
| 第一轮模型首包 / 全程 | 1040.08 / 1356.91ms | 1058.39 / 1326.51ms |
| 天气工具执行及衔接（估算） | **2253.47ms** | **0.84ms** |
| 第二轮模型首包 / 全程 | 480.95 / 764.66ms | 416.45 / 867.96ms |
| 末轮模型首包→首句埋点（估算） | 793.54ms | 692.48ms |
| 首字幕→首 UDP 音频 | 75.73ms | 59.35ms |
| 提交→TTS stop | 12085.34ms | 12016.21ms |
| 下行音频包数 | 117 | 157 |
| 收到的首末序列之间缺失数 | 0 | 0 |

第二次首音频等待减少 2469.06ms，约49%。主要差异发生在天气工具阶段；第一轮模型耗时基本一致。两次回答长度不同，约12秒的 TTS 结束时间包含播报，不等于约12秒的响应等待。下行缺失数为首末收到序列之间的统计，不证明首尾包完全没有丢失。

### 天气缓存来源

天气工具位于 Xiaozhi 后端 `main/xiaozhi-server/plugins_func/functions/get_weather.py`，按 `full_weather_{location}_{lang}` 读取/写入完整天气报告。缓存由 `core/utils/cache/manager.py` 的进程级 `cache_manager` 管理，保存在 Python 内存中，同进程设备可共享，进程重启会清空。默认有效期在 `core/utils/cache/config.py` 中设为28800秒，即8小时。

它缓存天气数据，不缓存 LLM 回答，也不存 Redis。未命中时工具先查询城市，再获取并解析天气页面；命中后直接返回报告，第二轮 LLM 和 TTS 仍会执行。第二次工具耗时0.84ms高度符合缓存命中，但当前无明确的命中埋点，只能作为推断。模型供应商是否另有提示词缓存也无法由这些指标确定。

缓存默认8小时，不能把命中后的报告直接认作刚刚获取的实时天气。后续需单独评估数据新鲜度，并为测试增加命中状态和数据更新时间；本次没有修改缓存策略。

### 优化节点

1. 冷查询：拆分城市查询与天气页面获取，定位工具阶段约2.25秒的等待。
2. 缓存命中后：第一轮模型仍需约1.33秒，可作为主要优化对象，评估工具选择流程与模型首包。
3. 分句与 TTS：末轮首包到首句埋点约0.69–0.79秒，需增加正文首字、首句提交、TTS 建连、供应商首音频和编码完成埋点才能准确拆分。

当前证据只支持链路成功、延时和回答文字基本一致；未核对原始工具结果或实际听音，不能据此验收天气事实准确性和真实设备播放质量。

## 脚本隔离回归

```bash
uv run --with-requirements KSZ/scripts/latency-requirements.txt python -m unittest discover -s KSZ/scripts -p test_request_latency.py -v
```

8项测试覆盖日志匹配、重复问题取最后一轮、Agent 去重、多轮工具衔接、并发归属保护、时间过滤、日志缺失、Ogg 校验，以及本机模拟 OTA/MQTT/加密 UDP 的 MCP、音频上行和缺音频失败。测试只监听本机回环端口，不请求真实模型或操作实际设备。

其他 KSZ 构建补丁的验证按 [README](README.md) 对应功能节执行；保持在隔离目录应用补丁，不直接改写工作区上游源码。
