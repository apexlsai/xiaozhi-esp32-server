const net = require('node:net');
const http = require('node:http');

const mqtt = new Promise((resolve, reject) => {
    const socket = net.connect(Number(process.env.MQTT_PORT), '127.0.0.1');
    socket.setTimeout(3000);
    socket.on('connect', () => { socket.end(); resolve(); });
    socket.on('timeout', () => { socket.destroy(); reject(new Error('MQTT timeout')); });
    socket.on('error', reject);
});
const api = new Promise((resolve, reject) => {
    const request = http.request({ method: 'POST', host: '127.0.0.1', port: Number(process.env.API_PORT), path: '/api/devices/status', timeout: 3000 }, (response) => {
        response.resume();
        if (response.statusCode === 401) resolve();
        else reject(new Error('Unexpected API authentication response'));
    });
    request.on('timeout', () => request.destroy(new Error('API timeout')));
    request.on('error', reject);
    request.end();
});
Promise.all([mqtt, api]).then(() => process.exit(0), () => process.exit(1));
