from typing import Any, Optional

LANGUAGE_AGENT_SUFFIXES = {
    "zh-CN": "汉语",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "zh-CN-yue": "粤语",
    "zh-CN-sichuan": "四川话",
    "zh-CN-shanghai": "上海话",
    "zh-CN-minnan": "闽南语",
    "zh-CN-shanxi": "陕西话",
}


def resolve_language_code_from_agent_name(agent_name: Optional[str]) -> Optional[str]:
    """从智能体名后缀反查规范语言码，如 小硕-粤语 → zh-CN-yue。"""
    if not isinstance(agent_name, str) or not agent_name:
        return None
    for separator in ("-", "－", "—"):
        for language, suffix in LANGUAGE_AGENT_SUFFIXES.items():
            if agent_name.endswith(f"{separator}{suffix}-测试") or agent_name.endswith(
                f"{separator}{suffix}"
            ):
                return language
    return None


def resolve_language_agent_name(
    current_agent_name: Optional[str], language: Any, dev: bool = False
) -> Optional[str]:
    """根据“小硕-汉语”命名规则推导同一智能体的目标语言版本。"""
    if not isinstance(current_agent_name, str) or not isinstance(language, str):
        return None

    target_suffix = LANGUAGE_AGENT_SUFFIXES.get(language)
    if target_suffix is None:
        return None
    if dev:
        target_suffix = f"{target_suffix}-测试"

    for separator in ("-", "－", "—"):
        current_suffixes = [
            candidate
            for suffix in LANGUAGE_AGENT_SUFFIXES.values()
            for candidate in (f"{suffix}-测试", suffix)
        ]
        for current_suffix in sorted(current_suffixes, key=len, reverse=True):
            marker = f"{separator}{current_suffix}"
            if current_agent_name.endswith(marker):
                prefix = current_agent_name[: -len(marker)]
                return f"{prefix}{separator}{target_suffix}" if prefix else None
    return None
