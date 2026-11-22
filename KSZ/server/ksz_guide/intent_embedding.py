import asyncio
import math
import os
import re
import unicodedata

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
        "帮我查一下本馆有没有一件叫青铜鼎的藏品",
        "这个博物馆有哪些藏品",
    ],
    "general_chat": [
        "你好",
        "给我讲个笑话",
        "你是谁",
        "我想和你聊聊天",
        "你听到了吗",
        "我现在在哪里",
    ],
    "play_music": [
        "播放一首音乐",
        "放点音乐",
        "来一首轻松的歌",
        "我想听歌",
    ],
    "movie_search": [
        "最近有什么好看的电影",
        "推荐几部最近的电影",
        "查一下最近上映的电影",
        "最近有哪些热门影片",
    ],
    "nearby_search": [
        "附近有什么好吃的",
        "附近有什么餐厅",
        "周边哪里可以吃饭",
        "附近有什么咖啡店",
    ],
}

TOOL_NAMES = {
    "weather": "get_weather",
    "yuanbao_search": "yuanbao_search",
    "web_search": "web_search",
    "take_photo": "self_camera_take_photo",
    "play_music": "play_music",
    "movie_search": "web_search",
    "nearby_search": "web_search",
}


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
    english = re.search(r"\b(?:weather|temperature)\s+(?:in|for)\s+([A-Za-z][A-Za-z .'-]{0,30})", text, re.I)
    if english:
        return english[1].strip(" .'-") or None

    question = re.sub(
        r"^(?:[\s，,。]+|嗯|呃|是|请|帮我|给我|查询(?:一下)?|查(?:一下|一查)?|看看|看一下|想知道)+",
        "", text,
    )
    match = re.match(r"([^，,。!?！？]{0,30}?)(?:的)?(?:天气|气温|温度|降雨|下雨|预报)", question)
    if not match:
        return None
    relative = r"今天|明天|后天|现在|当前|今晚|今早|本地|当地|我这里|这里|这儿|附近|什么|怎么样|如何"
    city = re.sub(rf"^(?:(?:{relative})\s*)+|(?:\s*(?:{relative}))+$", "", match[1]).strip()
    return city or None


_SEARCH_PREFIX = r"^[\s，,。]*(?:(?:嗯|呃|请|帮我|麻烦|你)\s*[，,\s]*)*"
_SEARCH_COMMAND = (
    r"(?:用?(?:腾讯)?元宝\s*[，,]?\s*(?:搜索|查询|检索|搜|查)|"
    r"(?:联网|上网|网页|网络)(?:搜索|查询|检索|搜|查)|搜索|搜一搜|搜一下|搜下|检索)"
    r"(?:一下|一搜|一查|下)?"
)
_MUSIC_COMMAND = r"(?:播放(?:一首)?|放(?:一首|点)|来一首|来首|(?:我想)?听(?:一首|点|歌|音乐))"


def extract_search_query(text):
    return re.sub(
        _SEARCH_PREFIX + rf"(?:{_SEARCH_COMMAND}|(?:查询|查一下|查一查))(?:[，,\s]*{_SEARCH_COMMAND})*",
        "", text, flags=re.IGNORECASE,
    ).strip(" ，,。.!！？?")


def extract_music_query(text):
    query = re.sub(_SEARCH_PREFIX + _MUSIC_COMMAND, "", text).strip(" ，,。.!！？?")
    return "random" if query in {"", "音乐", "歌", "歌曲"} else query


def explicit_tool_name(text):
    if re.match(_SEARCH_PREFIX + _SEARCH_COMMAND, text, flags=re.IGNORECASE):
        return "yuanbao_search"
    if re.search(r"天气|气温|降雨|下雨|天气预报", text):
        return "weather"
    if re.match(_SEARCH_PREFIX + _MUSIC_COMMAND, text) and not re.search(r"讲解|导览|电影|视频", text):
        return "play_music"
    if re.search(r"电影|影片|影院|上映", text) and re.search(r"推荐|最近|好看|查|有什么|哪些|新片", text):
        return "movie_search"
    if re.search(r"(?:附近|周边|这里|这儿|这边).*(?:吃|喝|餐厅|饭店|咖啡|美食)", text):
        return "nearby_search"
    if re.search(r"推荐.*(?:歌曲|音乐|专辑)|(?:歌曲|音乐|专辑).*(?:推荐|好听|最近)", text):
        return "web_search"
    return None


