# Agent 提示词接管与 20 轮历史

默认 `prompt_owner: xiaozhi`，保持旧链路。目标智能体所选 LLM 配置增加 `prompt_owner: agent` 才启用，优先级高于服务端全局同名配置。智控台的原始角色 `prompt` 原样传给 Agent，不在小智叠加 KSZ 回答、篇幅、定位或新闻政策。工具参数、权限、执行保护、语音清洗继续由代码处理。

```yaml
LLM:
  selected_agent:
    # 保留原有 type、model_name、base_url、api_key
    prompt_owner: agent
```

所选模块名称必须与 `selected_module.LLM` 一致。不要为直接连接其他模型的配置启用；对方必须先实现本接口。不合法的所有者值报配置错误。

## 构建与切换

从项目根目录构建，所有补丁叠加现有新闻修复：

```bash
docker build -f KSZ/Dockerfile-agent \
  --build-arg AGENT_IMAGE=ghcr.io/apexlsai/museum_guide_agent:0.1.6 \
  -t museum-guide-agent:prompt-owner-check .
docker build -f KSZ/Dockerfile-server -t xiaozhi-esp32-server:prompt-owner-check .
```

Agent 默认基底为 `showroom`；`AGENT_IMAGE` 应指向实际使用的基底，构建通过 `git apply --check` 检查兼容性。先发布支持接口的 Agent，再更新服务端并为目标智能体启用配置，最后重新建立设备会话。旧连接中已有的控制消息不会自动迁移。回滚恢复 `prompt_owner: xiaozhi`（或删除该配置）以及原镜像，重新建连。本次只构建隔离验证镜像，没有正式部署。

## 接口

文本请求的 `ksz_prompt_context` 支持 SDK 展开的顶层字段和嵌套 `extra_body`。两处同时存在必须相同；显式 `null`、未知版本、角色缺失或字段类型错误返回验证错误。完全不携带该字段的客户端保持兼容路径。

脱敏示例：

```json
{
  "model": "museum-guide-chat",
  "messages": [{"role": "user", "content": "牛顿第一定律是什么？"}],
  "tools": [{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}],
  "ksz_prompt_context": {
    "version": 1,
    "base_prompt": "<智控台原始角色，未记录>",
    "language": "zh-CN",
    "state": {
      "device_id": "<脱敏>",
      "current_time": "<服务端时间>",
      "device_location": {"source": "unknown", "status": "expired"}
    },
    "turn": {"kind": "user", "tool_depth_exhausted": false}
  }
}
```

`state` 复用已过滤的设备、场馆、定位字段及有效室内位置；过期或失效位置不作为当前位置。版本 1 的事件为 `user`、`narration`、`idle_greeting`，播放中断快照和工具深度上限随本轮状态传递。内部事件不写成用户消息，实际生成的讲解内容仍可进入助手历史。此接口承接已鉴权服务端的可信状态，不替代现有设备权限校验。

Agent 普通问答、工具调用、续答和视觉文字追问共用系统组装入口：下游只有一条最终 `system`，含原始角色、统一规则、语言及本轮状态。工具定义保留在 `tools`，结果保留 `role=tool`；RAG 以明确标记的资料块传入，不放进系统行为规则。视觉证据仍保留专用核验和主题约束。

一般知识、故事、闲聊可以直接回答。场馆归属、指定产品参数和实时事实按证据回答，未知部分明确限定。角色配置本身若包含过度限制，仍需管理员修改原始角色；系统不会悄悄删除管理员角色内容。

## 历史边界

按真实用户消息开始一轮，保留最近 20 轮，包括当前轮；工具递归、并行调用及其结果属于同轮。整轮裁剪，系统与本轮状态不占轮数。小智连接内历史和 Agent 外部客户端输入均执行边界处理，无摘要调用。

请求阶段修复被中断的悬空工具调用，移除无对应调用的孤立结果。模型供应商返回空工具 ID 时，按调用顺序生成非空 ID 并对应结果，避免续答报错；修复不修改调用方原始消息。

## 验收记录

自动回归、真实模型原问题/工具调用/回答、脱敏下游请求结构见 [测试篇](testing.md#agent-提示词接管验收)。链路通过不代表新闻或搜索事实已经独立核实；未验证的事实单独标记。测试使用 `ce:70` 实际角色、模型和工具配置，独立会话，不连接设备、不调用 TTS、不改变生产配置。

## 开放问答与回答自审

接管模式下，服务场馆只决定助手身份，不限制公开知识问答范围。用户纠正“我问的是深圳博物馆”时，应继续讨论深圳，不能改答服务场馆或以身份为由拒答。本馆 RAG 未命中不阻断公开背景回答；普通检索问答可使用实际提供的 `web_search` 核实，权限仅限搜索。

同轮回答自审要求保留能答的部分、限定不确定细节，不增加独立审查模型调用。它是生成规则，不是可靠的事实判定器：真实虚构藏品测试仍有错误，不能宣称能自动区分所有合理失误与胡编，也不能用它作为“一律兜底拒答”的开关。见[复核证据](logs/20261111_shenzhen-museum-diagnosis.md)。
