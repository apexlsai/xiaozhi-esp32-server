from types import SimpleNamespace

import pytest

from core.utils.dialogue import Dialogue, Message
from ksz_guide.prompt import prepare_system_prompt
from ksz_guide.prompt_owner import agent_owned, request_messages, trim_connection


def connection(owner="agent"):
    return SimpleNamespace(config={"prompt_owner": owner, "prompt": "RAW_ROLE"},
                           dialogue=Dialogue(), device_attributes={}, device_id="test-device")


def test_selected_model_owns_opt_in_and_default_remains_legacy():
    conn = connection("xiaozhi")
    assert not agent_owned(conn)
    conn.config.update(selected_module={"LLM": "selected"}, LLM={"selected": {"prompt_owner": "agent"}})
    assert agent_owned(conn)
    assert prepare_system_prompt(conn, "OLD_RENDERED_TEMPLATE") == "RAW_ROLE"


def test_unknown_owner_is_a_configuration_error():
    with pytest.raises(ValueError):
        agent_owned(connection("unknown"))


def test_persistent_history_and_wire_keep_same_twenty_rounds():
    conn = connection()
    conn.dialogue.put(Message(role="system", content="OLD_RULES"))
    for index in range(25):
        conn.dialogue.put(Message(role="user", content=str(index)))
        conn.dialogue.put(Message(role="assistant", content="answer"))
    trim_connection(conn)
    values = {"language": "en", "device_id": "test-device", "device_location": {"status": "expired"}}
    wire = request_messages(conn, conn.dialogue.get_llm_dialogue(), values, 5)
    assert sum(message.role == "user" for message in conn.dialogue.dialogue) == 20
    assert sum(message["role"] == "user" for message in wire) == 20
    assert not any(message["role"] == "system" for message in wire)
    assert wire[0]["content"] == "5"
    context = values["ksz_prompt_context"]
    assert context["base_prompt"] == "RAW_ROLE" and context["language"] == "en"
    assert context["turn"]["tool_depth_exhausted"] is True
    assert context["state"]["device_location"]["status"] == "expired"


def test_internal_greeting_is_request_state_not_user_history():
    conn = connection()
    values = {"language": "zh-CN"}
    assert request_messages(conn, [], values, kind="idle_greeting") == []
    assert values["ksz_prompt_context"]["turn"]["kind"] == "idle_greeting"
    assert conn.dialogue.dialogue == []


def test_other_connections_and_legacy_history_are_not_modified():
    conn = connection("xiaozhi")
    messages = [{"role": "system", "content": "legacy"}]
    values = {}
    assert request_messages(conn, messages, values) is messages
    assert values == {}
