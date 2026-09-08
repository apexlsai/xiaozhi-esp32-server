export const VISION_FAILURE = '视觉服务暂时不可用，请稍后再试。';

export function createVisionRequestId(cryptoApi = globalThis.crypto) {
    if (cryptoApi.randomUUID) return cryptoApi.randomUUID();
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const PUBLIC_FAILURES = new Set([
    VISION_FAILURE,
    '识别中断了，请再拍一次。',
    '摄像头尚未准备好，请稍后再试。',
    '视觉分析服务尚未配置。'
]);

export function visionFailure(requestId, text = VISION_FAILURE) {
    return {
        success: false,
        action: 'RESPONSE',
        response: text,
        message: text,
        ...(requestId ? { request_id: requestId } : {})
    };
}

function cancelledResult(requestId) {
    return { ...visionFailure(requestId, ''), delivery: 'cancelled' };
}

export function normalizeVisionResult(result, requestId) {
    if (!result || typeof result !== 'object' || Array.isArray(result)) {
        return visionFailure(requestId);
    }
    if (result.request_id && result.request_id !== requestId) {
        return visionFailure(requestId);
    }
    if (result.delivery === 'cancelled') {
        return cancelledResult(requestId);
    }
    const text = typeof result.response === 'string' ? result.response.trim() : '';
    const streamed = result.delivery === 'streamed';
    const success = result.success === true && (text.length > 0 || streamed);
    const response = success ? text : (PUBLIC_FAILURES.has(text) ? text : VISION_FAILURE);
    return {
        success,
        action: 'RESPONSE',
        response,
        message: success ? '' : response,
        request_id: requestId,
        ...(streamed ? { delivery: 'streamed' } : {})
    };
}

export class VisionRequests {
    constructor(fetchRequest = (...args) => fetch(...args), timeoutMs = 125000) {
        this.fetchRequest = fetchRequest;
        this.timeoutMs = timeoutMs;
        this.pending = new Set();
        this.current = null;
    }

    cancel() {
        for (const request of this.pending) {
            request.cancelled = true;
            request.controller.abort();
        }
        this.current = null;
    }

    async upload({ url, token, deviceId, clientId, question, image, requestId, deliveryMode }) {
        if (this.current) this.current.cancelled = true;
        const request = { controller: new AbortController(), cancelled: false };
        this.current = request;
        this.pending.add(request);
        const timeout = setTimeout(() => request.controller.abort(), this.timeoutMs);
        try {
            const formData = new FormData();
            formData.append('question', question);
            formData.append('image', image, 'photo.jpg');
            formData.append('request_id', requestId);
            formData.append('delivery_mode', deliveryMode);
            const response = await this.fetchRequest(url, {
                method: 'POST',
                body: formData,
                signal: request.controller.signal,
                headers: {
                    'Device-Id': deviceId,
                    'Client-Id': clientId,
                    'Authorization': `Bearer ${token}`
                }
            });
            if (!response.ok) return request.cancelled ? cancelledResult(requestId) : visionFailure(requestId);
            const result = await response.json();
            return request.cancelled ? cancelledResult(requestId) : normalizeVisionResult(result, requestId);
        } catch {
            return request.cancelled ? cancelledResult(requestId) : visionFailure(requestId);
        } finally {
            clearTimeout(timeout);
            this.pending.delete(request);
            if (this.current === request) this.current = null;
        }
    }
}
