import base64

import requests
from core.utils.util import check_model_key
from core.providers.tts.base import TTSProviderBase
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.api_key = config.get("api_key")
        self.api_url = config.get(
            "api_url", "https://api.xiaomimimo.com/v1/chat/completions"
        )
        self.model = config.get("model", "mimo-v2.5-tts")
        self.voice = config.get("voice", "mimo_default")
        self.audio_file_type = config.get("format", "wav")
        self.style = config.get("style", "")
        self.output_file = config.get("output_dir", "tmp/")
        model_key_msg = check_model_key("TTS", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)

    async def text_to_speak(self, text, output_file):
        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }
        messages = []
        if self.style:
            messages.append({"role": "user", "content": self.style})
        messages.append({"role": "assistant", "content": text})
        data = {
            "model": self.model,
            "messages": messages,
            "audio": {
                "format": self.audio_file_type,
                "voice": self.voice,
            },
        }
        response = requests.post(self.api_url, json=data, headers=headers)
        if response.status_code == 200:
            audio_data = response.json()["choices"][0]["message"]["audio"]["data"]
            audio_bytes = base64.b64decode(audio_data)
            if output_file:
                with open(output_file, "wb") as audio_file:
                    audio_file.write(audio_bytes)
            else:
                return audio_bytes
        else:
            raise Exception(
                f"MiMo TTS请求失败: {response.status_code} - {response.text}"
            )
