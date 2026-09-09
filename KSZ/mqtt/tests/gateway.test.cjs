const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const dgram = require('node:dgram');
const net = require('node:net');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { createRequire } = require('node:module');
const { test } = require('node:test');
const { gatewayConfig } = require('../start.cjs');
const { verifyOta } = require('../verify-ota.cjs');

const gatewayDir = process.env.GATEWAY_DIR || '/app';
const { WebSocketServer } = createRequire(path.join(gatewayDir, 'package.json'))('ws');
const signatureKey = 'KszGatewayDemo8Z';
const serverSecret = 'KszBackendDemo8Z';
const mac = 'aa:bb:cc:dd:ee:ff';
const safeMac = mac.replaceAll(':', '_');
const credentials = {
    client_id: `GID_default@@@${safeMac}@@@${safeMac}`,
    username: Buffer.from(JSON.stringify({ ip: '127.0.0.1' })).toString('base64'),
    publish_topic: 'device-server',
    subscribe_topic: `devices/p2p/${safeMac}`,
};
credentials.password = crypto.createHmac('sha256', signatureKey)
    .update(`${credentials.client_id}|${credentials.username}`).digest('base64');

class Inbox {
    items = [];
    waiters = [];
    push(item) {
        const index = this.waiters.findIndex((waiter) => waiter.predicate(item));
        if (index < 0) this.items.push(item);
        else {
            const [waiter] = this.waiters.splice(index, 1);
            clearTimeout(waiter.timer);
            waiter.resolve(item);
        }
    }
    next(predicate = () => true) {
        const index = this.items.findIndex(predicate);
        if (index >= 0) return Promise.resolve(this.items.splice(index, 1)[0]);
        return new Promise((resolve, reject) => {
            const waiter = { predicate, resolve };
            waiter.timer = setTimeout(() => {
                this.waiters.splice(this.waiters.indexOf(waiter), 1);
                reject(new Error('Timed out waiting for protocol message'));
            }, 5000);
            this.waiters.push(waiter);
        });
    }
}

function textField(text) {
    const body = Buffer.from(text);
    const size = Buffer.alloc(2);
    size.writeUInt16BE(body.length);
    return Buffer.concat([size, body]);
}

function packet(type, body) {
    const size = [];
    let remaining = body.length;
    do {
        const byte = remaining % 128;
        remaining = Math.floor(remaining / 128);
        size.push(byte | (remaining ? 128 : 0));
    } while (remaining);
    return Buffer.concat([Buffer.from([type, ...size]), body]);
}

class Device {
    inbox = new Inbox();
    buffer = Buffer.alloc(0);
    vision = null;
    toolLists = 0;
    constructor(port) {
        this.socket = net.connect(port, '127.0.0.1');
        this.socket.on('error', () => {});
        this.socket.on('data', (chunk) => {
            this.buffer = Buffer.concat([this.buffer, chunk]);
            while (this.buffer.length > 1) {
                let size = 0;
                let multiplier = 1;
                let position = 1;
                let byte;
                do {
                    if (position >= this.buffer.length) return;
                    byte = this.buffer[position++];
                    size += (byte & 127) * multiplier;
                    multiplier *= 128;
                } while (byte & 128);
                if (this.buffer.length < position + size) return;
                const type = this.buffer[0] >> 4;
                const body = this.buffer.subarray(position, position + size);
                this.buffer = this.buffer.subarray(position + size);
                if (type === 3) {
                    const topicLength = body.readUInt16BE(0);
                    assert.equal(body.toString('utf8', 2, 2 + topicLength), credentials.subscribe_topic);
                    const json = JSON.parse(body.subarray(2 + topicLength).toString());
                    this.onMessage(json);
                    this.inbox.push(json);
                } else this.inbox.push({ packetType: type, body });
            }
        });
    }
    async connect(auth = credentials) {
        await once(this.socket, 'connect');
        this.socket.write(packet(0x10, Buffer.concat([
            textField('MQTT'), Buffer.from([4, 0xc2, 0, 30]),
            textField(auth.client_id), textField(auth.username), textField(auth.password),
        ])));
        return (await this.inbox.next((item) => item.packetType === 2)).body[1];
    }
    send(json) {
        this.socket.write(packet(0x30, Buffer.concat([
            textField(credentials.publish_topic), Buffer.from(JSON.stringify(json)),
        ])));
    }
    async subscribe() {
        this.socket.write(packet(0x82, Buffer.concat([
            Buffer.from([0, 1]), textField(credentials.subscribe_topic), Buffer.from([0]),
        ])));
        assert.deepEqual((await this.inbox.next((item) => item.packetType === 9)).body, Buffer.from([0, 1, 0]));
    }
    hello() {
        this.send({ type: 'hello', version: 3, transport: 'udp',
            features: { mcp: true, vision_stream: true },
            audio_params: { format: 'opus', sample_rate: 16000, channels: 1, frame_duration: 60 } });
        return this.inbox.next((item) => item.type === 'hello');
    }
    onMessage(json) {
        if (json.type !== 'mcp' || json.payload.id === undefined || !json.payload.method) return;
        const { id, method, params } = json.payload;
        let result;
        if (method === 'initialize') {
            this.vision = params.capabilities.vision;
            result = { protocolVersion: '2024-11-05', capabilities: { tools: {} },
                serverInfo: { name: 'ksz-simulated-device', version: '1' } };
        } else if (method === 'tools/list') {
            this.toolLists++;
            result = { tools: this.vision ? [{ name: 'self.camera.take_photo', inputSchema: { type: 'object' } }] : [] };
        } else result = { content: [{ type: 'text', text: '已看到展品。' }] };
        this.send({ type: 'mcp', payload: { jsonrpc: '2.0', id, result } });
    }
}

