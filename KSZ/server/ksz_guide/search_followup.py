import asyncio
import json
import re
import unicodedata

from ksz_prompt import DEFAULT_CONTEXT_TURNS

from .intent_embedding import explicit_tool_name, extract_search_query
from .prompt_owner import configured_context_turns


_EDITION = re.compile(
    r"(?:上|下|同)一?届|\b(?:next|previous|last)\s+(?:one|edition|(?:summer |winter )?olympics|world cup)\b"
    r"|\b(?:it|that event) (?:will be |was )?held\b|次回(?:は|の|どこ)|(?:다음|지난)\s*(?:회차|대회)",
    re.IGNORECASE,
)
_EVENT_NAME = re.compile(
    r"(?<![A-Za-z0-9])(?P<abbreviation>[A-Z][A-Z0-9]{1,15})(?![A-Za-z0-9])"
    r"|(?i:\b(?:(?:summer|winter)\s+)?(?:olympic games|olympics|paralympics|world cup|asian games)\b)"
    r"|[\u3400-\u9fff]{0,18}?(?:奥运会|残奥会|亚运会|世界杯|世博会|博览会|峰会|展览会|运动会|大会|锦标赛)"
    r"|オリンピック|五輪|ワールドカップ|万博|올림픽|월드컵|엑스포|아시안 게임"
)
_NAME_PREFIX = re.compile(
    r"^(?:(?:你知道|您知道|我想知道|想了解|想问|说说|介绍一下|请问|请|帮我|查一下|聊聊|"
    r"今年|去年|明年|(?:上|下|同)一?届|举办的|和|与|及|的)+)"
)
_GENERIC_EVENTS = {"大会", "峰会", "博览会", "展览会", "运动会", "锦标赛"}
_QUESTION_WORD = re.compile(
    r"哪|什么|几[年月日号届]|怎么|谁|是否|有没有|何时|何地"
    r"|\b(?:where|when|which|what|who|how)\b|どこ|いつ|何時|어디|언제|누가|어느",
    re.IGNORECASE,
)
_EVENT_CUE = re.compile(
    r"举办|主办|举行|召开|会期|届|会议|峰会|活动|開催|개최"
    r"|\b(?:host\w*|held|summit|conference|edition|expo)\b",
    re.IGNORECASE,
)
_CLARIFICATION = {
    "zh-CN": ("您指的是{}？", "您指的是哪项活动？", "还是"),
    "en": ("Do you mean {}?", "Which event do you mean?", " or "),
    "ja": ("{}のどちらですか？", "どのイベントのことですか？", "、それとも"),
    "ko": ("{} 중 어느 행사인가요?", "어떤 행사를 말씀하시나요?", " 또는 "),
}


def _event_names(text):
    text = re.sub(r"(?<!\d)(?:19|20)\d{2}\s*年?", " ", unicodedata.normalize("NFKC", text)).strip()
    names = {}
    for match in _EVENT_NAME.finditer(text):
        name = _NAME_PREFIX.sub("", match[0])
        if match["abbreviation"] and name != text and not _EVENT_CUE.search(text):
            continue
        if name and name not in _GENERIC_EVENTS and not _QUESTION_WORD.search(name):
            names.setdefault(name.casefold(), name)
    return list(names.values())


def _recent_events(conn, previous_turns):
    dialogue = getattr(conn, "dialogue", None)
    if previous_turns < 1 or dialogue is None:
        return []
    names = {}
    for message in reversed(dialogue.dialogue):
        if message.is_temporary:
            continue
        texts = []
        if message.role == "user" and isinstance(message.content, str):
            if not message.content.startswith("[位置变化]"):
                texts.append(extract_search_query(message.content))
            previous_turns -= 1
        for call in message.tool_calls or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if function.get("name") != "yuanbao_search":
                continue
            args = function.get("arguments", {})
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                continue
            query = args.get("query") if isinstance(args, dict) else None
            if isinstance(query, str):
                texts.append(query)
        for text in texts:
            for name in _event_names(text):
                names.setdefault(name.casefold(), name)
        if previous_turns == 0:
            break
    return list(names.values())


def _search_intent(query):
    return {"function_call": {"name": "yuanbao_search", "arguments": {"query": query}}}


async def edition_search_intent(conn, classifier, text, language):
    pending = getattr(conn, "ksz_edition_clarification", None)
    if pending:
        conn.ksz_edition_clarification = None
    explicit = explicit_tool_name(text)
    if explicit not in (None, "yuanbao_search"):
        return None

    query = extract_search_query(text)
    edition = _EDITION.search(query)
    if not pending and edition is None:
        return None
    dialogue = getattr(conn, "dialogue", None)
    limit = configured_context_turns(conn) or DEFAULT_CONTEXT_TURNS
    if pending:
        previous_dialogue, previous_question, candidates = pending
        if previous_dialogue is dialogue and limit > 1:
            choice = re.sub(r"^(?:我说的是|我指的是|是)\s*", "", unicodedata.normalize("NFKC", query)).casefold()
            named_choice = next((name for name in candidates if name.casefold() == choice), None)
            if named_choice or (not candidates and choice in {name.casefold() for name in _event_names(query)}):
                return _search_intent(f"{named_choice or query} {previous_question}")

    if edition is None:
        return None
    if explicit is None and query[edition.end():].strip(" 呢吗はの는은요") and not _QUESTION_WORD.search(query):
        return None
    if _event_names(query):
        return _search_intent(query)

    candidates = _recent_events(conn, limit - 1)
    if len(candidates) == 1:
        return _search_intent(f"{candidates[0]} {query}")
    if candidates and classifier is not None and classifier.configured:
        try:
            name = await asyncio.wait_for(classifier.match_topic(query, candidates), timeout=2)
        except Exception as error:
            conn.logger.bind(tag=__name__).warning(f"活动话题排序失败: {type(error).__name__}")
        else:
            if name:
                return _search_intent(f"{name} {query}")

    named, unnamed, separator = _CLARIFICATION.get(language, _CLARIFICATION["zh-CN"])
    conn.ksz_edition_clarification = (dialogue, query, candidates)
    return {"search_clarification": named.format(separator.join(candidates)) if len(candidates) == 2 else unnamed}
