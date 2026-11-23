import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.utils.dialogue import Dialogue, Message
from ksz_guide.prompt import prepare_system_prompt
from ksz_guide.prompt_owner import agent_owned, request_messages, trim_connection
from ksz_prompt import CONSUMER_ROLE, compile_messages, compile_system, validate_context


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


@pytest.mark.parametrize("owner", ["agent", "xiaozhi"])
@pytest.mark.parametrize("limit", [1, 10, 30, 50])
def test_configured_limit_reaches_model_with_complete_tool_round(owner, limit):
    conn = connection(owner)
    conn.config["context_turns"] = limit
    conn.dialogue.put(Message(role="system", content="ROLE"))
    for index in range(55):
        conn.dialogue.put(Message(role="user", content=str(index)))
        conn.dialogue.put(Message(role="assistant", tool_calls=[{"id": f"call_{index}"}]))
        conn.dialogue.put(Message(role="tool", tool_call_id=f"call_{index}", content="result"))
        conn.dialogue.put(Message(role="assistant", content="answer"))

    trim_connection(conn)
    values = {"language": "zh-CN"}
    messages = request_messages(conn, conn.dialogue.get_llm_dialogue(), values)
    if owner == "agent":
        messages = compile_messages(values["ksz_prompt_context"], messages)

    assert messages[0]["role"] == "system"
    assert [m["content"] for m in messages if m["role"] == "user"] == [
        str(i) for i in range(55 - limit, 55)
    ]
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
        f"call_{i}" for i in range(55 - limit, 55)
    ]


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


def test_context_uses_bound_agent_language_when_request_metadata_is_missing():
    conn = connection()
    conn.device_attributes = {"agent_name": "儿童-日语", "language": "zh-CN"}
    values = {}
    request_messages(conn, [], values)
    assert values["ksz_prompt_context"]["language"] == "ja"


def test_context_uses_config_bound_agent_before_stale_request_language():
    conn = connection()
    conn.config["agent_name"] = "儿童-韩语"
    values = {"language": "zh-CN"}
    request_messages(conn, [], values)
    assert values["ksz_prompt_context"]["language"] == "ko"


def test_prompt_context_canonicalizes_language_aliases():
    context = validate_context({
        "version": 1, "base_prompt": "role", "language": "ko_KR",
        "state": {}, "turn": {},
    })
    assert context["language"] == "ko"


def test_other_connections_and_legacy_history_are_not_modified():
    conn = connection("xiaozhi")
    messages = [{"role": "system", "content": "legacy"}]
    values = {}
    assert request_messages(conn, messages, values) is messages
    assert values == {}


@pytest.mark.parametrize("owner", ["agent", "xiaozhi"])
def test_consumer_identity_replaces_venue_role_for_both_prompt_owners(owner):
    conn = connection(owner)
    conn.config.update(prompt="你是萍乡博物馆的助手，名字是小硕。", agent_name="旧配置名")
    conn.device_attributes = {"agent_name": " 青年-汉语 "}
    messages = [
        {"role": "system", "content": "<museum_role>你是萍乡博物馆的助手</museum_role>"},
        {"role": "user", "content": "你是谁？"},
    ]
    values = {"device_mode": "consumer"}
    wire = request_messages(conn, messages, values)
    prompt = compile_system(values["ksz_prompt_context"]) if owner == "agent" else wire[0]["content"]
    assert CONSUMER_ROLE in prompt
    assert '"小硕"' in prompt and "Apexls" in prompt
    assert "青年-汉语" not in prompt and "旧配置名" not in prompt and "萍乡博物馆" not in prompt
    assert values["agent_name"] == "青年-汉语"
    assert values["assistant_name"] == "小硕"
    assert wire[-1] == messages[-1]

    conn.config["prompt"] = "你是智能导览助手。"
    values = {"device_mode": "consumer"}
    wire = request_messages(conn, messages, values)
    prompt = compile_system(values["ksz_prompt_context"]) if owner == "agent" else wire[0]["content"]
    assert "Apexls" in prompt and "青年-汉语" not in prompt and "小硕" not in prompt
    assert values["agent_name"] == "青年-汉语" and "assistant_name" not in values

    conn.device_attributes.update(agent_name="小硕", apexls_role="youth", apexls_language="ja")
    conn.config["prompt"] = "你是智能导览助手，名字是旧名字。"
    values = {"device_mode": "consumer"}
    wire = request_messages(conn, messages, values)
    prompt = compile_system(values["ksz_prompt_context"]) if owner == "agent" else wire[0]["content"]
    assert '"小硕"' in prompt and "旧名字" not in prompt
    assert values["agent_name"] == values["assistant_name"] == "小硕"


