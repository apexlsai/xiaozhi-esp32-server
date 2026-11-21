import asyncio
import math
import os
import re
from dataclasses import dataclass

import httpx


DEFAULT_EXAMPLES = {
    "weather": [
        "今天的天气怎么样",
        "萍乡明天会下雨吗",
        "查一下广州现在的气温",
        "查一下深圳天气",
        "帮我查天气",
        "未来几天北京天气如何",
    ],
    "yuanbao_search": [
        "用元宝搜索最近的新闻",
        "请用元宝查一下这个事件的最新进展",
        "元宝搜一下这个公司的情况",
        "用腾讯元宝联网核实这个消息",
        "用元宝搜索一下黄仁勋离开英伟达的新闻",
        "用元宝搜索一下最近的国际新闻",
    ],
    "web_search": [
        "联网搜索这个问题",
        "上网查一下最新消息",
        "搜索一下这个活动什么时候举行",
        "帮我检索这个事件的实时进展",
        "帮我搜一下今天的新闻",
        "查一下最近的新闻",
        "今天有什么新闻",
        "看一下今天的娱乐圈新闻",
        "深圳的APEC会议结束了吗",
        "搜索一下深圳市康硕展人工智能科技有限公司",
        "搜索一下黄仁勋离开英伟达的新闻",
    ],
    "take_photo": [
        "拍张照片看看这是什么",
        "请拍照识别眼前的展品",
        "拍一下当前看到的东西",
        "帮我拍照分析这个物品",
    ],
    "continue_fixed_guide": [
        "继续刚才的讲解",
        "接着播放未完成的内容",
        "继续介绍这个展品",
        "刚才没讲完，请继续",
    ],
    "museum_rag": [
        "这个展品是什么年代的",
        "介绍一下眼前的文物",
        "这个展厅有什么历史背景",
        "讲讲这件藏品的用途",
    ],
    "general_chat": [
        "你好",
        "给我讲个笑话",
        "你是谁",
        "我想和你聊聊天",
    ],
}

TOOL_NAMES = {
    "weather": "get_weather",
    "yuanbao_search": "yuanbao_search",
    "web_search": "web_search",
    "take_photo": "self_camera_take_photo",
}


@dataclass(frozen=True)
class IntentMatch:
    name: str
    score: float
    margin: float


def _number(value, default):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _normalize(vector):
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 0:
        return None
    return [value / length for value in vector]


def _cosine(left, right):
    return sum(a * b for a, b in zip(left, right))


def extract_weather_location(text):
    match = re.search(
        r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z·\- ]{0,24}?)(?:的)?(?:天气|气温|温度|降雨|下雨|预报)",
        text,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(r"(?:weather|temperature)\s+(?:in|for)\s+([A-Za-z][A-Za-z .'-]{0,30})", text, re.IGNORECASE)
    if not match:
        return None
    location = re.sub(r"^(?:今天|明天|现在|当前|请|帮我|查一下|查询|看看)\s*", "", match.group(1), flags=re.IGNORECASE)
    location = location.strip(" ，,。.!！？?")
    return location or None


