import httpx
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.vllm.base import VLLMProviderBase

TAG = __name__
logger = setup_logging()


def _resolve_chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


class VLLMProvider(VLLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.api_key = config.get("api_key")
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")

        param_defaults = {
            "max_tokens": (500, int),
            "temperature": (0.7, lambda x: round(float(x), 1)),
            "top_p": (1.0, lambda x: round(float(x), 1)),
        }

        for param, (default, converter) in param_defaults.items():
            value = config.get(param)
            try:
                setattr(
                    self,
                    param,
                    converter(value) if value not in (None, "") else default,
                )
            except (ValueError, TypeError):
                setattr(self, param, default)

        model_key_msg = check_model_key("VLLM", self.api_key)
        if model_key_msg:
            raise ValueError(model_key_msg)
        if not self.base_url or not self.model_name:
            raise ValueError("VLLM base_url 或 model_name 未配置")
        self.api_url = _resolve_chat_completions_url(self.base_url)

    def response(self, question, base64_image, image_mime="image/jpeg", **kwargs):
        language = kwargs.get("language")
        prompt = question if language else f"{question}(请使用中文回复)"
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image_mime};base64,{base64_image}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "stream": False,
        }
        for key in ("device_id", "language", "last_beacon_id"):
            value = kwargs.get(key)
            if value:
                payload[key] = value
        try:
            with httpx.Client(timeout=120.0, trust_env=False) as client:
                response = client.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:500] if e.response is not None else str(e)
            logger.bind(tag=TAG).error(
                f"VLLM HTTP {e.response.status_code if e.response else '?'}: {detail}"
            )
            raise ValueError(f"VLLM 请求失败: {detail}") from e
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in response generation: {e}")
            raise
