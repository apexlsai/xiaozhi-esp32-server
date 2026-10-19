import json
import queue
import re
import select
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


PROMPTS = {"chat": "给我说一声谢谢", "weather": "今天深圳天气怎么样？",
           "news": "最近的新闻有什么？", "mcp": "请查询设备当前音量，使用设备状态工具。"}
DEVICE_TOOL = {"name": "self.get_device_status", "description": "查询设备当前状态，包括音量与屏幕亮度。",
               "inputSchema": {"type": "object", "properties": {}, "required": []}}


def ogg_packets(data):
    offset = 0
    pending = bytearray()
    result = []
    while offset < len(data):
        if data[offset:offset + 4] != b"OggS" or len(data) - offset < 27:
            raise ValueError("ffmpeg 输出无效的 Ogg 页面")
        count = data[offset + 26]
        sizes = data[offset + 27:offset + 27 + count]
        if len(sizes) != count:
            raise ValueError("Ogg 分段表不完整")
        offset += 27 + count
        for size in sizes:
            if offset + size > len(data):
                raise ValueError("Ogg 数据不完整")
            pending.extend(data[offset:offset + size])
            offset += size
            if size < 255:
                packet = bytes(pending)
                pending.clear()
                if not packet.startswith((b"OpusHead", b"OpusTags")):
                    result.append(packet)
    if pending or not result:
        raise ValueError("没有完整 Opus 音频帧")
    return result


def encode_audio(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ar", "16000", "-ac", "1",
                             "-c:a", "libopus", "-application", "voip", "-frame_duration", "60",
                             "-f", "ogg", "pipe:1"], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("音频转码失败：" + result.stderr.decode(errors="replace")[:300])
    return ogg_packets(result.stdout)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_ota(url, device, timeout):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("--ota 必须为不含用户名/密码的 HTTP(S) URL")
    body = {"application": {"version": "1.0.0", "elf_sha256": "ksz-latency"}, "board": {"mac": device}}
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Device-Id": device, "Client-Id": device})
    handlers = [NoRedirect]
    if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        handlers.append(urllib.request.ProxyHandler({}))
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
            ota = json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f"OTA HTTP {error.code}") from None
    except urllib.error.URLError:
        raise ValueError("OTA 连接失败") from None
    config = ota.get("mqtt")
    if not config or not all(config.get(key) for key in
                             ("endpoint", "client_id", "username", "password", "publish_topic", "subscribe_topic")):
        raise ValueError("OTA 未返回完整 MQTT 配置；检查设备绑定/激活与服务端配置")
    parts = config["client_id"].split("@@@")
    if len(parts) != 3 or parts[1].lower() != device.replace(":", "_").lower():
        raise ValueError("OTA MQTT 身份与测试 MAC 不一致")
    return config


