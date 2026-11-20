import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web

from ksz_guide.fixed import FixedClient, play_fixed, profile, resume_fixed_chat
from ksz_guide.models import GuideError
from ksz_guide.integration import track_chat
from ksz_guide.playback import handle_playback_progress, playback_state
from core.utils.dialogue import Dialogue
from test_guide import DEVICE, Harness, beacon


@pytest.fixture
def fixed(monkeypatch):
    from core.handle import sendAudioHandle, reportHandle
    h = Harness(items=[beacon(guide_mode='fixed', museum_code='0755001')])
    conn = h.conn()
    conn.sentence_id = 'before'
    conn.device_attributes = {'agent_name': '专家-英语', 'language': 'en', 'role': 'child'}
    conn.dialogue = Dialogue()
    conn.features = {'playback_progress': True}
    conn.audio_format = 'opus'
    conn.sample_rate = 24000
    conn.client_abort = False
    conn.max_output_size = 0
    conn.client_is_speaking = False
    item = h.client.items[0]
    h.runtime.state(DEVICE).owner = conn
    h.runtime.location = lambda mac: item
    conn.server = SimpleNamespace(guide_runtime=h.runtime)
    client = SimpleNamespace(guide=AsyncMock(return_value={
        'segments': [{'text': 'First.', 'audio_id': 'one'}, {'text': 'Second.', 'audio_id': 'two'}, {'text': 'Third.', 'audio_id': 'three'}],
    }), audio=AsyncMock(return_value=(b'wav', 'wav')))
    monkeypatch.setattr(FixedClient, 'from_env', lambda: client)
    encode = Mock(return_value=[b'opus'])
    monkeypatch.setattr('ksz_guide.fixed.encode_audio', encode)
    messages = []

    async def send(instance, state, text=None):
        messages.append((state, text))
        if state == 'sentence_start':
            reply = playback_state(conn).replies[conn.sentence_id]
            from ksz_guide.playback import Segment
            reply.segments.append(Segment(text, sent=True))
        elif state == 'stop':
            playback_state(conn).replies[conn.sentence_id].server_stopped = True

    async def audio(instance, kind, frames, text, **kwargs):
        await send(instance, 'sentence_start', text)

    monkeypatch.setattr(sendAudioHandle, 'send_tts_message', send)
    monkeypatch.setattr(sendAudioHandle, 'sendAudioMessage', audio)
    monkeypatch.setattr(sendAudioHandle, '_wait_for_audio_completion', AsyncMock())
    report = Mock()
    monkeypatch.setattr(reportHandle, 'enqueue_tts_report', report)
    return SimpleNamespace(h=h, conn=conn, item=item, client=client, messages=messages, report=report, encode=encode)


def progress(conn, index, state='played'):
    handle_playback_progress(conn, {'type': 'playback_progress', 'session_id': conn.session_id,
        'response_id': conn.sentence_id, 'played_through': index, 'state': state})


async def wait_until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(), 2)


@pytest.mark.asyncio
async def test_completed_segments_only_enter_history_and_resume_reuses_snapshot(fixed):
    f = fixed
    task = asyncio.create_task(play_fixed(f.h.runtime, f.conn, f.item))
    await wait_until(lambda: ('sentence_start', 'First.') in f.messages)
    assert f.conn.dialogue.dialogue == []
    progress(f.conn, 1)
    await wait_until(lambda: ('sentence_start', 'Second.') in f.messages)
    assert [m.content for m in f.conn.dialogue.dialogue] == ['First.']
    f.conn.client_abort = True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert f.conn.ksz_fixed_tour.next_segment == 1
    assert [m.content for m in f.conn.dialogue.dialogue] == ['First.']
    f.messages.clear()
    task = asyncio.create_task(play_fixed(f.h.runtime, f.conn, f.item, resume=True, query='继续讲解'))
    await wait_until(lambda: ('sentence_start', 'Second.') in f.messages)
    assert ('sentence_start', 'First.') not in f.messages
    progress(f.conn, 1)
    await wait_until(lambda: ('sentence_start', 'Third.') in f.messages)
    progress(f.conn, 1, 'finished')
    assert await task is True
    assert [m.content for m in f.conn.dialogue.dialogue] == ['First.', '继续讲解', 'Second.', 'Third.']
    assert f.report.call_count == 3
    assert f.client.guide.await_count == 1
    assert [call.args[1]['audio_id'] for call in f.client.audio.await_args_list] == ['one', 'two', 'two', 'three']


