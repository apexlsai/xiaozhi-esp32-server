import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.utils.dialogue import Dialogue, Message
from ksz_guide.intent_embedding import EmbeddingIntentClassifier
from ksz_guide.search_followup import edition_search_intent


def connection(*questions):
    conn = SimpleNamespace(config={}, dialogue=Dialogue())
    for question in questions:
        conn.dialogue.put(Message(role="user", content=question))
    return conn


def query(result):
    return result["function_call"]["arguments"]["query"]


@pytest.mark.parametrize(("vectors", "selected"), [
    ([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], "奥运会"),
    ([[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]], None),
    ([[1.0, 0.0], [0.5, 0.866], [0.0, 1.0]], None),
])
def test_multiple_topics_rank_or_clarify_and_resume(monkeypatch, vectors, selected):
    matcher = EmbeddingIntentClassifier({"base_url": "https://embedding.test"})
    embed = AsyncMock(return_value=vectors)
    monkeypatch.setattr(matcher, "_embed", embed)
    conn = connection("2026年APEC在哪里举办？", "奥运会哪年举办？")
    question = "下一届在哪里举办"
    result = asyncio.run(edition_search_intent(conn, matcher, question, "zh-CN"))
    embed.assert_awaited_once_with([question, "奥运会", "APEC"])
    if selected:
        assert query(result) == f"{selected} {question}"
    else:
        assert result == {"search_clarification": "您指的是奥运会还是APEC？"}
        reply = asyncio.run(edition_search_intent(conn, matcher, "是 APEC。", "zh-CN"))
        assert query(reply) == f"APEC {question}"


def test_explicit_topic_and_unrelated_question_preserve_normal_routing():
    conn = connection("2026年APEC在哪里举办？")
    assert query(asyncio.run(edition_search_intent(conn, None, "下一届世界杯在哪里？", "zh-CN"))) == (
        "下一届世界杯在哪里"
    )
    assert asyncio.run(edition_search_intent(conn, None, "今天深圳天气怎么样？", "zh-CN")) is None
    assert asyncio.run(edition_search_intent(conn, None, "给我讲个笑话", "zh-CN")) is None
    assert asyncio.run(edition_search_intent(conn, None, "给下一届学生写一句祝福", "zh-CN")) is None


def test_recent_topic_can_come_from_the_saved_tool_query():
    conn = connection("今年 APEC 在哪里举办？", "下一届在哪里举办？")
    conn.dialogue.put(Message(role="assistant", tool_calls=[{
        "id": "search-apec",
        "function": {"name": "yuanbao_search", "arguments": '{"query":"APEC 下一届在哪里举办"}'},
    }]))
    conn.dialogue.put(Message(role="tool", tool_call_id="search-apec", content="检索结果"))
    conn.config["context_turns"] = 2
    result = asyncio.run(edition_search_intent(conn, None, "下届是什么时候？", "zh-CN"))
    assert query(result) == "APEC 下届是什么时候"


def test_unknown_topic_does_not_carry_across_dialogues():
    conn = connection()
    assert asyncio.run(edition_search_intent(conn, None, "下一届呢？", "zh-CN")) == {
        "search_clarification": "您指的是哪项活动？",
    }
    conn.dialogue = Dialogue()
    assert asyncio.run(edition_search_intent(conn, None, "APEC", "zh-CN")) is None
