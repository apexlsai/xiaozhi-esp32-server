from core.utils.language import resolve_language_code_from_agent_name


VISION_MESSAGES = {
    "zh-CN": {
        "unavailable": "视觉服务暂时不可用，请稍后再试。",
        "interrupted": "识别中断了，请再拍一次。",
    },
    "en": {
        "unavailable": "Image recognition is temporarily unavailable. Please try again later.",
        "interrupted": "Image recognition was interrupted. Please take another photo.",
    },
    "ja": {
        "unavailable": "画像認識を一時的に利用できません。しばらくしてからもう一度お試しください。",
        "interrupted": "画像認識が中断されました。もう一度写真を撮ってください。",
    },
    "ko": {
        "unavailable": "현재 이미지 인식을 이용할 수 없어요. 잠시 후 다시 시도해 주세요.",
        "interrupted": "이미지 인식이 중단되었어요. 사진을 다시 찍어 주세요.",
    },
}


def vision_language(conn=None):
    attributes = getattr(conn, "device_attributes", None) or {}
    language = resolve_language_code_from_agent_name(attributes.get("agent_name"))
    if language is None:
        language = attributes.get("language") or getattr(conn, "device_language", None)
    return language if isinstance(language, str) and language in VISION_MESSAGES else "zh-CN"


def vision_message(conn=None, kind="unavailable"):
    return VISION_MESSAGES[vision_language(conn)][kind]


def vision_read_timeout(remaining=None):
    timeout = 120.0 if remaining is None else remaining
    if timeout <= 0:
        raise TimeoutError("VLLM 请求已超过截止时间")
    return timeout
