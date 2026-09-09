const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

function gatewayConfig(env) {
    const required = (name) => {
        const value = env[name];
        if (!value || value.trim() !== value || value === 'null') {
            throw new Error(`${name} must be configured without surrounding whitespace`);
        }
        return value;
    };
    const host = required('PUBLIC_IP');
    if (net.isIP(host) !== 4 && !/^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(host)) {
        throw new Error('PUBLIC_IP must be an IPv4 address or hostname without scheme or port');
    }
    const secret = required('MQTT_SIGNATURE_KEY');
    if (secret.length < 8 || !/[A-Z]/.test(secret) || !/[a-z]/.test(secret)
        || /test|1234|admin|password|qwerty|xiaozhi/i.test(secret)) {
        throw new Error('MQTT_SIGNATURE_KEY does not meet the gateway password requirements');
    }
    required('SERVER_SECRET');
    for (const name of ['MQTT_PORT', 'UDP_PORT', 'API_PORT']) {
        const port = required(name);
        if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
            throw new Error(`${name} must be a port between 1 and 65535`);
        }
    }
    if (Number(env.MQTT_PORT) === Number(env.API_PORT)) {
        throw new Error('MQTT_PORT and API_PORT must differ');
    }
    const endpoint = required('MQTT_CHAT_SERVER');
    const url = new URL(endpoint);
    if (!['ws:', 'wss:'].includes(url.protocol) || url.username || url.password
        || url.pathname !== '/xiaozhi/v1/' || url.search !== '?from=mqtt_gateway' || url.hash) {
        throw new Error('MQTT_CHAT_SERVER must end with /xiaozhi/v1/?from=mqtt_gateway');
    }
    return { production: { chat_servers: [endpoint] }, debug: false };
}

if (require.main === module) {
    try {
        const config = gatewayConfig(process.env);
        fs.mkdirSync(path.join(__dirname, 'config'), { recursive: true });
        fs.writeFileSync(path.join(__dirname, 'config/mqtt.json'), JSON.stringify(config));
        require('./app.js');
    } catch (error) {
        console.error(`MQTT startup failed: ${error.code || error.message}`);
        process.exit(1);
    }
}

module.exports = { gatewayConfig };
