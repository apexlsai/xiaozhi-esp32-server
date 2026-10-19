const assert = require('node:assert/strict');
const crypto = require('node:crypto');

function verifyOta(response, deviceId, signatureKey) {
    assert.ok(!response.error, 'OTA returned a business error');
    const mqtt = response.mqtt;
    assert.ok(mqtt, 'OTA did not return MQTT configuration');
    const endpoint = new URL(`mqtt://${mqtt.endpoint}`);
    assert.ok(endpoint.hostname && endpoint.port && !endpoint.pathname
        && !endpoint.username && !endpoint.password && !endpoint.search && !endpoint.hash,
    'Invalid MQTT endpoint');
    const parts = mqtt.client_id?.split('@@@');
    assert.equal(parts?.length, 3, 'Expected signed three-part MQTT identity');
    assert.equal(parts[1], deviceId.replaceAll(':', '_'), 'OTA identity does not match Device-Id');
    assert.ok(parts[0] && parts[2], 'Incomplete MQTT identity');
    assert.ok(mqtt.username && mqtt.password, 'Missing MQTT credentials');
    assert.equal(typeof JSON.parse(Buffer.from(mqtt.username, 'base64').toString()), 'object');
    assert.equal(mqtt.publish_topic, 'device-server');
    assert.equal(mqtt.subscribe_topic, `devices/p2p/${parts[1]}`);
    assert.ok(response.websocket?.url, 'Missing WebSocket fallback URL');
    if (signatureKey) {
        const expected = crypto.createHmac('sha256', signatureKey)
            .update(`${mqtt.client_id}|${mqtt.username}`).digest('base64');
        assert.ok(mqtt.password === expected, 'OTA signature differs from MQTT_SIGNATURE_KEY');
    }
    return { endpoint: mqtt.endpoint, device_id: deviceId, signature_checked: Boolean(signatureKey) };
}

if (require.main === module) {
    (async () => {
        const [endpoint, deviceId] = process.argv.slice(2);
        assert.ok(endpoint && /^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$/i.test(deviceId || ''),
            'Usage: node verify-ota.cjs <OTA URL> <device MAC>');
        const response = await fetch(endpoint, {
            method: 'POST', redirect: 'error', signal: AbortSignal.timeout(10000),
            headers: { 'Content-Type': 'application/json', 'Device-Id': deviceId, 'Client-Id': deviceId },
            body: JSON.stringify({ application: { version: '1.0.0', elf_sha256: 'mqtt-check' }, board: { mac: deviceId } }),
        });
        assert.ok(response.ok, `OTA HTTP ${response.status}`);
        const result = verifyOta(await response.json(), deviceId, process.env.MQTT_SIGNATURE_KEY);
        console.log(JSON.stringify({ ok: true, ...result }));
    })().catch((error) => {
        console.error(`OTA check failed: ${error.message}`);
        process.exitCode = 1;
    });
}

module.exports = { verifyOta };
