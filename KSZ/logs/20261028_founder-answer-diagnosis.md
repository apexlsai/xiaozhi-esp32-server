# 2026-10-28 康硕展负责人问答来源排查

结论：北京时间17:52:44起的三轮“老板/创始人”问答被Agent当作上下文追问，均跳过RAG，工具调用数为0。“刘辉”首次出现在模型生成结果，此前本轮输入历史没有该姓名；错误随后进入历史并被重复引用。

## 请求证据

Agent日志使用UTC；下列09:52:44对应北京时间17:52:44。

```text
2026-10-28 09:52:44 | INFO     | src.api.server:chat_completions:1010 - [RAG-GATE] request_id=req_ae2370938a9b420e route=direct rag_decision=skip reason=contextual_followup router_ms=0.0
2026-10-28 09:52:44 | INFO     | src.services.inference:_log_tools_request:339 - [KSZ-LLM] tools_passthrough_request {"model":"qwen3.7-flash","stream":true,"message_count":33,"tool_count":0,"tool_names":[],"tool_choice":null,"enable_thinking":false,"temperature":0.7,"top_p":null}
{"ts": "2026-10-28T09:52:45.737322+00:00", "request_id": "req_ae2370938a9b420e", "user_preview": "这里的老板是谁？", "route": "direct", "rag_decision": "skip", "reason": "contextual_followup", "rag_chunks": 0, "extra": {"tool_calls": 0, "language": "zh-CN", "handler_route": "tools_completion", "llm_ttft_ms": 663.67, "llm_ms": 862.9}}
2026-10-28 09:52:56 | INFO     | src.api.server:chat_completions:1010 - [RAG-GATE] request_id=req_733eb4703aa54a32 route=direct rag_decision=skip reason=contextual_followup router_ms=0.0
{"ts": "2026-10-28T09:52:57.967241+00:00", "request_id": "req_733eb4703aa54a32", "user_preview": "这里的老板是谁？", "route": "direct", "rag_decision": "skip", "reason": "contextual_followup", "rag_chunks": 0, "extra": {"tool_calls": 0, "language": "zh-CN", "handler_route": "tools_completion", "llm_ttft_ms": 983.26, "llm_ms": 1085.44}}
2026-10-28 09:53:08 | INFO     | src.api.server:chat_completions:1010 - [RAG-GATE] request_id=req_53bfe3caf8364e87 route=direct rag_decision=skip reason=contextual_followup router_ms=0.0
{"ts": "2026-10-28T09:53:10.009623+00:00", "request_id": "req_53bfe3caf8364e87", "user_preview": "呃，这里的创始人是谁？", "route": "direct", "rag_decision": "skip", "reason": "contextual_followup", "rag_chunks": 0, "extra": {"tool_calls": 0, "language": "zh-CN", "handler_route": "tools_completion", "llm_ttft_ms": 1066.21, "llm_ms": 1157.5}}
```

## 姓名第一次出现的位置

```text
261028 17:52:46[0.9.7-00000000000000][core.providers.tts.alibl_stream]-INFO-句子语音生成成功： 门厅，门厅。康硕展的创始人是刘辉先生。
261028 17:52:46[0.9.7-SiOpe5Alnoca39][core.handle.sendAudioHandle]-INFO-发送音频消息: SentenceType.FIRST, 门厅，门厅。康硕展的创始人是刘辉先生。
17:52:44 发往Agent的31条messages：含刘辉=False；含黎忠诚=False；tool角色消息数=0
```

## 同日RAG语料证据

17:53:26追问“雷曼光电的董事长是谁？”时实际执行RAG，17:53:27返回来源《母公司以及创始人》：

- 黎忠诚：语料标注为康硕展创始人、副董事长兼总经理。
- 李漫铁：语料标注为母公司雷曼光电创始人、康硕展董事长。
- 这不能作为前三轮已检索的证据；前三轮的rag_chunks均为0。

## 代码路径

- src/vision/intent.py：宽泛的指代正则匹配“这”，加“谁”将问题归为detail。
- src/api/visual_context.py:66：前文是conversation且没有命名作品时返回VisualTurn("conversation")。
- src/api/server.py:1007：对conversation直接设route=direct、rag_decision=skip，未再判断是否需要查事实。

改进方向：上下文用于解析“这里”指代的公司；人物身份、职务等事实仍需检索核验。模型的历史回答不能作为事实来源。本次仅排查，未修改或部署服务代码。