def test_unhandled_cross_museum_turn_carries_search_scope_to_agent(monkeypatch):
    from ksz_guide import integration

    conn = connection("xiaozhi")
    monkeypatch.setattr(integration, "runtime_for", lambda conn: True)
    value = {"venue_context": {"name": "萍乡博物馆"}}
    request_messages(conn, [{"role": "user", "content": "深圳博物馆里面有什么重要产品？"}], value)
    assert value["ksz_external_museum"] is True


@pytest.mark.parametrize("owner", ["agent", "xiaozhi"])
def test_search_follow_up_and_recall_use_current_dialogue(monkeypatch, owner):
    from ksz_guide import integration

    follow_up = "你知道下一届在哪里举办吗？"
    classifier = SimpleNamespace(configured=True, classify=AsyncMock(), match_topic=AsyncMock())
    monkeypatch.setattr(integration, "_embedding_classifier", lambda conn: classifier)
    conn = connection(owner)
    conn.config["context_turns"] = 20
    dialogue = conn.dialogue
    dialogue.update_system_message("ROLE")
    dialogue.put(Message(role="user", content="今年 APEC 在哪里举办？"))
    dialogue.put(Message(role="assistant", content="2026 年 APEC 在深圳举办。"))
    llm = Mock()
    llm.response.return_value = iter(["下一届 APEC 的主办地是……"])
    conn.session_id = "session-a"
    conn.sentence_id = "sentence-a"
    conn.logger = Mock()
    conn.llm = llm
    conn.func_handler = SimpleNamespace(
        get_functions=lambda: [{"function": {"name": "yuanbao_search"}}],
        has_tool=lambda name: name == "yuanbao_search",
        handle_llm_function_call=AsyncMock(),
    )

    def prepare(query, sentence_id, depth):
        values = {"language": "zh-CN"}
        return request_messages(conn, dialogue.get_llm_dialogue(), values, depth), values

    conn._prepare_llm_request = Mock(side_effect=prepare)
    result = json.loads(asyncio.run(integration.classify_embedding_intent(conn, follow_up)))
    arguments = result["function_call"]["arguments"]
    assert arguments == {"query": "APEC 你知道下一届在哪里举办吗"}
    dialogue.put(Message(role="user", content=follow_up))
    call = {"id": "call-a", "name": "yuanbao_search", "arguments": json.dumps(arguments)}
    asyncio.run(integration.execute_embedding_tool(conn, call))
    conn.func_handler.handle_llm_function_call.assert_awaited_once_with(conn, call)
    integration.answer_embedding_tool(conn, call, follow_up, "搜索结果")
    conn._prepare_llm_request.assert_called_once_with(follow_up, conn.sentence_id, depth=5)
    messages = llm.response.call_args.args[1]
    if owner == "agent":
        values = llm.response.call_args.kwargs["extra_body"]
        messages = compile_messages(values["ksz_prompt_context"], messages)
        assert "RAW_ROLE" in messages[0]["content"]
    else:
        assert messages[0] == {"role": "system", "content": "ROLE"}
    assert [m["content"] for m in messages if m["role"] == "user"] == [
        "今年 APEC 在哪里举办？", follow_up,
    ]
    assert messages[-2]["tool_calls"][0]["id"] == messages[-1]["tool_call_id"] == "call-a"
    assert messages[-1]["content"] == "搜索结果"

    recall = json.loads(asyncio.run(integration.classify_embedding_intent(conn, "我刚才跟你聊了什么？")))
    assert recall["function_call"]["name"] == "continue_chat"
    classifier.classify.assert_not_awaited()
    classifier.match_topic.assert_not_awaited()
    llm.response_no_stream.assert_not_called()

    conn.config["context_turns"] = 1
    assert json.loads(asyncio.run(integration.classify_embedding_intent(conn, follow_up))) == {
        "search_clarification": "您指的是哪项活动？",
    }