@pytest.mark.asyncio
async def test_legacy_device_uses_completed_send_and_profile_is_bound_role(fixed):
    f = fixed
    f.conn.features = {}
    assert profile(f.conn) == ('expert', 'en-US')
    assert await play_fixed(f.h.runtime, f.conn, f.item)
    assert len(f.conn.dialogue.dialogue) == 3
    assert f.conn.client_is_speaking is False
    f.client.guide.assert_awaited_once_with(f.item, 'expert', 'en-US')
    assert resume_fixed_chat(f.conn, '这件展品是什么？') is False


@pytest.mark.asyncio
async def test_abort_while_fetching_does_not_put_unsaid_text_in_history(fixed):
    f = fixed
    started = asyncio.Event()

    async def audio(*args):
        started.set()
        await asyncio.Event().wait()

    f.client.audio.side_effect = audio
    task = asyncio.create_task(play_fixed(f.h.runtime, f.conn, f.item))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert f.conn.dialogue.dialogue == []
    assert f.conn.ksz_fixed_tour.next_segment == 0
    assert f.conn.client_is_speaking is False


@pytest.mark.asyncio
async def test_changed_location_or_profile_prevents_old_tour_resume(fixed):
    f = fixed
    f.conn.features = {}
    await play_fixed(f.h.runtime, f.conn, f.item)
    f.conn.ksz_fixed_tour.next_segment = 1
    f.conn.device_attributes['agent_name'] = '儿童-英语'
    assert await play_fixed(f.h.runtime, f.conn, f.item, resume=True) is False
    f.conn.device_attributes['agent_name'] = '专家-英语'
    other = replace(f.item, place_id='other')
    f.h.runtime.location = lambda mac: other
    assert resume_fixed_chat(f.conn, '继续讲解') is False
    f.h.runtime.location = lambda mac: f.item
    f.conn.device_attributes = {'agent_name': '其他角色', 'role': 'unknown'}
    assert resume_fixed_chat(f.conn, '继续讲解') is False


@pytest.mark.asyncio
async def test_modes_and_explicit_rag_fallback(fixed, monkeypatch):
    f = fixed
    rag = AsyncMock(return_value=True)
    monkeypatch.setattr(f.h.runtime, '_announce_rag', rag)
    f.client.guide.side_effect = GuideError('fixed_rag_http_404')
    assert await f.h.runtime._announce(f.conn, f.item) is False
    rag.assert_not_awaited()
    assert f.h.runtime.devices[DEVICE].rejection == 'fixed_rag_http_404'
    assert await f.h.runtime._announce(f.conn, replace(f.item, fixed_rag_fallback=True)) is True
    assert rag.await_count == 1
    assert await f.h.runtime._announce(f.conn, replace(f.item, guide_mode='off')) is False
    assert rag.await_count == 1
    assert await f.h.runtime._announce(f.conn, replace(f.item, guide_mode='rag')) is True


@pytest.mark.asyncio
async def test_chat_interrupts_fixed_audio_answers_question_then_resumes(fixed, monkeypatch):
    from core.handle import abortHandle
    f = fixed
    f.conn.loop = asyncio.get_running_loop()
    monkeypatch.setattr(abortHandle, 'handleAbortMessage', AsyncMock())
    task = asyncio.create_task(play_fixed(f.h.runtime, f.conn, f.item))
    await wait_until(lambda: ('sentence_start', 'First.') in f.messages)
    progress(f.conn, 1)
    await wait_until(lambda: ('sentence_start', 'Second.') in f.messages)
    calls = []

    @track_chat
    def chat(conn, query):
        calls.append((query, [m.content for m in conn.dialogue.dialogue]))
        return 'ordinary answer'

    assert await asyncio.to_thread(chat, f.conn, 'Who made this?') == 'ordinary answer'
    assert task.cancelled()
    assert calls == [('Who made this?', ['First.'])]
    assert f.conn.ksz_fixed_tour.task is None
    f.messages.clear()
    resume = asyncio.create_task(asyncio.to_thread(chat, f.conn, '继续讲解'))
    await wait_until(lambda: ('sentence_start', 'Second.') in f.messages)
    progress(f.conn, 1)
    await wait_until(lambda: ('sentence_start', 'Third.') in f.messages)
    progress(f.conn, 1, 'finished')
    assert await resume is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_audio_failure_clears_task_before_rag_fallback(fixed, monkeypatch):
    f = fixed
    f.conn.features = {}
    f.client.audio.side_effect = GuideError('fixed_rag_http_503')

    async def rag(conn, item):
        assert conn.ksz_fixed_tour.task is None
        return True

    monkeypatch.setattr(f.h.runtime, '_announce_rag', rag)
    assert await f.h.runtime._announce(f.conn, replace(f.item, fixed_rag_fallback=True)) is True
    assert f.conn.dialogue.dialogue == []