def run_request(args):
    try:
        import paho.mqtt.client as mqtt
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise ValueError("请用 uv run KSZ/scripts/request_latency.py request ... 自动安装依赖") from None
    if not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", args.device):
        raise ValueError("--device 必须为 MAC 地址")
    packets = encode_audio(args.audio) if args.audio else None
    text = args.text or PROMPTS[args.scenario]
    report = {"query": text if not packets else str(args.audio), "recognized_text": None,
              "transport": "mqtt+udp", "input": "audio" if packets else "text",
              "mcp_enabled": args.mcp, "client": {}, "observed_tools": [], "warnings": [], "events": []}
    metrics = report["client"]
    begun = time.monotonic()
    deadline = begun + args.timeout
    config = fetch_ota(args.ota, args.device, min(10, args.timeout))
    metrics["ota_ms"] = round((time.monotonic() - begun) * 1000, 2)
    endpoint = urllib.parse.urlsplit("mqtt://" + config["endpoint"])
    if not endpoint.hostname or not endpoint.port or endpoint.path or endpoint.username or endpoint.password:
        raise ValueError("OTA MQTT endpoint 无效")
    inbox = queue.Queue()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config["client_id"],
                         protocol=mqtt.MQTTv311, reconnect_on_failure=False)
    client.username_pw_set(config["username"], config["password"])
    client.on_connect = lambda c, u, f, reason, properties: inbox.put((time.monotonic(), "connect", reason))
    client.on_subscribe = lambda c, u, mid, reasons, properties: inbox.put((time.monotonic(), "subscribe", reasons))
    client.on_message = lambda c, u, message: inbox.put((time.monotonic(), "message", bytes(message.payload)))
    client.on_disconnect = lambda c, u, f, reason, properties: inbox.put((time.monotonic(), "disconnect", reason))
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("0.0.0.0", 0))
    udp.setblocking(False)
    hello = None
    submitted = None
    audio_times = []
    audio_sequences = set()
    tts_started = False
    tts_start_at = None
    tts_stopped = False
    discovered = False
    downlink_params = None

    def send(message):
        outcome = client.publish(config["publish_topic"], json.dumps(message, ensure_ascii=False), qos=0)
        if outcome.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ValueError("MQTT 消息发送失败")

    def event(name, at, detail=None):
        row = {"event": name, "elapsed_ms": round((at - begun) * 1000, 2)}
        if detail is not None:
            row["detail"] = detail
        report["events"].append(row)
        print(f"  +{row['elapsed_ms']:9.2f} ms {name}" + (f" {detail}" if detail is not None else ""), flush=True)

    def observe_tool(name):
        if name not in report["observed_tools"]:
            report["observed_tools"].append(name)

    def handle(at, kind, payload):
        nonlocal hello, tts_started, tts_start_at, tts_stopped, discovered
        if kind == "disconnect":
            if not tts_stopped:
                raise ValueError("请求完成前 MQTT 断开")
            return
        if kind != "message":
            return
        try:
            message = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("收到无效 MQTT JSON") from None
        typ = message.get("type")
        if typ == "hello":
            hello = message
            event("hello", at)
        elif typ == "mcp":
            rpc = message.get("payload", {})
            method = rpc.get("method")
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "ksz-latency-simulator", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [DEVICE_TOOL] if args.mcp else []}
                discovered = True
                metrics["mcp_discovery_ms"] = round((at - hello_sent) * 1000, 2)
            elif method == "tools/call":
                tool = (rpc.get("params") or {}).get("name")
                observe_tool(tool)
                result = {"content": [{"type": "text", "text": json.dumps({
                    "audio_speaker": {"volume": 50}, "screen": {"brightness": 80}, "simulated": True})}],
                          "isError": False} if args.mcp and tool == DEVICE_TOOL["name"] else {
                              "content": [{"type": "text", "text": "unsupported tool"}], "isError": True}
            else:
                if "id" in rpc and method:
                    send({"type": "mcp", "payload": {"jsonrpc": "2.0", "id": rpc["id"],
                         "error": {"code": -32601, "message": "Method not found"}}})
                return
            event("mcp " + method, at, (rpc.get("params") or {}).get("name") if method == "tools/call" else None)
            send({"type": "mcp", "payload": {"jsonrpc": "2.0", "id": rpc["id"], "result": result}})
        elif submitted is not None and typ == "stt":
            value = message.get("text", "")
            if value.startswith("% "):
                observe_tool(value[2:].strip())
                event("tool display", at, value[2:])
            elif "submit_to_stt_ms" not in metrics:
                report["recognized_text"] = value
                metrics["submit_to_stt_ms"] = round((at - submitted) * 1000, 2)
                event("stt", at, value)
        elif submitted is not None and typ == "tts":
            state = message.get("state")
            if state == "start":
                tts_started = True
                tts_start_at = at if tts_start_at is None else tts_start_at
            elif state == "sentence_start" and "submit_to_sentence_ms" not in metrics:
                metrics["submit_to_sentence_ms"] = round((at - submitted) * 1000, 2)
            elif state == "stop":
                metrics["submit_to_stop_ms"] = round((at - submitted) * 1000, 2)
                tts_stopped = True
            event("tts " + str(state), at, message.get("text"))
        elif typ in ("goodbye", "error"):
            raise ValueError("服务端结束会话或返回错误；请检查服务端日志")

    def pump(wait=0.01):
        if time.monotonic() >= deadline:
            raise ValueError("请求超时（可能缺少 STT、TTS stop 或 UDP 音频）")
        while not inbox.empty():
            at, kind, payload = inbox.get_nowait()
            handle(at, kind, payload)
        if select.select([udp], [], [], min(wait, max(0, deadline - time.monotonic())))[0]:
            while True:
                try:
                    data = udp.recv(65535)
                except BlockingIOError:
                    break
                if submitted is None or downlink_params is None or len(data) < 17:
                    continue
                header, ciphertext = data[:16], data[16:]
                if len(ciphertext) != struct.unpack_from(">H", header, 2)[0]:
                    continue
                key, nonce = downlink_params
                if header[0] != 1 or header[4:8] != nonce[4:8]:
                    continue
                decrypted = Cipher(algorithms.AES(key), modes.CTR(header)).decryptor().update(ciphertext)
                if not decrypted:
                    continue
                sequence = struct.unpack_from(">I", header, 12)[0]
                if sequence in audio_sequences:
                    continue
                audio_sequences.add(sequence)
                at = time.monotonic()
                audio_times.append(at)
                if len(audio_times) == 1:
                    metrics["submit_to_audio_ms"] = round((at - submitted) * 1000, 2)
                    event("first UDP audio", at)

    def await_kind(kind):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("MQTT 建连/订阅超时")
            try:
                at, name, value = inbox.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if name == kind:
                return at, value
            handle(at, name, value)

    exit_code = 0
    try:
        connecting = time.monotonic()
        client.connect_timeout = min(10, args.timeout)
        client.connect(args.mqtt_host or endpoint.hostname, endpoint.port, keepalive=30)
        client.loop_start()
        at, reason = await_kind("connect")
        if reason.is_failure:
            raise ValueError("MQTT 鉴权失败")
        metrics["mqtt_connect_ms"] = round((at - connecting) * 1000, 2)
        event("MQTT connected", at)
        subscribing = time.monotonic()
        status, _ = client.subscribe(config["subscribe_topic"], qos=0)
        if status != mqtt.MQTT_ERR_SUCCESS:
            raise ValueError("MQTT 订阅发送失败")
        at, reasons = await_kind("subscribe")
        if any(reason.is_failure for reason in reasons):
            raise ValueError("MQTT 订阅被拒绝")
        metrics["mqtt_subscribe_ms"] = round((at - subscribing) * 1000, 2)
        hello_sent = time.monotonic()
        send({"type": "hello", "version": 3, "transport": "udp", "features": {"mcp": args.mcp},
              "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}})
        while hello is None or (args.mcp and not discovered):
            pump()
        hello_event = next(row for row in report["events"] if row["event"] == "hello")
        metrics["hello_ms"] = round(hello_event["elapsed_ms"] - (hello_sent - begun) * 1000, 2)
        params = hello.get("udp", {})
        if params.get("encryption") != "aes-128-ctr":
            raise ValueError("不支持的 UDP 加密方式")
        key, nonce = bytes.fromhex(params["key"]), bytes.fromhex(params["nonce"])
        if len(key) != 16 or len(nonce) != 16:
            raise ValueError("UDP key/nonce 长度无效")
        downlink_params = key, nonce
        remote = (socket.gethostbyname(args.udp_host or params["server"]), int(params["port"]))
        sequence = 0

        def send_audio(opus, audio_timestamp):
            nonlocal sequence
            sequence += 1
            header = bytearray(nonce)
            struct.pack_into(">H", header, 2, len(opus))
            struct.pack_into(">I", header, 8, audio_timestamp)
            struct.pack_into(">I", header, 12, sequence)
            encrypted = Cipher(algorithms.AES(key), modes.CTR(bytes(header))).encryptor().update(opus)
            udp.sendto(header + encrypted, remote)

        send_audio(b"\xf8\xff\xfe", 0)
        ready_until = time.monotonic() + 0.1
        while time.monotonic() < ready_until:
            pump()
        if packets:
            send({"type": "listen", "state": "start", "mode": "manual"})
            upload_start = time.monotonic()
            for i, opus in enumerate(packets):
                due = upload_start + (i + 1) * 0.06
                while time.monotonic() < due:
                    pump(min(0.01, due - time.monotonic()))
                send_audio(opus, i * 60)
            metrics["uplink_packets"] = len(packets)
            metrics["input_audio_ms"] = len(packets) * 60
            submitted = time.monotonic()
            send({"type": "listen", "state": "stop", "audio_sequence_end": sequence,
                  "audio_packet_count": len(packets)})
        else:
            submitted = time.monotonic()
            send({"type": "listen", "state": "detect", "text": text})
        event("request submitted", submitted)
        while not tts_stopped:
            pump()
        drain_until = min(deadline, time.monotonic() + 0.2)
        while time.monotonic() < drain_until:
            pump()
        metrics["downlink_packets"] = len(audio_times)
        if audio_times:
            metrics["audio_span_ms"] = round((audio_times[-1] - audio_times[0]) * 1000, 2)
            if "submit_to_sentence_ms" in metrics:
                metrics["sentence_to_audio_ms"] = round(metrics["submit_to_audio_ms"] - metrics["submit_to_sentence_ms"], 2)
            metrics["downlink_missing_sequences"] = max(audio_sequences) - min(audio_sequences) + 1 - len(audio_sequences)
        else:
            report["warnings"].append("收到 TTS stop 但未收到 UDP 音频；控制链路成功不能证明音频送达")
            exit_code = 1
        if not tts_started:
            report["warnings"].append("本轮未收到 tts:start，协议启停不完整")
            exit_code = 1
        if report["warnings"]:
            exit_code = 1
        if audio_times and tts_start_at is not None and audio_times[0] < tts_start_at:
            report["warnings"].append("UDP 首包先于 MQTT tts:start 到达，需结合服务端发送日志区分跨通道乱序与协议问题。")
        if args.scenario == "mcp":
            report["warnings"].append("MCP 返回模拟设备状态，只测调用链路，不代表真实固件耗时。")
        if not packets:
            report["warnings"].append("文本模式跳过采集/上行音频/ASR；submit→STT 不是识别耗时。")
        report["warnings"].append("客户端首包是 UDP 收到时间，不含浏览器桥接、解码和扬声器播放；场景由模型选工具。")
    except (ValueError, OSError, KeyError) as error:
        report["warnings"].append(str(error) if isinstance(error, ValueError) else "连接/协议失败，请检查端口、OTA 和服务日志")
        exit_code = 1
    finally:
        client.disconnect()
        client.loop_stop()
        udp.close()
    report["events"].sort(key=lambda row: row["elapsed_ms"])
    metrics.setdefault("downlink_packets", len(audio_times))
    return report, exit_code
