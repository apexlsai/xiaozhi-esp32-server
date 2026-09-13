const crypto = require('node:crypto');

class PresenceReporter {
    constructor({ env = process.env, fetchImpl = fetch, now = Date.now,
        intervalMs = 10000, debounceMs = 100, retryMs = 1000, timeoutMs = 3000,
        warn = console.warn } = {}) {
        this.url = env.KSZ_GUIDE_CONTROL_URL?.replace(/\/control$/, '/presence');
        this.token = env.KSZ_GUIDE_CONTROL_TOKEN;
        this.enabled = Boolean(this.url && this.token);
        this.fetch = fetchImpl;
        this.now = now;
        this.debounceMs = debounceMs;
        this.retryMs = retryMs;
        this.retryDelay = retryMs;
        this.timeoutMs = timeoutMs;
        this.warn = warn;
        this.lastWarning = -Infinity;
        this.instanceId = crypto.randomUUID();
        this.sequence = 0;
        this.active = new Map();
        this.pending = new Map();
        this.closed = false;
        if (this.enabled) {
            this.interval = setInterval(() => this.snapshot(), intervalMs);
            this.interval.unref();
        }
    }

    connect(deviceMac, connectionId) {
        if (!this.enabled || this.closed) return;
        const observed = this.now() / 1000;
        const report = { device_mac: deviceMac.toUpperCase(), connection_id: connectionId,
            online: true, connected_at: observed, observed_at: observed };
        this.active.set(connectionId, report);
        this.pending.set(connectionId, report);
        this.schedule(this.debounceMs);
    }

    disconnect(deviceMac, connectionId) {
        if (!this.enabled || this.closed) return;
        const mac = deviceMac.toUpperCase();
        const previous = this.active.get(connectionId);
        if (!previous || previous.device_mac !== mac) return;
        this.active.delete(connectionId);
        this.pending.set(connectionId, { ...previous, online: false, observed_at: this.now() / 1000 });
        this.schedule(this.debounceMs);
    }

    snapshot() {
        if (!this.enabled || this.closed) return;
        const observed = this.now() / 1000;
        for (const [connectionId, record] of this.active) {
            const report = { ...record, observed_at: observed };
            this.active.set(connectionId, report);
            this.pending.set(connectionId, report);
        }
        for (const [connectionId, record] of this.pending) {
            if (!record.online && observed - record.observed_at >= 60) this.pending.delete(connectionId);
        }
        this.schedule(this.debounceMs);
    }

    schedule(delay) {
        if (this.closed || this.timer || this.inFlight || !this.pending.size) return;
        this.timer = setTimeout(() => {
            this.timer = null;
            void this.flush();
        }, delay);
        this.timer.unref();
    }

    flush() {
        if (this.inFlight) return this.inFlight;
        if (this.closed || !this.pending.size) return Promise.resolve();
        clearTimeout(this.timer);
        this.timer = null;
        const reports = [...this.pending.values()].slice(0, 100);
        this.abort = new AbortController();
        const timeout = setTimeout(() => this.abort?.abort(), this.timeoutMs);
        timeout.unref();
        const signal = this.abort.signal;
        this.inFlight = Promise.resolve().then(async () => {
            let delay = 0;
            try {
                const response = await this.fetch(this.url, {
                    method: 'POST', signal,
                    headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
                    body: JSON.stringify({ instance_id: this.instanceId, sequence: ++this.sequence, reports }),
                });
                await response.body?.cancel();
                if (!response.ok) throw new Error('Presence report rejected');
                for (const report of reports) {
                    if (this.pending.get(report.connection_id) === report) this.pending.delete(report.connection_id);
                }
                this.retryDelay = this.retryMs;
            } catch {
                delay = this.retryDelay;
                this.retryDelay = Math.min(this.retryDelay * 2, 10000);
                if (!this.closed && this.now() - this.lastWarning >= 60000) {
                    this.lastWarning = this.now();
                    this.warn('设备在线状态同步失败，等待重试');
                }
            } finally {
                clearTimeout(timeout);
                this.abort = null;
                this.inFlight = null;
                this.schedule(delay);
            }
        });
        return this.inFlight;
    }

    close() {
        this.closed = true;
        clearInterval(this.interval);
        clearTimeout(this.timer);
        this.abort?.abort();
        this.active.clear();
        this.pending.clear();
    }
}

module.exports = { PresenceReporter };
