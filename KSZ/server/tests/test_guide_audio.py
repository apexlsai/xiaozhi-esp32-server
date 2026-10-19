import asyncio
import importlib.util
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from test_guide import DEVICE, Harness


@pytest_asyncio.fixture
async def playback(monkeypatch):
    monkeypatch.setitem(sys.modules, "opuslib_next", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "core.utils.util", SimpleNamespace(audio_to_data=AsyncMock()))
    monkeypatch.setitem(sys.modules, "core.utils.audioRateController", SimpleNamespace(AudioRateController=Mock()))
    path = Path(__file__).resolve().parents[3] / "main/xiaozhi-server/core/handle/sendAudioHandle.py"
    spec = importlib.util.spec_from_file_location("ksz_guide_audio_transport_test", path)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    monkeypatch.setitem(sys.modules, "core.handle.sendAudioHandle", transport)
    h = Harness()
    await h.sync()
    await h.observe(1)
    conn = h.conn()
    conn.sentence_id = "previous-turn"
    conn.client_abort = True
    conn.client_is_speaking = False
    conn.max_output_size = 0
    conn.config = {}
    conn.logger = Mock()
    conn.calling = False
    conn.close_after_chat = False
    conn.features = {}
    conn.tts = SimpleNamespace(tts_audio_first_sentence=True)
    conn.clearSpeakStatus = lambda: setattr(conn, "client_is_speaking", False)
    conn.executor = ThreadPoolExecutor(max_workers=1)
    conn.chat = Mock()
    await h.runtime.bind(conn)
    try:
        yield SimpleNamespace(h=h, conn=conn, transport=transport, beacon=h.runtime.location(DEVICE))
    finally:
        conn.executor.shutdown(wait=True)
        await h.runtime.close()


def wire_messages(conn):
    return [json.loads(call.args[0]) for call in conn.websocket.send.await_args_list
            if isinstance(call.args[0], str)]


@pytest.mark.asyncio
async def test_guide_sends_tts_before_audio_without_echoing_location_as_stt(playback):
    h, conn, transport = playback.h, playback.conn, playback.transport
    loop = asyncio.get_running_loop()

    async def send_audio(instance, packets):
        for packet in packets:
            await instance.websocket.send(packet)

    transport.sendAudio = send_audio

    def chat(prompt):
        assert conn.client_abort is False
        assert conn.client_is_speaking is True
        assert conn.last_activity_time == h.clock() * 1000
        assert h.runtime.context(DEVICE)["last_beacon_id"] == "asset-1"
        assert playback.beacon.to_prompt_context() in prompt
        assert wire_messages(conn) == [{"type": "tts", "state": "start", "session_id": conn.session_id}]
        conn.sentence_id = "guide-turn"

        async def speak():
            await transport.sendAudioMessage(conn, transport.SentenceType.FIRST, [b"audio"], "欢迎来到展区。")
            await transport.sendAudioMessage(conn, transport.SentenceType.LAST, [], None)

        asyncio.run_coroutine_threadsafe(speak(), loop).result(timeout=2)

    conn.chat.side_effect = chat
    await h.runtime._announce(conn, playback.beacon)

    conn.chat.assert_called_once()
    messages = wire_messages(conn)
    assert [(item["type"], item["state"]) for item in messages] == [
        ("tts", "start"), ("tts", "sentence_start"), ("tts", "stop"),
    ]
    assert all("[位置变化]" not in item.get("text", "") for item in messages)
    assert conn.websocket.send.await_args_list[2].args == (b"audio",)
    assert conn.client_is_speaking is False


@pytest.mark.asyncio
async def test_guide_respects_output_limit_without_sending_playback_commands(playback, monkeypatch):
    from core.utils import output_counter

    conn = playback.conn
    conn.max_output_size = 10
    monkeypatch.setattr(output_counter, "get_device_output", lambda device: 10 if device == DEVICE else 0)
    await playback.h.runtime._announce(conn, playback.beacon)
    conn.chat.assert_not_called()
    conn.websocket.send.assert_not_awaited()
    assert conn.client_is_speaking is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["abort", "new_turn", "disconnect"])
async def test_guide_does_not_override_activity_during_playback_start(playback, change):
    conn = playback.conn

    async def send(message):
        if change == "abort":
            conn.client_abort = True
            conn.client_is_speaking = False
        elif change == "new_turn":
            conn.sentence_id = "user-turn"
        else:
            conn.websocket.closed = True

    conn.websocket.send.side_effect = send
    await playback.h.runtime._announce(conn, playback.beacon)
    conn.chat.assert_not_called()
    assert [item["state"] for item in wire_messages(conn)] == ["start"]
    if change == "abort":
        assert conn.client_abort is True
        assert conn.client_is_speaking is False
    elif change == "new_turn":
        assert conn.sentence_id == "user-turn"
        assert conn.client_is_speaking is True


@pytest.mark.asyncio
async def test_location_invalidated_during_start_does_not_reach_llm(playback):
    conn = playback.conn

    async def send(message):
        if json.loads(message)["state"] == "start":
            playback.h.runtime.state(DEVICE).current = None

    conn.websocket.send.side_effect = send
    await playback.h.runtime._announce(conn, playback.beacon)
    conn.chat.assert_not_called()
    assert [item["state"] for item in wire_messages(conn)] == ["start", "stop"]
    assert conn.client_is_speaking is False


@pytest.mark.asyncio
async def test_cancelled_guide_start_closes_playback_without_starting_chat(playback):
    conn = playback.conn
    started = asyncio.Event()

    async def send(message):
        if json.loads(message)["state"] == "start":
            started.set()
            await asyncio.Future()

    conn.websocket.send.side_effect = send
    task = asyncio.create_task(playback.h.runtime._announce(conn, playback.beacon))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    conn.chat.assert_not_called()
    assert [item["state"] for item in wire_messages(conn)] == ["start", "stop"]
    assert conn.client_is_speaking is False


@pytest.mark.asyncio
async def test_cancelled_queued_announcement_never_starts_after_worker_is_free(playback):
    conn = playback.conn
    release = threading.Event()
    occupied = conn.executor.submit(release.wait)
    started = asyncio.Event()
    conn.websocket.send.side_effect = lambda message: started.set()
    task = asyncio.create_task(playback.h.runtime._announce(conn, playback.beacon))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.wrap_future(occupied)
    await asyncio.wrap_future(conn.executor.submit(lambda: None))
    conn.chat.assert_not_called()
    assert [item["state"] for item in wire_messages(conn)] == ["start", "stop"]
    assert conn.client_is_speaking is False