def test_actual_wav_decoding_to_device_frames():
    import io
    import wave
    from ksz_guide.fixed import encode_audio
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b'\0\0' * 1440)
    conn = SimpleNamespace(audio_format='opus', sample_rate=24000)
    frames = encode_audio(output.getvalue(), 'wav', conn)
    assert len(frames) == 1 and isinstance(frames[0], bytes) and frames[0]


@pytest.mark.asyncio
async def test_fixed_uses_existing_audio_wire_and_playback_receipts(fixed, monkeypatch):
    import json
    import importlib.util
    from pathlib import Path
    from core.handle import sendAudioHandle
    f = fixed
    source = Path(sendAudioHandle.__file__)
    spec = importlib.util.spec_from_file_location('fixed_audio_transport', source)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    f.conn.config = {}
    f.conn.logger = Mock()
    f.conn.tts = SimpleNamespace(tts_audio_first_sentence=True)
    f.conn.calling = False
    f.conn.close_after_chat = False
    f.conn.clearSpeakStatus = lambda: setattr(f.conn, 'client_is_speaking', False)
    f.conn.features = {'playback_progress': True}

    def receipt(data):
        if json.loads(data)['state'] == 'stop':
            asyncio.get_running_loop().call_soon(progress, f.conn, 1, 'finished')

    f.conn.websocket.send.side_effect = receipt
    monkeypatch.setattr(transport, 'sendAudio', AsyncMock())
    monkeypatch.setattr(transport, '_wait_for_audio_completion', AsyncMock())
    monkeypatch.setattr(sendAudioHandle, 'sendAudioMessage', transport.sendAudioMessage)
    monkeypatch.setattr(sendAudioHandle, 'send_tts_message', transport.send_tts_message)
    assert await play_fixed(f.h.runtime, f.conn, f.item)
    messages = [json.loads(call.args[0]) for call in f.conn.websocket.send.await_args_list]
    assert [(m['state'], m.get('text')) for m in messages] == [
        ('start', None), ('sentence_start', 'First.'), ('stop', None),
        ('start', None), ('sentence_start', 'Second.'), ('stop', None),
        ('start', None), ('sentence_start', 'Third.'), ('stop', None),
    ]
    assert transport.sendAudio.await_count == 3
    assert all(call.args[1] == [b'opus'] for call in transport.sendAudio.await_args_list)
    assert len(playback_state(f.conn).replies) == 3
    assert all(len(reply.segments) == 1 for reply in playback_state(f.conn).replies.values())
    assert all(reply.played_through == 1 for reply in playback_state(f.conn).replies.values())
    assert len({m['response_id'] for m in messages}) == 3
    assert all(m['segment_id'] == 1 for m in messages if m['state'] == 'sentence_start')


@pytest.mark.asyncio
async def test_disabled_place_keeps_location_but_never_schedules_announcement():
    h = Harness(items=[beacon(guide_mode='off')])
    try:
        await h.sync()
        await h.observe(1)
        assert h.runtime.location(DEVICE).place_id == 'place1'
        assert h.runtime.devices[DEVICE].pending is None
        h.announce.assert_not_awaited()
    finally:
        await h.runtime.close()


@pytest.mark.asyncio
async def test_client_uses_scoped_published_api_and_binary_audio():
    seen = []
    app = web.Application()

    async def guide(request):
        seen.append((request.path, dict(request.query), request.headers.get('Authorization')))
        return web.json_response({'museum_id': '0755001', 'place_id': 'place1', 'role': 'expert', 'language': 'en-US',
            'segments': [{'text': 'Welcome.', 'audio_id': 'asset_one'}]})

    async def audio(request):
        seen.append((request.path, dict(request.query), request.headers.get('Authorization')))
        return web.Response(body=b'RIFF0000WAVEdata', content_type='audio/wav')

    app.router.add_get('/api/v1/guides', guide)
    app.router.add_get('/api/v1/guides/audio/asset_one', audio)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        client = FixedClient(f'http://127.0.0.1:{port}', 'test-secret')
        item = beacon(guide_mode='fixed', museum_code='0755001')
        data = await client.guide(item, 'expert', 'en-US')
        assert await client.audio(item, data['segments'][0]) == (b'RIFF0000WAVEdata', 'wav')
        assert seen[0] == ('/api/v1/guides', {'museum_id': '0755001', 'place_id': 'place1', 'role': 'expert', 'language': 'en-US'}, 'Bearer test-secret')
        assert seen[1][1] == {'museum_id': '0755001'}
        with pytest.raises(GuideError, match='identity_mismatch'):
            await client.guide(item, 'child', 'en-US')
        with pytest.raises(GuideError, match='url_invalid'):
            await client.request('https://other.test/audio', audio=True)
    finally:
        await runner.cleanup()
