import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.websocket_server import WebSocketServer


def build_server():
    server = WebSocketServer.__new__(WebSocketServer)
    server.logger = MagicMock()
    server.logger.bind.return_value = server.logger
    server.device_connections = {}
    return server


class Connection:
    def __init__(self, device_id, peer_address):
        host, port = peer_address.rsplit(":", 1)
        self.device_id = device_id
        self.headers = {"client-id": device_id}
        self.websocket = type("WebSocket", (), {"remote_address": (host, int(port))})()
        self.need_bind = False
        self.tts = object()


def build_connection(device_id, peer_address):
    return Connection(device_id, peer_address)


class DevWebSocketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = build_server()
        self.first = build_connection("98:88:e0:6c:29:1c", "113.87.144.31:61157")
        self.second = build_connection("98:88:e0:6c:29:1c", "113.87.144.31:2231")
        self.server.device_connections = {
            self.first: {
                "device_id": self.first.device_id,
                "client_id": self.first.device_id,
                "peer_address": "113.87.144.31:61157",
                "connected_at": 1,
            },
            self.second: {
                "device_id": self.second.device_id,
                "client_id": self.second.device_id,
                "peer_address": "113.87.144.31:2231",
                "connected_at": 2,
            },
        }

    async def test_lists_registered_connections(self):
        response = await self.server._handle_dev_command({"action": "list_connections"})

        self.assertTrue(response["ok"])
        self.assertEqual(response["connections"][0]["peer_address"], "113.87.144.31:2231")

    async def test_finds_latest_device_connection_or_peer_connection(self):
        self.assertIs(
            self.server.find_device_connection({"device_id": "98:88:e0:6c:29:1c"}),
            self.second,
        )
        self.assertIs(
            self.server.find_device_connection({"peer_address": "113.87.144.31:61157"}),
            self.first,
        )

    async def test_registers_and_unregisters_connection(self):
        server = build_server()
        server.register_device_connection(self.first)

        self.assertEqual(server.list_device_connections()[0]["peer_address"], "113.87.144.31:61157")
        server.unregister_device_connection(self.first)
        self.assertEqual(server.list_device_connections(), [])

    async def test_speak_routes_to_target_connection(self):
        with patch(
            "core.handle.receiveAudioHandle.startToChat", new_callable=AsyncMock
        ) as start_to_chat:
            response = await self.server._handle_dev_command(
                {
                    "action": "speak",
                    "target": {"peer_address": "113.87.144.31:61157"},
                    "text": "请介绍附近展品",
                }
            )

        self.assertTrue(response["ok"])
        start_to_chat.assert_awaited_once_with(self.first, "请介绍附近展品")

    async def test_speak_rejects_invalid_target(self):
        response = await self.server._handle_dev_command(
            {
                "action": "speak",
                "target": {"device_id": "not-connected"},
                "text": "测试",
            }
        )

        self.assertEqual(response, {"ok": False, "error": "未找到目标在线设备"})

    async def test_speak_rejects_unready_device(self):
        self.second.tts = None

        response = await self.server._handle_dev_command(
            {
                "action": "speak",
                "target": {"device_id": self.second.device_id},
                "text": "测试",
            }
        )

        self.assertEqual(response, {"ok": False, "error": "目标设备语音模块尚未就绪"})
