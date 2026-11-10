from ksz_prompt import compile_messages, validate_context


def prepare_request(body):
    if body.ksz_prompt_context is None:
        return body
    from src.api.schemas import ChatMessage
    body = body.model_copy(deep=True)
    context = validate_context(body.ksz_prompt_context)
    body.language = context["language"]
    messages = [message.model_dump(exclude_none=True) for message in body.messages]
    body.messages = [ChatMessage.model_validate(message) for message in compile_messages(context, messages)]
    return body


def owned_payload(body, evidence, disable_tools):
    context = body.ksz_prompt_context
    if context is None:
        return None
    messages = [message.model_dump(exclude_none=True) for message in body.messages]
    if disable_tools:
        from copy import deepcopy
        context = deepcopy(context)
        context.setdefault("turn", {})["tools_available"] = False
    return {"messages": compile_messages(context, messages, evidence)}


def is_text_fact_question(body, visual_turn, question):
    import re
    return (body.ksz_prompt_context is not None and visual_turn.action == "clarify"
            and not visual_turn.record
            and bool(re.search(r"场馆|展厅|展馆|本馆|产品|LED|OLED|LCD|屏幕|投影|\b(?:venue|museum|showroom|product|display|screen)\b", question, re.I))
            and not re.search(r"照片|图片|图中|画面|拍照|摄像|\b(?:photo|picture|image|camera)\b", question, re.I))


def fallback_answer(body, default):
    if body.ksz_prompt_context is None or not body.messages or body.messages[-1].role != "tool":
        return default
    from src.api.server import _latest_tool_name
    message = body.messages[-1].content
    if (_latest_tool_name(body) not in {"get_news_from_newsnow", "get_news_from_chinanews"}
            or not isinstance(message, str)
            or not message.startswith(("未配置", "抱歉，未能获取"))):
        return default
    language = body.ksz_prompt_context["language"].lower()
    if language.startswith("zh"):
        return message
    if language.startswith("ja"):
        return "指定されたニュースの配信元またはカテゴリを利用できません。別の配信元には切り替えていません。"
    if language.startswith("ko"):
        return "요청한 뉴스 출처 또는 카테고리를 사용할 수 없습니다. 다른 출처로 대체하지 않았습니다."
    return "The requested news source or category is unavailable. Please choose another source or category."


def permits_evidence_search(body, decision):
    context = body.ksz_prompt_context
    return bool(context and decision.route == "rag"
                and context.get("turn", {}).get("kind", "user") == "user"
                and not context.get("turn", {}).get("tool_depth_exhausted")
                and body.messages and body.messages[-1].role == "user"
                and body.tool_choice != "none"
                and any(tool.get("function", {}).get("name") == "web_search" for tool in body.tools or []))
