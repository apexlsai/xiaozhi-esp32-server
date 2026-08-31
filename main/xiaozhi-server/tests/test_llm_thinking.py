import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.providers.llm.openai.openai import LLMProvider


class OpenAIThinkingPreferenceTests(unittest.TestCase):
    def build_provider(self, base_url, model_name, enable_thinking=False):
        provider = LLMProvider.__new__(LLMProvider)
        provider.base_url = base_url
        provider.model_name = model_name
        provider.enable_thinking = enable_thinking
        return provider

    def test_museum_agent_defaults_to_explicit_thinking_disabled(self):
        provider = self.build_provider(
            "http://127.0.0.1:15000/v1/", "museum-guide-chat"
        )
        request_params = {}

        provider._apply_thinking_preference(request_params)

        self.assertEqual(
            request_params["extra_body"]["enable_thinking"], False
        )

    def test_known_provider_uses_its_disable_payload(self):
        provider = self.build_provider(
            "https://api.deepseek.com", "deepseek-chat"
        )
        request_params = {}

        provider._apply_thinking_preference(request_params)

        self.assertEqual(
            request_params["extra_body"]["thinking"], {"type": "disabled"}
        )

    def test_unknown_provider_does_not_receive_vendor_parameter(self):
        provider = self.build_provider(
            "https://example.com/v1", "custom-chat"
        )
        request_params = {}

        provider._apply_thinking_preference(request_params)

        self.assertNotIn("extra_body", request_params)

    def test_enabled_thinking_leaves_provider_default_untouched(self):
        provider = self.build_provider(
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen-plus",
            True,
        )
        request_params = {}

        provider._apply_thinking_preference(request_params)

        self.assertNotIn("extra_body", request_params)


if __name__ == "__main__":
    unittest.main()
