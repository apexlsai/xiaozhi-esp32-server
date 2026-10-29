import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.handle.abortHandle import handleAbortMessage
from core.handle.sendAudioHandle import _send_sentence_start, send_tts_message
from ksz_guide.integration import handle_control_message
from ksz_guide.playback import (
    begin_response, commit_playback_message, handle_playback_progress, interrupt_response,
    playback_context_messages, playback_state, prepare_playback_message,
    record_response_delta, record_response_text,
)
from ksz_guide.settings import GuideSettings
from test_guide_announcement import conn, spoken


def connection(progress=True):
    return SimpleNamespace(session_id="session1", websocket=object(), sentence_id="answer1",
                           features={"playback_progress": progress})


def delivery(conn, text=None, state="sentence_start", commit=True):
    message = {"type": "tts", "state": state, "session_id": conn.session_id}
    if text is not None:
        message["text"] = text
    prepared = prepare_playback_message(conn, message)
    if commit:
        commit_playback_message(conn, prepared)
    return message


def acknowledge(conn, index, state="played", **fields):
    return handle_playback_progress(conn, {
        "type": "playback_progress", "session_id": conn.session_id,
        "response_id": "answer1", "played_through": index, "state": state, **fields,
    })


def context(conn, response_id="next"):
    messages = playback_context_messages(conn, [{"role": "system", "content": "Role."}], response_id)
    text = messages[0]["content"]
    return json.loads(text.split("<ksz_playback_context>\n", 1)[1].split("\n</ksz_playback_context>", 1)[0])


@pytest.mark.parametrize("progress", [False, True])
def test_generated_started_and_confirmed_are_distinct(progress):
    conn = connection(progress)
    begin_response(conn, "answer1", "讲解这个展项")
    record_response_delta(conn, "第一句。第二句。第三句。", "answer1")
    first = delivery(conn, "第一句。")
    delivery(conn, "第二句。")
    if progress:
        assert (first["response_id"], first["segment_id"]) == ("answer1", 1)
        acknowledge(conn, 1)
    else:
        assert set(first) == {"type", "state", "session_id", "text"}
        acknowledge(conn, 1)
    interrupt_response(conn)
    data = context(conn)
    assert data["confirmed_played_text"] == ("第一句。" if progress else "")
    assert data["started_unconfirmed_text"] == ("第二句。" if progress else "第一句。第二句。")
    assert data["generated_not_started_text"] == "第三句。"
    assert data["ungenerated_remainder"] == "unknown"


@pytest.mark.parametrize("fields", [
    {"session_id": "another-session"}, {"response_id": "unknown"},
    {"played_through": -1}, {"played_through": 2}, {"played_through": True},
    {"played_through": "1"}, {"state": "unknown"}, {"state": "finished"},
])
def test_unverified_progress_never_marks_text_as_heard(fields):
    conn = connection()
    begin_response(conn, "answer1", "讲解")
    delivery(conn, "第一句。")
    message = {"type": "playback_progress", "session_id": "session1", "response_id": "answer1",
               "played_through": 1, "state": "played", **fields}
    assert handle_playback_progress(conn, message)
    assert playback_state(conn).replies["answer1"].played_through == 0


def test_failed_transport_and_out_of_order_progress_cannot_claim_completion():
    conn = connection()
    begin_response(conn, "answer1", "讲解")
    delivery(conn, "第一句。", commit=False)
    acknowledge(conn, 1)
    assert playback_state(conn).replies["answer1"].played_through == 0
    conn = connection()
    begin_response(conn, "answer1", "讲解")
    delivery(conn, "第一句。")
    delivery(conn, "第二句。")
    acknowledge(conn, 2)
    acknowledge(conn, 1)
    assert playback_state(conn).replies["answer1"].played_through == 2
    delivery(conn, state="stop")
    acknowledge(conn, 2, "finished")
    interrupt_response(conn)
    assert playback_state(conn).replies["answer1"].interrupted is False


