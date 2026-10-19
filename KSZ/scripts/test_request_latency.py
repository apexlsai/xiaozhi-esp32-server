import contextlib
import io
import json
import socket
import struct
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from latency_mqtt import encode_audio, ogg_packets, run_request
from request_latency import analyze, main, write_run_log


def log(second, message, tag="session"):
    return f"2026-10-13T06:00:{second:06.3f}Z [v-{tag}][core.connection]-INFO-{message}"


def agent_log(second, query="谢谢。", request_id="req-1", **changes):
    item = {"ts": f"2026-10-13T06:00:{second:06.3f}+00:00", "path": "/v1/chat/completions",
            "request_id": request_id, "user_preview": query, "duration_ms": 1000,
            "route": "rag", "status": "ok", "rag_ms": 200,
            "extra": {"llm_ms": 800, "llm_ttft_ms": 500, "tool_calls": 0}}
    item.update(changes)
    return log(second, json.dumps(item))


class LogAnalysisTests(unittest.TestCase):
    def test_each_run_writes_a_concise_human_log(self):
        report = {"query": "测试问题", "client": {"submit_to_audio_ms": 12.5},
                  "warnings": [], "events": [{"event": "tts sentence_start",
                                               "elapsed_ms": 10, "detail": "测试回复"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = write_run_log(report, "request", directory, 0)
            content = path.read_text()
        self.assertTrue(path.name.endswith("_request_测试问题.log"))
        self.assertIn("KSZ request latency diagnostic", content)
        self.assertIn("提交→首 UDP 音频: 12.50 ms", content)
        self.assertIn("tts sentence_start 测试回复", content)
        self.assertNotIn("report_json:", content)
        self.assertEqual(report["log_file"], str(path))

    def test_no_log_keeps_terminal_results_without_creating_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "server.log"
            empty = root / "empty.log"
            server.write_text(log(1, "大模型收到用户消息: 谢谢。"))
            empty.write_text("")
            argv = ["request_latency.py", "logs", "--query", "谢谢。",
                    "--server-log", str(server), "--mqtt-log", str(empty),
                    "--agent-log", str(empty), "--log-dir", str(root / "results"), "--no-log"]
            output = io.StringIO()
            with patch("sys.argv", argv), contextlib.redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertIn("问题：谢谢。", output.getvalue())
            self.assertNotIn("日志文件：", output.getvalue())
            self.assertFalse((root / "results").exists())

    def test_repeated_query_selects_last_and_deduplicates_agent_records(self):
        server = "\n".join([log(1, "大模型收到用户消息: 谢谢。"), log(2, "发送第一段语音: 不客气。"),
                            log(10, "ASR输入音频: 帧=20, 时长=1.20秒"), log(10.4, "识别文本: 谢谢。"),
                            log(10.401, "大模型收到用户消息: 谢谢。"), log(12, "发送第一段语音: 不客气。")])
        record = agent_log(11.5)
        report = analyze(server, log(9.99, "上行音频统计: aa_bb_cc_dd_ee_ff 收到=20 序列跨度=22 缺失=2"),
                         record + "\n" + record, "谢谢", device="aa:bb:cc:dd:ee:ff")
        self.assertEqual(report["stages"]["asr_ms"], 400)
        self.assertEqual(report["stages"]["uplink_missing_packets"], 2)
        self.assertEqual(report["stages"]["chat_to_first_sentence_ms"], 1599)
        self.assertEqual(len(report["agent_rounds"]), 1)
        self.assertEqual(report["stages"]["last_llm_first_chunk_to_sentence_ms_estimate"], 800)

    def test_tools_have_two_rounds_and_no_negative_tail_estimate(self):
        server = "\n".join([log(10, "大模型收到用户消息: 谢谢。"), log(11, "执行工具: get_weather，参数: {}"),
                            log(11.7, "发送给LLM的请求: [secret payload]"), log(12, "发送第一段语音: 晴。")])
        agent = agent_log(11) + "\n" + agent_log(13, request_id="req-2")
        report = analyze(server, "", agent, "谢谢")
        self.assertEqual(len(report["agent_rounds"]), 2)
        self.assertEqual(report["tools"][0]["execution_and_resume_ms_estimate"], 700)
        self.assertNotIn("last_llm_first_chunk_to_sentence_ms_estimate", report["stages"])
        self.assertNotIn("secret payload", json.dumps(report))

    def test_concurrent_session_skips_unattributed_tools(self):
        server = "\n".join([log(10, "大模型收到用户消息: 谢谢。"),
                            log(10.1, "大模型收到用户消息: 天气。", "other"),
                            log(10.2, "执行工具: get_weather，参数: {}"), log(12, "发送第一段语音: 不客气。")])
        report = analyze(server, "", "", "谢谢")
        self.assertEqual(report["tools"], [])
        self.assertTrue(any("并发" in value for value in report["warnings"]))

    def test_since_and_missing_timestamp(self):
        with self.assertRaises(ValueError):
            analyze(log(10, "大模型收到用户消息: 谢谢。"), "", "", "谢谢", "2026-10-13T07:00:00Z")
        with self.assertRaises(ValueError):
            analyze("没有时间戳", "", "", "谢谢")

    def test_invalid_ogg_is_rejected(self):
        with self.assertRaises(ValueError):
            ogg_packets(b"OggS")


def field(value):
    data = value.encode()
    return struct.pack(">H", len(data)) + data


def mqtt_packet(kind, data):
    size = len(data)
    prefix = bytearray([kind])
    while True:
        prefix.append((size & 127) | (128 if size > 127 else 0))
        size >>= 7
        if not size:
            break
    return bytes(prefix) + data


def receive_packet(connection):
    def read(size):
        data = bytearray()
        while len(data) < size:
            part = connection.recv(size - len(data))
            if not part:
                raise EOFError
            data.extend(part)
        return bytes(data)
    kind = read(1)[0] >> 4
    size, shift = 0, 0
    while True:
        byte = read(1)[0]
        size |= (byte & 127) << shift
        if not byte & 128:
            break
        shift += 7
    return kind, read(size)


class FakeChain:
    def __init__(self, mcp=False, no_audio=False):
        self.mcp, self.no_audio = mcp, no_audio
        self.errors = []
        self.messages = []
        self.key = b"k" * 16
        self.nonce = b"\x01\x00\x00\x00" + b"n" * 12
        self.tcp = socket.socket()
        self.tcp.bind(("127.0.0.1", 0))
        self.tcp.listen()
        self.tcp.settimeout(5)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(5)
        config = {"mqtt": {"endpoint": f"127.0.0.1:{self.tcp.getsockname()[1]}",
                            "client_id": "diag@@@aa_bb_cc_dd_ee_ff@@@aa_bb_cc_dd_ee_ff",
                            "username": "username", "password": "test-secret-do-not-print",
                            "publish_topic": "device-server", "subscribe_topic": "devices/test"}}
        class OtaHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(config).encode())
            def log_message(self, *args):
                pass
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), OtaHandler)
        self.http_thread = threading.Thread(target=self.http.serve_forever)
        self.broker_thread = threading.Thread(target=self.serve)

    def __enter__(self):
        self.http_thread.start()
        self.broker_thread.start()
        return self

    def __exit__(self, *args):
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(5)
        self.broker_thread.join(5)
        self.tcp.close()
        self.udp.close()

    def serve(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        try:
            connection, _ = self.tcp.accept()
            with connection:
                connection.settimeout(5)
                self.assert_kind(connection, 1)
                connection.sendall(mqtt_packet(0x20, b"\x00\x00"))
                body = self.assert_kind(connection, 8)
                connection.sendall(mqtt_packet(0x90, body[:2] + b"\x00"))
                def send(message):
                    connection.sendall(mqtt_packet(0x30, field("devices/test") + json.dumps(message).encode()))
                def rpc(ident, method, params=None):
                    send({"type": "mcp", "payload": {"jsonrpc": "2.0", "id": ident,
                          "method": method, "params": params or {}}})
                while True:
                    kind, data = receive_packet(connection)
                    if kind == 14:
                        break
                    if kind == 12:
                        connection.sendall(mqtt_packet(0xD0, b""))
                        continue
                    length = struct.unpack_from(">H", data)[0]
                    message = json.loads(data[2 + length:])
                    self.messages.append(message)
                    if message["type"] == "hello":
                        send({"type": "hello", "session_id": "session", "audio_params": {"sample_rate": 16000},
                              "udp": {"server": "127.0.0.1", "port": self.udp.getsockname()[1],
                                      "key": self.key.hex(), "nonce": self.nonce.hex(), "encryption": "aes-128-ctr"}})
                        if self.mcp:
                            rpc(1, "initialize")
                    elif message["type"] == "mcp":
                        ident = message["payload"]["id"]
                        if ident == 1:
                            rpc(2, "tools/list")
                        elif ident == 3:
                            self.respond(send, "工具已返回。", Cipher, algorithms, modes)
                    elif message["type"] == "listen" and message["state"] in ("detect", "stop"):
                        if message["state"] == "stop":
                            latest = 0
                            while latest < message["audio_sequence_end"]:
                                data, remote = self.udp.recvfrom(65535)
                                latest = struct.unpack_from(">I", data, 12)[0]
                            self.audio_remote = remote
                        if self.mcp:
                            send({"type": "stt", "text": message["text"]})
                            send({"type": "tts", "state": "start"})
                            rpc(3, "tools/call", {"name": "self.get_device_status", "arguments": {}})
                        else:
                            self.respond(send, message.get("text", "音频测试。"), Cipher, algorithms, modes)
        except EOFError:
            pass
        except Exception as error:
            self.errors.append(error)

    def respond(self, send, text, Cipher, algorithms, modes):
        if not hasattr(self, "audio_remote"):
            _, self.audio_remote = self.udp.recvfrom(65535)
        send({"type": "stt", "text": text})
        send({"type": "tts", "state": "start"})
        send({"type": "tts", "state": "sentence_start", "text": "谢谢。"})
        if not self.no_audio:
            header = bytearray(self.nonce)
            struct.pack_into(">H", header, 2, 3)
            struct.pack_into(">I", header, 12, 1)
            cipher = Cipher(algorithms.AES(self.key), modes.CTR(bytes(header))).encryptor()
            self.udp.sendto(header + cipher.update(b"\xf8\xff\xfe"), self.audio_remote)
        send({"type": "tts", "state": "stop"})

    def assert_kind(self, connection, expected):
        kind, body = receive_packet(connection)
        if kind != expected:
            raise AssertionError((kind, expected))
        return body

    def args(self, **changes):
        values = {"ota": f"http://127.0.0.1:{self.http.server_port}/ota/", "device": "aa:bb:cc:dd:ee:ff",
                  "audio": None, "text": "谢谢", "scenario": "mcp" if self.mcp else "chat", "mcp": self.mcp,
                  "timeout": 5, "mqtt_host": None, "udp_host": None}
        values.update(changes)
        return SimpleNamespace(**values)


class NativeRequestTests(unittest.TestCase):
    def test_mqtt_udp_and_mcp_without_credential_output(self):
        with FakeChain(mcp=True) as chain, contextlib.redirect_stdout(io.StringIO()) as output:
            report, code = run_request(chain.args())
        self.assertEqual(chain.errors, [])
        self.assertEqual(code, 0)
        self.assertEqual(report["client"]["downlink_packets"], 1)
        self.assertIn("self.get_device_status", report["observed_tools"])
        self.assertIn("mcp_discovery_ms", report["client"])
        self.assertNotIn("test-secret-do-not-print", output.getvalue() + json.dumps(report))

    def test_wav_upload_uses_declared_last_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00\x00" * 1920)
            expected = len(encode_audio(path))
            with FakeChain() as chain, contextlib.redirect_stdout(io.StringIO()):
                report, code = run_request(chain.args(audio=path, text=None))
        self.assertEqual(chain.errors, [])
        self.assertEqual(code, 0)
        stop = next(message for message in chain.messages if message.get("state") == "stop")
        self.assertEqual(stop["audio_packet_count"], expected)
        self.assertEqual(stop["audio_sequence_end"], expected + 1)
        self.assertEqual(report["client"]["uplink_packets"], expected)

    def test_tts_stop_without_udp_is_failure(self):
        with FakeChain(no_audio=True) as chain, contextlib.redirect_stdout(io.StringIO()):
            report, code = run_request(chain.args())
        self.assertEqual(chain.errors, [])
        self.assertEqual(code, 1)
        self.assertTrue(any("未收到 UDP" in message for message in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