async function freeTcpPort() {
    const server = net.createServer();
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const port = server.address().port;
    await new Promise((resolve) => server.close(resolve));
    return port;
}

test('configuration rejects missing secrets and incorrect gateway URLs', () => {
    const env = { PUBLIC_IP: 'mqtt.example.com', MQTT_SIGNATURE_KEY: signatureKey,
        SERVER_SECRET: serverSecret, MQTT_PORT: '1883', UDP_PORT: '8884', API_PORT: '8007',
        MQTT_CHAT_SERVER: 'ws://127.0.0.1:8000/xiaozhi/v1/?from=mqtt_gateway' };
    assert.equal(gatewayConfig(env).debug, false);
    for (const name of ['PUBLIC_IP', 'MQTT_SIGNATURE_KEY', 'SERVER_SECRET']) {
        assert.throws(() => gatewayConfig({ ...env, [name]: '' }));
    }
    for (const value of ['ws://localhost:8000/xiaozhi/v1/',
        'ws://localhost:8000/xiaozhi/v1/?from=mqtt_gateway&extra=1', 'https://localhost/']) {
        assert.throws(() => gatewayConfig({ ...env, MQTT_CHAT_SERVER: value }));
    }
    assert.throws(() => gatewayConfig({ ...env, MQTT_PORT: '8007' }));
    assert.throws(() => gatewayConfig({ ...env, UDP_PORT: '65536' }));
    assert.throws(() => gatewayConfig({ ...env, MQTT_SIGNATURE_KEY: 'testAbcd' }));
});

test('OTA checks identity, topics, fallback and signature without returning credentials', () => {
    const response = { mqtt: { ...credentials, endpoint: 'mqtt.example.com:1883' },
        websocket: { url: 'ws://server.example.com:8000/xiaozhi/v1/' } };
    const summary = verifyOta(response, mac, signatureKey);
    assert.equal(summary.signature_checked, true);
    assert.ok(!JSON.stringify(summary).includes(credentials.password));
    assert.throws(() => verifyOta(response, '11:22:33:44:55:66', signatureKey));
    assert.throws(() => verifyOta(response, mac, 'wrong'));
    assert.throws(() => verifyOta({ ...response, error: 'language failed' }, mac));
    assert.throws(() => verifyOta({ websocket: response.websocket }, mac));
    assert.throws(() => verifyOta({ mqtt: response.mqtt }, mac));
});

