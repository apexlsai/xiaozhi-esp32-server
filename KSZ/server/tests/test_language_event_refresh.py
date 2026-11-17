import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.handle.deviceEventHandle import handle_device_event


@pytest.mark.asyncio
async def test_language_change_refreshes_missing_current_agent():
    websocket = MagicMock()
    websocket.send = AsyncMock()
    websocket.close = AsyncMock()
    conn = SimpleNamespace(
        device_id="aa:bb:cc:dd:ee:ff",
        read_config_from_api=True,
        websocket=websocket,
        device_attributes={},
        device_language=None,
    )
    refreshed = 0

    async def refresh_attributes():
        nonlocal refreshed
        refreshed += 1
        conn.device_attributes = {"agent_name": "小硕-汉语"}

    conn._refresh_device_attributes = refresh_attributes

    with patch(
        "core.handle.deviceEventHandle.rebind_device_agent",
        new_callable=AsyncMock,
    ) as rebind, patch(
        "core.handle.deviceEventHandle.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        rebind.return_value = {"agentName": "小硕-日语"}
        await handle_device_event(
            conn,
            {
                "type": "device_event",
                "event": "language_change",
                "request_id": "lang-refresh",
                "payload": {"language": "ja", "dev": False},
            },
        )

    assert refreshed == 1
    assert rebind.await_args.kwargs == {
        "device_id": "aa:bb:cc:dd:ee:ff",
        "current_agent_name": "小硕-汉语",
        "target_agent_name": "小硕-日语",
        "confirm": True,
        "language": "ja",
        "dev": False,
    }
    result = json.loads(websocket.send.await_args.args[0])
    assert result["success"] is True
    assert result["request_id"] == "lang-refresh"
