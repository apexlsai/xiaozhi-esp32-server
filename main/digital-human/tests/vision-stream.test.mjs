import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../js/core/network/vision.js', import.meta.url), 'utf8');
const { VisionRequests, VISION_FAILURE, normalizeVisionResult, createVisionRequestId } = await import(
    `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
);

function uploadOptions(requestId = 'request-1', deliveryMode = 'mcp') {
    return {
        url: 'https://vision.example.test/mcp/vision/explain',
        token: 'test-token',
        deviceId: 'test-device',
        clientId: 'test-client',
        question: '这是什么？',
        image: new Blob(['test-image'], { type: 'image/jpeg' }),
        requestId,
        deliveryMode
    };
}

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
}

function response(payload) {
    return { ok: true, json: async () => payload };
}

test('streaming upload carries explicit mode and request ID and keeps a final receipt', async () => {
    const requests = new VisionRequests(async (url, options) => {
        assert.equal(url, uploadOptions().url);
        assert.equal(options.body.get('request_id'), 'request-1');
        assert.equal(options.body.get('delivery_mode'), 'mcp');
        assert.equal(options.body.get('question'), '这是什么？');
        assert.equal(options.body.get('image').type, 'image/jpeg');
        assert.equal(options.headers['Device-Id'], 'test-device');
        assert.equal(options.headers.Authorization, 'Bearer test-token');
        return response({ success: true, response: '这是一只花瓶。', delivery: 'streamed', request_id: 'request-1' });
    });
    const result = await requests.upload(uploadOptions());
    assert.equal(result.delivery, 'streamed');
    assert.equal(result.success, true);
    assert.equal(result.response, '这是一只花瓶。');
    assert.equal('photo_data' in result, false);
});

test('empty streaming receipt remains successful while empty buffered response fails', () => {
    assert.equal(normalizeVisionResult({ success: true, delivery: 'streamed' }, 'one').success, true);
    assert.equal(normalizeVisionResult({ success: true }, 'one').success, false);
});

test('failed streamed result preserves delivery and strips raw provider data', () => {
    const result = normalizeVisionResult({
        success: false,
        delivery: 'streamed',
        response: 'VLLM 请求失败: {"error":"internal-provider-error"}',
        debug: 'secret',
        vision_analysis: { error: 'secret' }
    }, 'one');
    assert.equal(result.delivery, 'streamed');
    assert.equal(result.response, VISION_FAILURE);
    assert.equal(JSON.stringify(result).includes('secret'), false);
    assert.equal(JSON.stringify(result).includes('provider-error'), false);
    assert.equal(normalizeVisionResult({ success: false, response: '识别中断了，请再拍一次。' }, 'one').response,
        '识别中断了，请再拍一次。');
});

test('cancelled and mismatched receipts cannot be interpreted as a valid answer', () => {
    const cancelled = normalizeVisionResult({ success: true, response: '旧回答', delivery: 'cancelled' }, 'old');
    assert.equal(cancelled.delivery, 'cancelled');
    assert.equal(cancelled.response, '');
    assert.equal(normalizeVisionResult({ success: true, response: '旧回答', request_id: 'old' }, 'new').success, false);
});

test('new photo reaches server before old receipt settles, and stale result stays cancelled', async () => {
    const first = deferred();
    const second = deferred();
    let oldSignal;
    const sent = [];
    const requests = new VisionRequests(async (url, options) => {
        const id = options.body.get('request_id');
        sent.push(id);
        if (id === 'old') {
            oldSignal = options.signal;
            return first.promise;
        }
        assert.equal(oldSignal.aborted, false);
        assert.equal(options.body.get('delivery_mode'), 'push');
        return second.promise;
    });
    const older = requests.upload(uploadOptions('old'));
    const newer = requests.upload(uploadOptions('new', 'push'));
    assert.deepEqual(sent, ['old', 'new']);
    first.resolve(response({ success: true, response: '旧回答', delivery: 'streamed' }));
    const oldResult = await older;
    assert.equal(oldResult.request_id, 'old');
    assert.equal(oldResult.delivery, 'cancelled');
    assert.equal(oldResult.response, '');
    assert.notEqual(requests.current, null);
    second.resolve(response({ success: true, response: '新回答', delivery: 'streamed' }));
    const newResult = await newer;
    assert.equal(newResult.success, true);
    assert.equal(newResult.response, '新回答');
});

test('disconnect cancels pending requests without converting abort into a spoken failure', async () => {
    const requests = new VisionRequests(async (url, { signal }) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(new Error('abort: internal details')));
    }));
    const pending = requests.upload(uploadOptions());
    requests.cancel();
    const result = await pending;
    assert.equal(result.delivery, 'cancelled');
    assert.equal(result.response, '');
    assert.equal(requests.pending.size, 0);
});

test('network and malformed JSON failures return only public errors', async () => {
    for (const fetchRequest of [
        async () => { throw new Error('secret-token'); },
        async () => ({ ok: false, status: 500 }),
        async () => ({ ok: true, json: async () => { throw new Error('secret-content'); } })
    ]) {
        const result = await new VisionRequests(fetchRequest).upload(uploadOptions());
        assert.equal(result.success, false);
        assert.equal(result.response, VISION_FAILURE);
        assert.equal(JSON.stringify(result).includes('secret'), false);
    }
});

test('manual UUID uses browser randomUUID with fallback for older browser contexts', () => {
    assert.equal(createVisionRequestId({ randomUUID: () => 'native-uuid' }), 'native-uuid');
    const fallback = createVisionRequestId({ getRandomValues: bytes => bytes.fill(1) });
    assert.match(fallback, /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/);
});

async function loadBrowserModule(path, globals, expose = '') {
    const contents = await readFile(new URL(path, import.meta.url), 'utf8');
    const script = contents.replace(/^import .*;\n/gm, '').replace(/^export default .*;\n/gm, '').replace(/^export /gm, '');
    const context = vm.createContext({ log() {}, setTimeout, clearTimeout, ...globals });
    vm.runInContext(`${script}\n${expose}`, context);
    return context;
}

test('hello advertises streaming; MCP metadata and original RPC IDs survive concurrent results', async () => {
    const calls = [];
    const sent = [];
    const first = deferred();
    const second = deferred();
    const context = await loadBrowserModule('../js/core/network/websocket.js', {
        window: {},
        WebSocket: { OPEN: 1 },
        getConfig: () => ({}),
        executeMcpTool: (name, args, metadata) => {
            calls.push({ name, args, metadata });
            return metadata.vision_request_id === 'old' ? first.promise : second.promise;
        }
    }, 'globalThis.Handler = WebSocketHandler;');
    const handler = new context.Handler();
    const originalSocket = {
        readyState: 1,
        send: text => sent.push(JSON.parse(text)),
        addEventListener: (event, callback) => queueMicrotask(() => callback({ data: '{"type":"hello","session_id":"session-1"}' })),
        removeEventListener() {}
    };
    handler.websocket = originalSocket;
    assert.equal(await handler.sendHelloMessage(), true);
    assert.equal(sent[0].features.vision_stream, true);
    for (const [id, requestId] of [[1, 'old'], [2, 'new']]) {
        handler.handleMCPMessage({ session_id: 'session-1', payload: {
            method: 'tools/call', id,
            params: { name: 'self.camera.take_photo', arguments: { question: '看一下' }, _meta: { vision_request_id: requestId } }
        } });
    }
    assert.equal(calls[0].metadata.vision_request_id, 'old');
    handler.websocket = { readyState: 1, send: () => assert.fail('old receipt sent to a new websocket') };
    second.resolve({ success: true, response: '新回答', delivery: 'streamed', request_id: 'new' });
    first.resolve({ success: false, response: '', delivery: 'cancelled', request_id: 'old' });
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(sent.slice(1).map(item => item.payload.id), [2, 1]);
    assert.equal(sent[2].payload.result.isError, false);
    assert.equal(JSON.parse(sent[1].payload.result.content[0].text).delivery, 'streamed');
});

test('camera tools forward server context and retain buffered compatibility when context is absent', async () => {
    const captures = [];
    const context = await loadBrowserModule('../js/core/mcp/tools.js', {
        window: { takePhoto: async (question, options) => { captures.push({ question, options }); return { success: true }; } }
    }, "mcpTools = [{name: 'self_camera_take_photo'}]; globalThis.executeTool = executeMcpTool;");
    await context.executeTool('self.camera.take_photo', { question: '新图' }, { vision_request_id: 'mcp-request' });
    await context.executeTool('self_camera_take_photo', { question: '旧端' });
    assert.equal(captures[0].options.requestId, 'mcp-request');
    assert.equal(captures[0].options.deliveryMode, 'mcp');
    assert.equal(captures[1].options.deliveryMode, 'return');
});

test('actual camera entrypoints upload separate IDs without image data in MCP receipt', async () => {
    const elements = new Map();
    const uploaded = [];
    const frame = {
        readyState: 4, HAVE_ENOUGH_DATA: 4,
        videoWidth: 1280, videoHeight: 720
    };
    const createElement = () => ({
        style: {},
        events: {},
        addEventListener(name, callback) { this.events[name] = callback; },
        appendChild(child) { elements.set(child.id, child); },
        getContext: () => ({ drawImage() {} }),
        toDataURL: () => 'data:image/jpeg;base64,aW1hZ2U='
    });
    elements.set('cameraContainer', createElement());
    elements.set('cameraSwitchMask', createElement());
    elements.set('cameraVideo', frame);
    let nextId = 0;
    let configuration = '{"url":"https://vision.example.test/","token":"secret"}';
    const context = await loadBrowserModule('../js/app.js', {
        window: {},
        document: {
            getElementById: id => elements.get(id),
            addEventListener() {},
            createElement
        },
        localStorage: { getItem: () => configuration },
        atob,
        Blob,
        VisionRequests: class {
            async upload(options) {
                uploaded.push(options);
                return { success: true, response: '画面正文', delivery: 'streamed', request_id: options.requestId };
            }
            cancel() {}
        },
        createVisionRequestId: () => `manual-${++nextId}`,
        visionFailure: (requestId, response = VISION_FAILURE) => ({ success: false, response, request_id: requestId })
    });
    await context.window.chatApp.initCamera();
    const mcpResult = await context.window.takePhoto('相机问题', { requestId: 'server-request', deliveryMode: 'mcp' });
    assert.equal(mcpResult.request_id, 'server-request');
    assert.equal(mcpResult.delivery, 'streamed');
    assert.equal(mcpResult.photo_width, 1280);
    assert.equal('photo_data' in mcpResult, false);
    assert.equal(uploaded[0].deliveryMode, 'mcp');
    await elements.get('takePhotoBtn').events.click();
    await elements.get('takePhotoBtn').events.click();
    assert.deepEqual(uploaded.slice(1).map(options => options.requestId), ['manual-1', 'manual-2']);
    assert.equal(uploaded[1].deliveryMode, 'push');
    configuration = null;
    assert.equal((await context.window.takePhoto()).response, '视觉分析服务尚未配置。');
    frame.readyState = 0;
    assert.equal((await context.window.takePhoto()).response, '摄像头尚未准备好，请稍后再试。');
});