test('KSZ MQTT gateway protocol integration', { timeout: 60000 }, async (t) => {
    const backend = new WebSocketServer({ host: '127.0.0.1', port: 0 });
    await once(backend, 'listening');
    const backendInbox = new Inbox();
    let sessionNumber = 0;
    let backendSocket;
    backend.on('connection', (socket, request) => {
        backendSocket = socket;
        const session = `session-${++sessionNumber}`;
        assert.equal(request.url, '/xiaozhi/v1/?from=mqtt_gateway');
        assert.equal(request.headers['device-id'], mac);
        assert.equal(request.headers['client-id'], safeMac);
        const [signature, timestamp] = request.headers.authorization.slice(7).split('.');
        assert.equal(signature, crypto.createHmac('sha256', serverSecret)
            .update(`${safeMac}|${mac}|${timestamp}`).digest('base64url'));
        socket.on('close', () => backendInbox.push({ closed: session }));
        socket.on('message', (data, binary) => {
            if (binary) {
                backendInbox.push({ audio: data });
                socket.send(data, { binary: true });
                return;
            }
            const json = JSON.parse(data);
            backendInbox.push(json);
            if (json.type === 'hello') {
                assert.deepEqual(json.features, { mcp: true, vision_stream: true });
                socket.send(JSON.stringify({ type: 'hello', session_id: session, audio_params: json.audio_params }));
                socket.send(JSON.stringify({ type: 'mcp', payload: { jsonrpc: '2.0', id: 1, method: 'initialize',
                    params: { capabilities: { vision: { url: 'http://vision.example/mcp/vision/explain', token: session } } } } }));
            } else if (json.type === 'mcp' && json.payload.id === 1 && json.payload.result) {
                socket.send(JSON.stringify({ type: 'mcp', payload: { jsonrpc: '2.0', id: 2, method: 'tools/list' } }));
            }
        });
    });
    t.after(() => {
        for (const client of backend.clients) client.terminate();
        backend.close();
    });
    const mqttPort = await freeTcpPort();
    const apiPort = await freeTcpPort();
    const probe = dgram.createSocket('udp4');
    probe.bind(0, '127.0.0.1');
    await once(probe, 'listening');
    const udpPort = probe.address().port;
    probe.close();
    const env = { ...process.env, PUBLIC_IP: '127.0.0.1', MQTT_SIGNATURE_KEY: signatureKey,
        SERVER_SECRET: serverSecret, MQTT_PORT: String(mqttPort), UDP_PORT: String(udpPort),
        API_PORT: String(apiPort), MQTT_CHAT_SERVER: `ws://127.0.0.1:${backend.address().port}/xiaozhi/v1/?from=mqtt_gateway` };
    const child = spawn(process.execPath, ['start.cjs'], { cwd: gatewayDir, env, stdio: ['ignore', 'pipe', 'pipe'] });
    const output = new Inbox();
    let logs = '';
    for (const stream of [child.stdout, child.stderr]) stream.on('data', (data) => {
        logs += data.toString();
        output.push(logs);
    });
    t.after(async () => {
        if (child.exitCode === null && child.signalCode === null) {
            child.kill();
            await once(child, 'exit');
        }
        assert.ok(!logs.includes(signatureKey));
        assert.ok(!logs.includes(serverSecret));
    });
    await output.next((text) => text.includes('管理API服务启动在端口'));

    await t.test('healthcheck requires MQTT and authenticated management API', async () => {
        const check = spawn(process.execPath, ['healthcheck.cjs'], { cwd: gatewayDir, env });
        assert.equal((await once(check, 'exit'))[0], 0);
    });
    await t.test('bad signature and legacy unsigned identity are rejected before CONNACK success', async () => {
        for (const auth of [{ ...credentials, password: 'wrong' },
            { ...credentials, client_id: `GID_default@@@${safeMac}`, password: '' }]) {
            const device = new Device(mqttPort);
            t.after(() => device.socket.destroy());
            assert.equal(await device.connect(auth), 5);
        }
        assert.equal(sessionNumber, 0);
    });
    await t.test('signed uppercase MAC identities remain accepted', async () => {
        const auth = { ...credentials, client_id: credentials.client_id.toUpperCase() };
        auth.password = crypto.createHmac('sha256', signatureKey)
            .update(`${auth.client_id}|${auth.username}`).digest('base64');
        const device = new Device(mqttPort);
        t.after(() => device.socket.destroy());
        assert.equal(await device.connect(auth), 0);
        device.socket.destroy();
    });

    const device = new Device(mqttPort);
    t.after(() => device.socket.destroy());
    assert.equal(await device.connect(), 0);
    await device.subscribe();
    await t.test('an idle MQTT connection does not start a backend session for beacon events', async () => {
        device.send({ type: 'device_event', event: 'beacon_change', payload: { beacon_id: 'idle-beacon' } });
        await device.inbox.next((item) => item.type === 'goodbye');
        assert.equal(sessionNumber, 0);
        assert.equal(device.vision, null);
    });
    let hello;
    await t.test('hello and MCP vision initialization reach the device before tool discovery', async () => {
        hello = await device.hello();
        assert.equal(hello.transport, 'udp');
        assert.equal(hello.udp.port, udpPort);
        assert.equal(hello.udp.server, '127.0.0.1');
        const tools = await backendInbox.next((item) => item.type === 'mcp' && item.payload.id === 2);
        assert.equal(tools.payload.result.tools[0].name, 'self.camera.take_photo');
        assert.equal(device.vision.token, hello.session_id);
        assert.equal(device.toolLists, 1);
    });
    await t.test('custom KSZ events, results, subtitles and MCP vision metadata are preserved', async () => {
        for (const event of [
            { type: 'device_event', event: 'beacon_change', beacon_mac: { beacon_id: 'DA:01:16:00:08:87', rssi: -65 } },
            { type: 'device_event', event: 'language_change', request_id: 'lang-1', payload: { language: 'zh-CN-yue', dev: true } },
            { type: 'abort', reason: 'wake_word_detected' },
        ]) {
            device.send(event);
            assert.deepEqual(await backendInbox.next((item) => item.type === event.type && item.event === event.event), event);
        }
        for (const event of [
            { type: 'device_event_result', event: 'language_change', request_id: 'lang-1', success: false, error: '目标不存在' },
            { type: 'tts', state: 'sentence_start', text: '展品介绍。' },
            { type: 'llm', emotion: 'happy', text: '😆' },
        ]) {
            backendSocket.send(JSON.stringify(event));
            assert.deepEqual(await device.inbox.next((item) => item.type === event.type), event);
        }
        backendSocket.send(JSON.stringify({ type: 'mcp', payload: { jsonrpc: '2.0', id: 3, method: 'tools/call',
            params: { name: 'self.camera.take_photo', arguments: { question: '这是什么？' }, _meta: { vision_request_id: 'vision-1' } } } }));
        const request = await device.inbox.next((item) => item.type === 'mcp' && item.payload.id === 3);
        assert.equal(request.payload.params._meta.vision_request_id, 'vision-1');
        const result = await backendInbox.next((item) => item.type === 'mcp' && item.payload.id === 3);
        assert.equal(result.payload.result.content[0].text, '已看到展品。');
    });

    const udp = dgram.createSocket('udp4');
    udp.bind(0, '127.0.0.1');
    await once(udp, 'listening');
    t.after(() => udp.close());
    await t.test('encrypted UDP audio crosses the WebSocket bridge in both directions', async () => {
        const opus = Buffer.from([0xf8, 0xff, 0xfe]);
        const header = Buffer.from(hello.udp.nonce, 'hex');
        header.writeUInt16BE(opus.length, 2);
        header.writeUInt32BE(123, 8);
        header.writeUInt32BE(1, 12);
        const key = Buffer.from(hello.udp.key, 'hex');
        const cipher = crypto.createCipheriv(hello.udp.encryption, key, header);
        const reply = once(udp, 'message');
        udp.send(Buffer.concat([header, cipher.update(opus), cipher.final()]), udpPort, '127.0.0.1');
        const received = await backendInbox.next((item) => item.audio);
        assert.equal(received.audio.readUInt32BE(8), 123);
        assert.deepEqual(received.audio.subarray(16), opus);
        const [audio] = await Promise.race([reply, new Promise((_, reject) => setTimeout(() => reject(new Error('No UDP audio received')), 3000))]);
        const decipher = crypto.createDecipheriv(hello.udp.encryption, key, audio.subarray(0, 16));
        assert.deepEqual(Buffer.concat([decipher.update(audio.subarray(16)), decipher.final()]), opus);
    });
    await t.test('backend close produces goodbye and a new hello creates a fresh session', async () => {
        backendSocket.send(JSON.stringify({ type: 'device_event_result', event: 'language_change', success: true }));
        backendSocket.close();
        assert.equal((await device.inbox.next((item) => item.type === 'device_event_result')).success, true);
        assert.equal((await device.inbox.next((item) => item.type === 'goodbye')).session_id, hello.session_id);
        const oldSession = hello.session_id;
        hello = await device.hello();
        assert.notEqual(hello.session_id, oldSession);
        await backendInbox.next((item) => item.type === 'mcp' && item.payload.id === 2);
        assert.equal(device.vision.token, hello.session_id);
    });
    await t.test('repeated hello replaces the active bridge without a stale goodbye closing the new session', async () => {
        const previous = hello;
        hello = await device.hello();
        await backendInbox.next((item) => item.closed === previous.session_id);
        await backendInbox.next((item) => item.type === 'mcp' && item.payload.id === 2);
        assert.notEqual(hello.session_id, previous.session_id);
        assert.notEqual(hello.udp.key, previous.udp.key);
        device.send({ type: 'device_event', event: 'beacon_change', payload: { beacon_id: 'new-session' } });
        const event = await backendInbox.next((item) => item.payload?.beacon_id === 'new-session');
        assert.equal(event.event, 'beacon_change');
        assert.equal(device.inbox.items.filter((item) => item.type === 'goodbye').length, 0);
    });
    await t.test('MQTT disconnect closes the backend session', async () => {
        device.socket.destroy();
        await backendInbox.next((item) => item.closed === hello.session_id);
    });
});