def extract_search_query(text):
    query = re.sub(
        r"^\s*(?:请|帮我|麻烦)?\s*(?:用(?:腾讯)?元宝|元宝|联网|上网|网络)?\s*(?:搜索|搜|查|查询|检索)(?:一下|一搜|一查|下)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return query.strip(" ，,。.!！？?") or text.strip()


def explicit_tool_name(text):
    normalized = re.sub(r"\s+", "", text or "")
    if re.search(r"天气|气温|温度|降雨|下雨|预报", normalized):
        return "weather"
    if "元宝" in normalized and re.search(r"搜索|搜|查|查询|检索", normalized):
        return "yuanbao_search"
    if re.search(r"联网|上网|网络搜索|网页搜索", normalized):
        return "web_search"
    if re.search(r"搜索|搜一下|搜一搜|搜下|检索", normalized):
        return "web_search"
    return None


class EmbeddingIntentClassifier:
    def __init__(self, config=None):
        config = config if isinstance(config, dict) else {}
        self.config = config
        self.base_url = (
            config.get("base_url")
            or os.environ.get("KSZ_INTENT_EMBEDDING_BASE_URL")
            or os.environ.get("EMBEDDING_BASE_URL")
            or ""
        ).strip()
        self.api_key = (
            config.get("api_key")
            or os.environ.get("KSZ_INTENT_EMBEDDING_API_KEY")
            or os.environ.get("EMBEDDING_API_KEY")
            or "dummy"
        ).strip()
        self.model = (
            config.get("model")
            or os.environ.get("KSZ_INTENT_EMBEDDING_MODEL")
            or os.environ.get("EMBEDDING_MODEL")
            or "qwen3.7-text-embedding-flash"
        ).strip()
        self.dimensions = int(_number(
            config.get("dimensions")
            or os.environ.get("KSZ_INTENT_EMBEDDING_DIMENSIONS")
            or os.environ.get("EMBEDDING_DIMENSIONS")
            or 0,
            0,
        ))
        self.threshold = _number(
            config.get("threshold") or os.environ.get("KSZ_INTENT_EMBEDDING_THRESHOLD"),
            0.72,
        )
        self.margin_threshold = _number(
            config.get("margin") or os.environ.get("KSZ_INTENT_EMBEDDING_MARGIN"),
            0.04,
        )
        self.timeout = _number(config.get("timeout"), 8.0)
        self.examples = self._load_examples(config.get("examples"))
        self._references = None
        self._lock = asyncio.Lock()

    @property
    def configured(self):
        return bool(self.base_url and self.model)

    def _load_examples(self, configured):
        if not isinstance(configured, dict):
            return DEFAULT_EXAMPLES
        examples = {}
        for name, defaults in DEFAULT_EXAMPLES.items():
            values = configured.get(name, defaults)
            if isinstance(values, list):
                values = [item.strip() for item in values if isinstance(item, str) and item.strip()]
            else:
                values = []
            examples[name] = values or defaults
        return examples

    def _endpoint(self):
        base = self.base_url.rstrip("/")
        if base.endswith("/embeddings"):
            return base
        if not base.endswith("/v1"):
            base += "/v1"
        return base + "/embeddings"

    async def _embed(self, texts):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        batch_size = 20 if self.model.startswith("qwen3.7-text-embedding") else len(texts)
        vectors = []
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            for start in range(0, len(texts), batch_size):
                batch = texts[start:start + batch_size]
                payload = {"model": self.model, "input": batch}
                if self.dimensions > 0 and self.model.startswith(("text-embedding", "qwen3.7-text-embedding")):
                    payload["dimensions"] = self.dimensions
                response = await client.post(self._endpoint(), json=payload, headers=headers)
                response.raise_for_status()
                data = response.json().get("data")
                if not isinstance(data, list) or len(data) != len(batch):
                    raise ValueError("embedding API returned an unexpected number of vectors")
                for item in sorted(data, key=lambda value: value.get("index", 0)):
                    vector = item.get("embedding") if isinstance(item, dict) else None
                    if not isinstance(vector, list) or not vector:
                        raise ValueError("embedding API returned an invalid vector")
                    try:
                        normalized = _normalize([float(value) for value in vector])
                    except (TypeError, ValueError):
                        normalized = None
                    if normalized is None:
                        raise ValueError("embedding API returned an invalid vector")
                    vectors.append(normalized)
        return vectors

    async def _references_ready(self):
        if self._references is not None:
            return self._references
        async with self._lock:
            if self._references is not None:
                return self._references
            names = list(self.examples)
            texts = [text for name in names for text in self.examples[name]]
            vectors = await self._embed(texts)
            references = {}
            offset = 0
            for name in names:
                values = vectors[offset:offset + len(self.examples[name])]
                offset += len(self.examples[name])
                mean = [sum(vector[index] for vector in values) / len(values) for index in range(len(values[0]))]
                references[name] = _normalize(mean)
            self._references = references
            return references

    async def classify(self, text, available_tools, language="zh_CN"):
        if not self.configured or not isinstance(text, str) or not text.strip():
            return None
        explicit_name = explicit_tool_name(text)
        if explicit_name:
            tool_name = TOOL_NAMES[explicit_name]
            if tool_name not in available_tools:
                return None
            arguments = {}
            if explicit_name == "weather":
                location = extract_weather_location(text)
                if location:
                    arguments["location"] = location
                arguments["lang"] = language
            else:
                arguments["query"] = extract_search_query(text)
            return {"function_call": {"name": tool_name, "arguments": arguments}}
        references = await self._references_ready()
        query = (await self._embed([text]))[0]
        ranked = sorted(
            ((name, _cosine(query, vector)) for name, vector in references.items()),
            key=lambda item: item[1],
            reverse=True,
        )
        best_name, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else -1
        match = IntentMatch(best_name, best_score, best_score - second_score)
        if match.score < self.threshold or match.margin < self.margin_threshold:
            if match.name != "web_search" or match.score < 0.60:
                return None
        tool_name = TOOL_NAMES.get(match.name)
        if tool_name and tool_name not in available_tools:
            return None
        arguments = {}
        if match.name == "weather":
            location = extract_weather_location(text)
            if location:
                arguments["location"] = location
            arguments["lang"] = language
        elif match.name in {"yuanbao_search", "web_search"}:
            arguments["query"] = extract_search_query(text)
        if tool_name:
            return {"function_call": {"name": tool_name, "arguments": arguments}}
        return {"function_call": {"name": "continue_chat"}}
