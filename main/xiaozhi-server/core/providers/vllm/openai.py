import asyncio
import json

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
    supports_streaming = True

    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.api_key = config.get("api_key")
        self.enable_thinking = config.get("enable_thinking")
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

    def _request_payload(self, question, base64_image, image_mime, **kwargs):
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
        return payload

    def _request_headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def response(self, question, base64_image, image_mime="image/jpeg", **kwargs):
        payload = self._request_payload(question, base64_image, image_mime, **kwargs)
        try:
            with httpx.Client(timeout=120.0, trust_env=False) as client:
                response = client.post(
                    self.api_url,
                    headers=self._request_headers(),
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

    async def response_stream(
        self, question, base64_image, image_mime="image/jpeg", **kwargs
    ):
        payload = self._request_payload(question, base64_image, image_mime, **kwargs)
        payload["stream"] = True
        if isinstance(self.enable_thinking, bool):
            payload["enable_thinking"] = self.enable_thinking

        remaining = None
        deadline = kwargs.get("deadline")
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("VLLM 请求已超过截止时间")
        read_timeout = min(30.0, remaining) if remaining is not None else 30.0
        timeout = httpx.Timeout(read_timeout, connect=min(10.0, read_timeout))
        has_content = False
        completed = False

        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                async with client.stream(
                    "POST",
                    self.api_url,
                    headers={**self._request_headers(), "Accept": "text/event-stream"},
                    json=payload,
                ) as response:
                    if response.is_error:
                        await response.aread()
                    response.raise_for_status()
                    async for event in self._stream_events(response):
                        if event.strip() == "[DONE]":
                            completed = True
                            break

                        data = json.loads(event)
                        if not isinstance(data, dict) or "error" in data:
                            logger.bind(tag=TAG).error(f"VLLM 流返回错误: {event[:500]}")
                            raise ValueError("VLLM 流响应无效")
                        choices = data.get("choices")
                        if not isinstance(choices, list):
                            raise ValueError("VLLM 流响应缺少 choices")
                        for choice in choices:
                            if not isinstance(choice, dict):
                                raise ValueError("VLLM 流响应 choice 无效")
                            if choice.get("index", 0) != 0:
                                continue
                            delta = choice.get("delta") or {}
                            if not isinstance(delta, dict):
                                raise ValueError("VLLM 流响应 delta 无效")
                            content = delta.get("content")
                            if content is not None and not isinstance(content, str):
                                raise ValueError("VLLM 流响应正文无效")
                            if content:
                                has_content = has_content or bool(content.strip())
                                yield content

                            finish_reason = choice.get("finish_reason")
                            if finish_reason is not None:
                                if finish_reason != "stop":
                                    raise ValueError(
                                        f"VLLM 流未正常完成: {finish_reason}"
                                    )
                                completed = True
                        if completed:
                            break
            if not completed:
                raise ValueError("VLLM 流意外中断，缺少结束标记")
            if not has_content:
                raise ValueError("VLLM 流未返回有效正文")
        except httpx.HTTPStatusError as e:
            logger.bind(tag=TAG).error(
                f"VLLM HTTP {e.response.status_code}: {e.response.text[:500]}"
            )
            raise ValueError("VLLM 请求失败") from e
        except Exception as e:
            logger.bind(tag=TAG).error(f"VLLM 流请求失败: {type(e).__name__}: {e}")
            raise

    @staticmethod
    async def _stream_events(response):
        data_lines = []
        async for line in response.aiter_lines():
            if not line:
                if data_lines:
                    event = "\n".join(data_lines)
                    if event.strip():
                        yield event
                    data_lines.clear()
            elif line.startswith("data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(" ") else value)
        if data_lines:
            event = "\n".join(data_lines)
            if event.strip():
                yield event