def test_new_question_records_interruption_and_rejects_late_generation():
    conn = connection()
    begin_response(conn, "answer1", "介绍这个展区")
    record_response_delta(conn, "已生成的部分。", "answer1")
    conn.sentence_id = "answer2"
    begin_response(conn, "answer2", "价格是多少？")
    record_response_delta(conn, "迟到的旧回答。", "answer1")
    data = context(conn, "answer2")
    assert data["reason"] == "new_request"
    assert data["generated_text"] == "已生成的部分。"
    acknowledge(conn, 0, "interrupted")
    assert not playback_state(conn).replies["answer2"].interrupted


def test_new_session_does_not_reuse_previous_playback_acknowledgments():
    conn = connection()
    begin_response(conn, "answer1", "讲解")
    delivery(conn, "第一句。")
    conn.session_id = "session2"
    conn.websocket = object()
    acknowledge(conn, 1, session_id="session1")
    assert playback_state(conn, False) is None
    begin_response(conn, "answer2", "你好")
    assert len(playback_state(conn).replies) == 1


def test_unmatched_subtitles_do_not_invent_unsent_content():
    conn = connection()
    begin_response(conn, "answer1", "讲解")
    record_response_delta(conn, "原始内容。", "answer1")
    delivery(conn, "经过语音纠音的字幕。")
    interrupt_response(conn)
    assert context(conn)["generated_not_started_text"] is None


def test_progress_storage_is_bounded_and_marks_truncation():
    conn = connection()
    for index in range(8):
        conn.sentence_id = str(index)
        begin_response(conn, str(index), "讲解")
    record_response_text(conn, "长" * 70000, "7")
    delivery(conn, "长" * 70000)
    interrupt_response(conn)
    state = playback_state(conn)
    assert len(state.replies) == 6
    assert len(state.replies["7"].generated) == 65536
    assert len(state.replies["7"].segments[0].text) == 65536
    assert context(conn)["truncated"] is True


@pytest.mark.asyncio
async def test_progress_is_consumed_even_without_guide_runtime():
    conn = connection()
    assert await handle_control_message(conn, {"type": "playback_progress"}) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("aborted", [False, True])
async def test_queued_subtitles_from_an_old_or_aborted_reply_are_discarded(conn, aborted):
    conn.sentence_id = "new-reply"
    conn.client_abort = aborted
    await _send_sentence_start(conn, "迟到的旧讲解。", "new-reply" if aborted else "old-reply")
    conn.websocket.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_button_abort_is_visible_to_next_real_model_request(conn):
    conn.features["playback_progress"] = True
    text = "第一句。第二句。第三句。"
    conn.llm.response.return_value = iter(text)
    conn.chat("介绍一下这个展项")
    response_id = conn.sentence_id
    await send_tts_message(conn, "sentence_start", "第一句。")
    await send_tts_message(conn, "sentence_start", "第二句。")
    acknowledge(conn, 1, response_id=response_id)
    conn.clear_queues = Mock()
    conn.clearSpeakStatus = Mock()
    await handleAbortMessage(conn)
    stop = json.loads(conn.websocket.send.await_args.args[0])
    assert stop["response_id"] == response_id
    assert stop["interrupted"] is True
    conn.client_abort = False
    conn.llm.response.return_value = iter(["价格暂时无法确认。"])
    conn.chat("它多少钱？")
    data = context(conn, conn.sentence_id)
    assert data["confirmed_played_text"] == "第一句。"
    assert data["started_unconfirmed_text"] == "第二句。"
    assert data["generated_not_started_text"] == "第三句。"
    request = conn.llm.response.call_args
    system = request.args[1][0]["content"]
    assert "Answer the newest user question first" in system
    assert "ksz_playback_context" in system
    assert request.args[1][-1]["content"] == "它多少钱？"
    assert request.kwargs["max_tokens"] == 4096


def test_long_response_has_no_sentence_or_character_truncation(conn):
    text = "这是一段有依据的展项讲解。" * 100
    conn.llm.response.return_value = iter(text)
    conn.chat("请讲解这个展区")
    assert spoken(conn) == text
    assert playback_state(conn).replies[conn.sentence_id].generated == text


@pytest.mark.parametrize("value", ["0", "-1", "bad", "131073"])
def test_invalid_narration_budget_is_rejected(value):
    with pytest.raises(ValueError, match="KSZ_NARRATION_MAX_TOKENS"):
        GuideSettings.from_env({"KSZ_NARRATION_MAX_TOKENS": value})
