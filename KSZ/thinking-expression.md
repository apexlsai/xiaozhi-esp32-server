# 请求等待表情

服务端收到文字或 ASR 识别后的用户问题，进入普通 `chat` 处理时，通过现有设备协议先下发：

```json
{"type":"llm","text":"🤔","emotion":"thinking","session_id":"当前会话"}
```

不等待模型生成表情、不增加模型调用、不播放“正在思考”语音，也不写入用户或助手历史。语音识别完成前不显示；自动位置讲解不触发。工具递归沿用当前等待状态，不重复触发。

正文出现原有情绪标记时，切换到正文表情；无标记时，在首段字幕/语音开始前发送 `😶 / neutral`。无正文结束、请求异常及用户打断均清理等待状态；断线取消尚未发送的表情。每条连接按会话和回答标识隔离，旧请求的迟到回调不能重置新回答；共享发送锁处理慢发送及快速连续提问的顺序。

设备声明 `hello.features.emoji=false` 时不发送等待表情。其他设备复用已有 `llm` 消息，MQTT 网关继续 JSON 透传，无需新增协议字段或 Agent 改动；屏幕实际显示依赖固件已有的 `thinking` 表情资源。当前实现不覆盖独立拍照视觉流程的既有等待提示。

实现位于 `KSZ/server/ksz_guide/thinking.py`，`KSZ/patches/thinking-expression.patch` 接入普通问答、正文表情、TTS 开始/结束及打断处理，在 `KSZ/Dockerfile-server` 中叠加现有提示词补丁。构建：

```bash
docker build -f KSZ/Dockerfile-server -t xiaozhi-esp32-server:thinking-expression-check .
```

只需更新 Xiaozhi 服务端；回滚为此前服务端镜像即可。验收结果见[测试记录](testing.md#请求等待表情)。
