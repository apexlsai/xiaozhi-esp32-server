from abc import ABC, abstractmethod
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class VLLMProviderBase(ABC):
    supports_streaming = False

    @abstractmethod
    def response(self, question, base64_image, image_mime="image/jpeg", **kwargs):
        """VLLM response generator"""
        pass
