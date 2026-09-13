const assert = require('node:assert/strict');
const { test } = require('node:test');
const { PresenceReporter } = require('../presence.cjs');
const { GuideControl } = require('../guide-control.cjs');

const env = { KSZ_GUIDE_CONTROL_URL: 'http://localhost/internal/ksz/guide/control',
    KSZ_GUIDE_CONTROL_TOKEN: 'presence-integration-token-with-32-characters' };
const mac = 'aa:bb:cc:dd:ee:ff';

function setup(t, options = {}) {
    const requests = [];
    const reporter = new PresenceReporter({ env, intervalMs: 3600000, debounceMs: 3600000,
        warn: () => {}, fetchImpl: async (url, init) => {
            assert.equal(url, 'http://localhost/internal/ksz/guide/presence');
            assert.equal(init.headers.Authorization, `Bearer ${env.KSZ_GUIDE_CONTROL_TOKEN}`);
            requests.push(JSON.parse(init.body));
            return new Response(null, { status: 204 });
        }, ...options });
    t.after(() => reporter.close());
    return { reporter, requests };
}

test('authenticated idle connection reports presence without firmware or voice messages', async (t) => {
    const { reporter, requests } = setup(t);
    const control = new GuideControl({ macAddress: mac,
        sendMqttMessage: () => assert.fail('Presence must not contact firmware') }, { presence: reporter });
    await reporter.flush();
    assert.equal(requests[0].reports[0].device_mac, mac.toUpperCase());
    assert.equal(requests[0].reports[0].connection_id, control.connectionId);
    assert.equal(requests[0].reports[0].online, true);
    control.close();
    control.close();
    await reporter.flush();
    assert.equal(requests[1].reports[0].online, false);
    assert.equal(requests.length, 2);
    assert.equal(reporter.active.size, 0);
    assert.equal(reporter.pending.size, 0);
});

test('snapshots recover server state and preserve connection start time', async (t) => {
    let now = 100000;
    const { reporter, requests } = setup(t, { now: () => now });
    reporter.connect(mac, 'connection-1');
    await reporter.flush();
    now += 10000;
    reporter.snapshot();
    await reporter.flush();
    assert.equal(requests[1].instance_id, requests[0].instance_id);
    assert.ok(requests[1].sequence > requests[0].sequence);
    assert.equal(requests[1].reports[0].connected_at, 100);
    assert.equal(requests[1].reports[0].observed_at, 110);
    assert.equal(reporter.interval.hasRef(), false);
});

test('bulk delivery is serialized and never exceeds one hundred reports', async (t) => {
    const { reporter, requests } = setup(t);
    for (let i = 0; i < 201; i++) reporter.connect(mac, `connection-${i}`);
    const first = reporter.flush();
    assert.equal(reporter.flush(), first);
    await first;
    await reporter.flush();
    await reporter.flush();
    assert.deepEqual(requests.map((request) => request.reports.length), [100, 100, 1]);
    assert.deepEqual(requests.map((request) => request.sequence), [1, 2, 3]);
    assert.equal(reporter.pending.size, 0);
});

test('failed in-flight online is superseded by offline and retried without resurrection', async (t) => {
    let release;
    const blocked = new Promise((resolve) => { release = resolve; });
    const requests = [];
    const { reporter } = setup(t, { fetchImpl: async (_, init) => {
        requests.push(JSON.parse(init.body));
        if (requests.length === 1) {
            await blocked;
            return new Response(null, { status: 503 });
        }
        return new Response(null, { status: 204 });
    } });
    reporter.connect(mac, 'old');
    const first = reporter.flush();
    await Promise.resolve();
    reporter.disconnect(mac, 'old');
    release();
    await first;
    assert.equal(reporter.pending.get('old').online, false);
    await reporter.flush();
    assert.equal(requests[0].reports[0].online, true);
    assert.equal(requests[1].reports[0].online, false);
    assert.equal(reporter.pending.size, 0);
});

test('old connection offline and replacement connection online are both preserved', async (t) => {
    const { reporter, requests } = setup(t);
    reporter.connect(mac, 'old');
    await reporter.flush();
    reporter.connect(mac, 'new');
    reporter.disconnect(mac, 'old');
    await reporter.flush();
    const states = new Map(requests[1].reports.map((report) => [report.connection_id, report.online]));
    assert.equal(states.get('old'), false);
    assert.equal(states.get('new'), true);
    reporter.snapshot();
    await reporter.flush();
    assert.deepEqual(requests[2].reports.map((report) => report.connection_id), ['new']);
});

test('offline failures remain pending until success or expiry beyond the server lease', async (t) => {
    let now = 100000;
    const { reporter } = setup(t, { now: () => now, fetchImpl: () => { throw new Error('Offline'); } });
    reporter.connect(mac, 'connection-1');
    reporter.disconnect(mac, 'connection-1');
    await reporter.flush();
    now += 59000;
    reporter.snapshot();
    assert.equal(reporter.pending.get('connection-1').online, false);
    await reporter.flush();
    assert.equal(reporter.inFlight, null);
    now += 1000;
    reporter.snapshot();
    assert.equal(reporter.pending.size, 0);
});

test('network timeout aborts delivery while retaining retry state', async (t) => {
    let aborted = false;
    const { reporter } = setup(t, { timeoutMs: 10, fetchImpl: (_, init) => new Promise((_, reject) => {
        init.signal.addEventListener('abort', () => { aborted = true; reject(new Error('Aborted')); });
    }) });
    reporter.connect(mac, 'connection-1');
    await Promise.all([reporter.flush(), new Promise((resolve) => setTimeout(resolve, 20))]);
    assert.equal(aborted, true);
    assert.equal(reporter.pending.size, 1);
    assert.equal(reporter.inFlight, null);
    assert.equal(reporter.timer.hasRef(), false);
});

test('missing configuration leaves presence disabled', async (t) => {
    const { reporter, requests } = setup(t, { env: {} });
    reporter.connect(mac, 'connection-1');
    reporter.snapshot();
    await reporter.flush();
    assert.equal(reporter.interval, undefined);
    assert.equal(reporter.pending.size, 0);
    assert.equal(requests.length, 0);
});
