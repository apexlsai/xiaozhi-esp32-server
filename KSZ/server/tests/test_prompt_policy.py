from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jinja2 import Template

from core.connection import ConnectionHandler
from core.utils.prompt_manager import PromptManager
from ksz_guide.prompt import LANGUAGE_NAMES, prepare_system_prompt, prompt_template_path, response_language


def connection(**attributes):
    return SimpleNamespace(device_id="synthetic-device", device_attributes=attributes,
                           device_language="zh-CN", dialogue=SimpleNamespace(update_system_message=Mock()))


@pytest.mark.parametrize("name,language", [("青年-英语", "en"), ("专家—日语", "ja"), ("儿童－韩语-测试", "ko")])
def test_prompt_and_llm_request_use_bound_agent_despite_stale_language_attribute(name, language):
    conn = connection(agent_name=name, language="zh-CN")
    ConnectionHandler.change_system_prompt(conn, "Use the configured professional role.")
    assert f"({language})" in conn.prompt
    assert ConnectionHandler._build_llm_extra_body(conn)["language"] == language
    conn.dialogue.update_system_message.assert_called_once_with(conn.prompt)


@pytest.mark.parametrize("language", LANGUAGE_NAMES)
def test_each_supported_language_has_a_single_policy_and_preserves_the_role(language):
    conn = connection(language=language)
    role = "A carefully configured museum role, including its name and expertise."
    prompt = prepare_system_prompt(conn, role)
    assert prompt.startswith(role + "\n\n")
    assert f"Write every spoken reply in {LANGUAGE_NAMES[language]}" in prompt
    assert prepare_system_prompt(conn, prompt) == prompt


def test_rebinding_replaces_previous_language_policy_without_repeating_or_removing_role():
    conn = connection(agent_name="青年-英语", language="en")
    prompt = prepare_system_prompt(conn, "Configured museum role.")
    conn.device_attributes = {"agent_name": "青年-日语", "language": "en"}
    updated = prepare_system_prompt(conn, prompt)
    assert updated.startswith("Configured museum role.")
    assert updated.count("<ksz_response_policy>") == 1
    assert "Write every spoken reply in Japanese" in updated
    assert "Write every spoken reply in English" not in updated


@pytest.mark.parametrize("value,expected", [("en_US", "en"), ("ja-JP", "ja"), ("ko_KR", "ko"),
                                           ("ZH-CN-YUE", "zh-CN-yue"), ("unknown", "zh-CN"), (None, "zh-CN")])
def test_language_aliases_and_unknown_values_align_prompt_with_llm_metadata(value, expected):
    conn = connection(language=value)
    assert response_language(conn) == expected
    assert ConnectionHandler._build_llm_extra_body(conn)["language"] == expected


def test_initial_prompt_uses_device_language_when_attributes_are_not_ready():
    conn = connection()
    conn.device_language = "en"
    ConnectionHandler.change_system_prompt(conn, "Initial configured role.")
    assert "Write every spoken reply in English" in conn.prompt
    assert ConnectionHandler._build_llm_extra_body(conn)["language"] == "en"


@pytest.mark.parametrize("path", [None, "agent-base-prompt.txt", "./agent-base-prompt.txt"])
def test_default_template_is_replaced_without_reusing_the_upstream_template_cache(path):
    manager = PromptManager.__new__(PromptManager)
    manager.config = {"prompt_template": path}
    manager.logger = Mock()
    manager.cache_manager = Mock()
    manager.cache_manager.get.return_value = None
    manager.CacheType = SimpleNamespace(CONFIG="config")
    manager._load_base_template()
    assert manager.base_prompt_template == Path(prompt_template_path(path)).read_text()
    cache_key = manager.cache_manager.get.call_args.args[1]
    assert "ksz_guide" in cache_key


def test_explicit_custom_template_is_respected(tmp_path):
    path = tmp_path / "custom.txt"
    path.write_text("Custom {{base_prompt}}")
    assert prompt_template_path(str(path)) == str(path)


@pytest.mark.parametrize("emoji_enabled", [True, False])
def test_museum_template_renders_role_context_and_device_emoji_preference(emoji_enabled):
    text = Template(Path(prompt_template_path(None)).read_text()).render(
        base_prompt="Synthetic museum role.", emoji_enabled=emoji_enabled, emojiList=["🙂"],
        current_time="12:00", today_date="2026-01-01", today_weekday="Thursday",
        local_address="Synthetic city", beacon_location="Synthetic gallery", dynamic_context="",
    )
    assert "Synthetic museum role." in text
    assert "Synthetic gallery" in text
    assert ("Allowed emotion markers" in text) is emoji_enabled
    assert ("Do not output emoji" in text) is not emoji_enabled
    for conflict in ["playful, expressive", "我在呢", "咋回事", "要继续听吗", "Chinese semicolons"]:
        assert conflict not in text
