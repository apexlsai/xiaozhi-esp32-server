from core.utils.language import resolve_language_code_from_agent_name


LANGUAGE_NAMES = {
    "zh-CN": "Mandarin Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "zh-CN-yue": "Cantonese",
    "zh-CN-sichuan": "Sichuan Chinese",
    "zh-CN-shanghai": "Shanghainese",
    "zh-CN-minnan": "Southern Min Chinese",
    "zh-CN-shanxi": "Shaanxi Chinese",
}
LANGUAGE_ALIASES = {
    "zh": "zh-CN", "zh-cn": "zh-CN", "中文": "zh-CN", "普通话": "zh-CN",
    "汉语": "zh-CN", "英语": "en", "日语": "ja", "韩语": "ko",
    "en-us": "en", "en-gb": "en", "english": "en",
    "ja-jp": "ja", "jp": "ja", "japanese": "ja",
    "ko-kr": "ko", "kr": "ko", "korean": "ko",
    "yue": "zh-CN-yue", "zh-hk": "zh-CN-yue",
}


def normalize_language(value):
    if not isinstance(value, str):
        return None
    normalized = value.strip().replace("_", "-").lower()
    canonical = {code.lower(): code for code in LANGUAGE_NAMES}
    return canonical.get(normalized) or LANGUAGE_ALIASES.get(normalized)


def response_language(conn):
    attributes = getattr(conn, "device_attributes", None) or {}
    language = resolve_language_code_from_agent_name(attributes.get("agent_name"))
    if language:
        return language
    for value in (attributes.get("language"), getattr(conn, "device_language", None)):
        language = normalize_language(value)
        if language:
            return language
    return "zh-CN"