_NAMED_MUSEUM = re.compile(
    r"[\u3400-\u9fffぁ-ヿ]{2,}?(?:博物[馆館]|美术馆|美術館|纪念馆|記念館)|"
    r"[가-힣]{2,}?(?:\s*박물관|\s*미술관)|"
    r"\b(?:[A-Z][\w’'-]+\s+){1,6}(?:Museum|Gallery)\b"
)
_MUSEUM_REFERENCE = re.compile(
    r"(?:这个|那个|这家|那家|哪家|哪个|一家|一座|什么|附近|本地|当地|参观|去过|所有|"
    r"各地的?|博物[馆館]的|这座|那座|我们(?:的)?|我(?:的)?|这里的?|你们(?:的)?|"
    r"私は|この|その|近くの|어느|우리|이곳의|(?:this|that|the|a|which)\s+)$",
    re.I,
)


def external_museum_query(text, venue):
    text = unicodedata.normalize("NFKC", text)
    if text.startswith("[位置变化]") or re.match(
        r"^(?:(?:请问|请|帮我)[，, ]*)*(?:(?:你|您)(?:们)?(?:是|属于|代表|服务于)|"
        r"拍(?:照|张照|一张照)|(?:are you|do you work|take a photo)\b)", text, re.I
    ):
        return False
    name = unicodedata.normalize("NFKC", (venue or {}).get("name") or "").strip()
    name = re.sub(r"市(?=博物[馆館]|美术馆|纪念馆)", "", name).casefold()
    for match in _NAMED_MUSEUM.finditer(text):
        museum = match[0]
        suffix = re.search(r"博物[馆館]|美术馆|美術館|纪念馆|記念館|박물관|미술관|Museum|Gallery", museum)
        if not suffix or _MUSEUM_REFERENCE.search(museum[:suffix.start()]):
            continue
        museum = re.sub(r"市(?=博物[馆館]|美术馆|纪念馆)", "", museum).casefold()
        if not name or not museum.endswith(name):
            return True
    return False


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
        name = explicit_name
        score = 1.0 if explicit_name else 0.0
        margin = 1.0 if explicit_name else 0.0
        if not name:
            references = await self._references_ready()
            query = (await self._embed([text]))[0]
            ranked = sorted(
                ((label, _cosine(query, vector)) for label, vector in references.items()),
                key=lambda item: item[1],
                reverse=True,
            )
            name, score = ranked[0]
            second_score = ranked[1][1] if len(ranked) > 1 else -1
            margin = score - second_score
            if score < self.threshold or margin < self.margin_threshold:
                if name != "web_search" or score < 0.60:
                    return None
            if name == "yuanbao_search":
                name = "web_search"

        if name == "general_chat":
            return {
                "route": "direct_answer",
                "confidence": round(max(0.0, min(1.0, score)), 4),
                "margin": round(max(0.0, min(1.0, margin)), 4),
            }
        tool_name = TOOL_NAMES.get(name)
        if not tool_name:
            return None
        if name == "yuanbao_search" and tool_name not in available_tools:
            tool_name = "web_search"
        if tool_name not in available_tools:
            return None

        arguments = {}
        if name == "weather":
            location = extract_weather_location(text)
            if location:
                arguments["location"] = location
            arguments["lang"] = language
        elif name == "play_music":
            arguments["song_name"] = extract_music_query(text)
        else:
            arguments["query"] = extract_search_query(text)
        if name == "nearby_search":
            return {"function_call": {"name": tool_name, "arguments": arguments}, "nearby": True}
        return {"function_call": {"name": tool_name, "arguments": arguments}}
