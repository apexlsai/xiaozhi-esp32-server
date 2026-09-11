const assert = require('node:assert/strict');
const { test } = require('node:test');
const { GuideControl } = require('../guide-control.cjs');

const env = { KSZ_GUIDE_CONTROL_URL: 'http://localhost/internal/ksz/guide/control',
    KSZ_GUIDE_CONTROL_TOKEN: 'local-integration-token-with-32-characters' };
const message = { type: 'guide_control', version: 1, action: 'sync', request_id: 'sync-1' };

function setup(fetchImpl) {
    const sent = [];
    const connection = { macAddress: 'aa:bb:cc:dd:ee:ff', bridge: null, udp: {},
        sendMqttMessage: (json) => sent.push(JSON.parse(json)) };
    return { sent, connection, control: new GuideControl(connection, { env, fetchImpl }) };
}

test('idle guide control authenticates device identity without a voice bridge', async () => {
    let body;
    const { control, sent } = setup(async (url, options) => {
        assert.equal(url, env.KSZ_GUIDE_CONTROL_URL);
        assert.equal(options.headers.Authorization, `Bearer ${env.KSZ_GUIDE_CONTROL_TOKEN}`);
        body = JSON.parse(options.body);
        return Response.json({ messages: [{ type: 'guide_control', version: 1, action: 'policy' }] });
    });
    await control.handle({ ...message, device_mac: 'attacker', session_id: 'forged' });
    assert.equal(body.device_mac, 'AA:BB:CC:DD:EE:FF');
    assert.equal(body.message.session_id, null);
    assert.equal(body.udp_ready, false);
    assert.equal(body.connection_id, control.connectionId);
    assert.equal(sent[0].action, 'policy');
});

test('ready carries the real UDP session and readiness', async () => {
    let body;
    const { control, connection } = setup(async (_, options) => {
        body = JSON.parse(options.body);
        return Response.json({ messages: [] });
    });
    connection.bridge = {};
    connection.udp = { session_id: 'actual-session', remoteAddress: { address: '127.0.0.1' } };
    await control.handle({ ...message, action: 'ready', session_id: 'forged' });
    assert.equal(body.message.session_id, 'actual-session');
    assert.equal(body.udp_ready, true);
});

test('duplicate activation hellos reuse the same voice handshake and UDP session', async () => {
    const command = { type: 'guide_control', version: 1, action: 'request_hello',
        activation_id: 'move-1', session_attempt: 1 };
    const { control, connection, sent } = setup(async () => Response.json({ messages: [command] }));
    await control.handle(message);
    let starts = 0;
    let release;
    const pending = new Promise((resolve) => { release = resolve; });
    const hello = { type: 'hello', guide: { activation_id: 'move-1', session_attempt: 1, boot_id: 'boot-1',
        connection_id: 'forged' } };
    const start = async (request) => {
        starts++;
        assert.equal(request.guide.connection_id, control.connectionId);
        await pending;
        connection.bridge = {};
        connection.udp = { session_id: 'session-1' };
        return { type: 'hello', session_id: 'session-1' };
    };
    const first = control.hello(hello, start);
    const duplicate = control.hello(hello, start);
    release();
    await Promise.all([first, duplicate]);
    assert.equal(starts, 1);
    assert.equal(sent.at(-1).session_id, 'session-1');
    await control.hello({ ...hello, guide: { activation_id: 'forged', session_attempt: 1, boot_id: 'boot-1' } }, start);
    assert.equal(starts, 1);
    assert.equal(sent.at(-1).reason, 'invalid_activation');
});

test('interactive v1 hello passes session metadata without an activation', async () => {
    const { control, sent } = setup();
    let request;
    await control.hello({ type: 'hello', guide: { boot_id: 'boot-1', session_attempt: 2 } }, async (value) => { request = value; });
    assert.equal(request.guide.connection_id, control.connectionId);
    assert.equal(request.guide.activation_id, undefined);
    await control.hello({ type: 'hello', guide: { boot_id: '', session_attempt: 3 } }, async () => { assert.fail('invalid hello started'); });
    assert.equal(sent.at(-1).reason, 'invalid_session_metadata');
});

test('invalid controls never reach the backend and unavailable controls do not create voice sessions', async () => {
    let requests = 0;
    const { control, sent } = setup(async () => { requests++; throw new Error('Offline'); });
    await control.handle({ ...message, action: 'arbitrary' });
    assert.equal(requests, 0);
    assert.equal(sent.at(-1).reason, 'invalid_control_message');
    await control.handle(message);
    assert.equal(requests, 1);
    assert.equal(sent.at(-1).reason, 'guide_unavailable');
    control.close();
    await control.handle(message);
    assert.equal(requests, 1);
});

test('queued control requests are bounded', async () => {
    let release;
    const pending = new Promise((resolve) => { release = resolve; });
    const { control, sent } = setup(async () => {
        await pending;
        return Response.json({ messages: [] });
    });
    const requests = Array.from({ length: 9 }, (_, i) => control.handle({ ...message, request_id: String(i) }));
    assert.equal(sent.at(-1).reason, 'rate_limited');
    release();
    await Promise.all(requests);
    assert.equal(control.pending, 0);
});
