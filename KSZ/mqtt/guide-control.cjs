const crypto = require('node:crypto');

const actions = new Set(['sync', 'policy_ack', 'observation', 'lost', 'heartbeat', 'ready']);

class GuideControl {
    constructor(connection, { env = process.env, fetchImpl = fetch } = {}) {
        this.connection = connection;
        this.url = env.KSZ_GUIDE_CONTROL_URL;
        this.token = env.KSZ_GUIDE_CONTROL_TOKEN;
        this.fetch = fetchImpl;
        this.connectionId = crypto.randomUUID();
        this.pending = 0;
        this.queue = Promise.resolve();
        this.activation = null;
        this.handshake = null;
        this.closed = false;
    }

    send(message) {
        if (!this.closed) this.connection.sendMqttMessage(JSON.stringify(message));
    }

    reject(message, reason) {
        this.send({ type: 'guide_control', version: 1, action: 'result',
            request_id: message.request_id, accepted: false, reason });
    }

    handle(message) {
        if (!this.url || !this.token) {
            this.reject(message, 'guide_unavailable');
            return Promise.resolve();
        }
        if (message.version !== 1 || !actions.has(message.action)
            || typeof message.request_id !== 'string' || message.request_id.length > 128
            || !message.request_id || Buffer.byteLength(JSON.stringify(message)) > 65536) {
            this.reject(message, 'invalid_control_message');
            return Promise.resolve();
        }
        if (this.pending >= 8) {
            this.reject(message, 'rate_limited');
            return Promise.resolve();
        }
        this.pending++;
        const task = this.queue.then(() => this.forward(message)).catch(() => {
            this.reject(message, 'guide_unavailable');
        }).finally(() => { this.pending--; });
        this.queue = task;
        return task;
    }

    async forward(message) {
        if (this.closed) return;
        const conn = this.connection;
        const response = await this.fetch(this.url, {
            method: 'POST',
            headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
            signal: AbortSignal.timeout(3000),
            body: JSON.stringify({ device_mac: conn.macAddress.toUpperCase(),
                connection_id: this.connectionId, transport: 'mqtt',
                udp_ready: Boolean(conn.bridge && conn.udp?.remoteAddress),
                message: { ...message, session_id: conn.bridge ? conn.udp?.session_id : null } }),
        });
        if (!response.ok) throw new Error('Guide control rejected');
        const reader = response.body.getReader();
        const chunks = [];
        let size = 0;
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                size += value.length;
                if (size > 2097152) throw new Error('Guide response too large');
                chunks.push(Buffer.from(value));
            }
        } finally {
            await reader.cancel();
        }
        const data = JSON.parse(Buffer.concat(chunks).toString());
        if (!Array.isArray(data.messages) || data.messages.length > 256) {
            throw new Error('Invalid guide response');
        }
        for (const command of data.messages) {
            if (command.type !== 'guide_control' || command.version !== 1) continue;
            if (command.action === 'request_hello') {
                this.activation = { ...command, issuedAt: Date.now() };
            }
            this.send(command);
        }
    }

    async hello(json, start) {
        if (!json.guide) return start(json);
        const guide = json.guide;
        if (typeof guide.boot_id !== 'string' || !guide.boot_id || guide.boot_id.length > 128
            || !Number.isSafeInteger(guide.session_attempt) || guide.session_attempt < 1) {
            this.reject(json, 'invalid_session_metadata');
            return;
        }
        const key = `${guide.boot_id}:${guide.activation_id || ''}:${guide.session_attempt}`;
        if (this.handshake?.key === key) {
            const reply = await this.handshake.promise;
            if (reply && this.connection.bridge && this.connection.udp?.session_id === reply.session_id) {
                this.send(reply);
            }
            return;
        }
        if (guide.activation_id && (!this.activation || guide.activation_id !== this.activation.activation_id
            || guide.session_attempt !== this.activation.session_attempt
            || Date.now() - this.activation.issuedAt > 15000)) {
            this.reject(json, 'invalid_activation');
            return;
        }
        const message = { ...json, guide: { ...guide, connection_id: this.connectionId } };
        const handshake = { key };
        this.handshake = handshake;
        handshake.promise = start(message);
        await handshake.promise;
    }

    close() {
        this.closed = true;
        this.activation = null;
        this.handshake = null;
    }
}

module.exports = { GuideControl };
