import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ModelError, MOMENTS_ENDPOINT, MOMENTS_MODEL,
  rewritePresetOptions, compileRevisionInstruction, generateMoments, reviseMoment,
} from '../worker/moments-model.mjs';

const KEY = 'synthetic-key-only-for-tests.0123456789';
const encoder = new TextEncoder();
const variants = () => ({ variants: [1, 2, 3].map((n) => ({ title: `合成标题${n}`, angle: `合成切口${n}`, body: '合成事实：准备下周先试1次，目前没有结果。', tags: ['实践'] })), note: '三个合成切口。' });
const frame = (data) => `data: ${JSON.stringify(data)}\n\n`;
const fixture = (result = { body: '合成修订正文' }, { finish = 'stop', done = true, reasoning = 'PRIVATE_REASONING' } = {}) =>
  frame({ choices: [{ index: 0, delta: { reasoning_content: reasoning } }] })
  + frame({ choices: [{ index: 0, delta: { content: JSON.stringify(result) }, finish_reason: finish }] })
  + (done ? 'data: [DONE]\n\n' : '');
const response = (data, size = 47) => {
  const bytes = typeof data === 'string' ? encoder.encode(data) : data;
  let offset = 0;
  return new Response(new ReadableStream({
    pull(controller) {
      if (offset === bytes.length) { controller.close(); return; }
      controller.enqueue(bytes.slice(offset, offset + size)); offset += Math.min(size, bytes.length - offset);
    },
  }), { headers: { 'content-type': 'text/event-stream; charset=utf-8' } });
};
const modelError = (code) => (error) => error instanceof ModelError && error.code === code && !String(error).includes(KEY);

test('server adapter uses official endpoint, scoped key header, JSON and no tools or retries', async () => {
  let calls = 0; let captured; const events = []; let acknowledged = false;
  const result = await reviseMoment({}, '合成旧稿', '删去套话', {
    apiKey: KEY,
    progress: async (phase, message) => {
      await Promise.resolve();
      acknowledged = true; events.push({ phase, message });
    },
    fetcher: async (url, options) => {
      calls += 1; captured = { url, options };
      assert.equal(acknowledged, true);
      return response(fixture());
    },
  });
  assert.equal(calls, 1);
  assert.equal(captured.url, MOMENTS_ENDPOINT);
  assert.equal(captured.options.method, 'POST');
  assert.equal(captured.options.redirect, 'manual');
  assert.equal(captured.options.headers.authorization, `Bearer ${KEY}`);
  assert.ok(captured.options.signal instanceof AbortSignal);
  assert.ok(!captured.options.body.includes(KEY));
  const body = JSON.parse(captured.options.body);
  assert.equal(body.model, MOMENTS_MODEL);
  assert.equal(body.stream, true);
  assert.deepEqual(body.thinking, { type: 'enabled' });
  assert.equal(body.reasoning_effort, 'low');
  assert.deepEqual(body.response_format, { type: 'json_object' });
  assert.equal(body.max_tokens, 8192);
  assert.equal(Object.hasOwn(body, 'tools'), false);
  assert.equal(Object.hasOwn(body, 'tool_choice'), false);
  assert.equal(result.body, '合成修订正文');
  assert.deepEqual(events.map((event) => event.phase), ['requesting', 'receiving', 'validating']);
  assert.ok(!JSON.stringify(events).includes('PRIVATE_REASONING'));
});

test('generation preserves strict three-variant schema and direction prompt', async () => {
  let sent;
  const output = await generateMoments({ demo: true }, '准备下周先试1次，目前没有结果。', {
    apiKey: KEY, fetcher: async (_, options) => { sent = JSON.parse(options.body); return response(fixture(variants())); },
  });
  assert.equal(output.variants.length, 3);
  for (const text of ['第一稿讲过程', '第二稿说判断', '第三稿代入读者场景', '历史作品只供写法参考', '未知保持未知']) {
    assert.ok(sent.messages[0].content.includes(text));
  }
});

test('preset IDs are allowlisted and fixed facts stay above additional requests', () => {
  assert.deepEqual(rewritePresetOptions().map((item) => item.id), ['colloquial', 'authentic', 'professional']);
  assert.ok(rewritePresetOptions().every((item) => Object.keys(item).sort().join() === 'description,id,label'));
  const value = compileRevisionInstruction('authentic', '保留最后的问题');
  assert.ok(value.includes('不新增客户、收入、成效'));
  assert.ok(value.includes('不要为了生动补造'));
  assert.ok(value.endsWith('保留最后的问题'));
  assert.equal(compileRevisionInstruction(undefined, '  保留分行\n'), '  保留分行\n');
  for (const id of ['__proto__', 'unknown', {}, [], 1, 'authentic;execute']) {
    assert.throws(() => compileRevisionInstruction(id, '意见'), modelError('INVALID_REWRITE_PRESET'));
  }
  for (const [preset, custom] of [[undefined, ''], ['professional', '长'.repeat(3900)], [undefined, '长'.repeat(4001)]]) {
    assert.throws(() => compileRevisionInstruction(preset, custom), modelError('INVALID_MODEL_INPUT'));
  }
});

test('invalid or excessive inputs and accidental key context fail before fetch', async () => {
  let calls = 0;
  const options = { apiKey: KEY, fetcher: async () => { calls += 1; throw new Error(KEY); } };
  await assert.rejects(generateMoments({}, '', options), modelError('INVALID_MODEL_INPUT'));
  await assert.rejects(generateMoments([], '材料', options), modelError('INVALID_MODEL_INPUT'));
  await assert.rejects(generateMoments({ body: '长'.repeat(50000) }, '材料', options), modelError('CONTEXT_TOO_LARGE'));
  await assert.rejects(generateMoments({ accidental: KEY }, '材料', options), modelError('INVALID_MODEL_INPUT'));
  await assert.rejects(reviseMoment({}, '原稿', '修改', { ...options, apiKey: '' }), modelError('MODEL_KEY_MISSING'));
  assert.equal(calls, 0);
});

test('vendor failures are redacted without reattempt or alternate provider', async () => {
  for (const [status, code] of [[401, 'MODEL_AUTH'], [403, 'MODEL_ACCESS'], [429, 'MODEL_LIMIT'], [500, 'MODEL_UNAVAILABLE'], [302, 'MODEL_UNAVAILABLE']]) {
    let calls = 0;
    await assert.rejects(generateMoments({}, '合成素材', { apiKey: KEY, fetcher: async () => {
      calls += 1; return new Response(`${KEY} private vendor diagnostics`, { status });
    } }), modelError(code));
    assert.equal(calls, 1);
  }
  let calls = 0;
  await assert.rejects(generateMoments({}, '材料', { apiKey: KEY, fetcher: async () => {
    calls += 1; throw new Error(`network ${KEY}`);
  } }), modelError('MODEL_UNAVAILABLE'));
  assert.equal(calls, 1);
});

test('truncated, unfinished and malformed streams never become a manuscript', async () => {
  for (const stream of [fixture(undefined, { done: false }), fixture(undefined, { finish: 'length' }), fixture(undefined, { finish: null }),
    'data: not-json\n\ndata: [DONE]\n\n', frame({ choices: 'invalid' }) + 'data: [DONE]\n\n']) {
    let calls = 0;
    await assert.rejects(reviseMoment({}, '合成旧稿', '删套话', { apiKey: KEY, fetcher: async () => {
      calls += 1; return response(stream);
    } }), modelError('MODEL_FORMAT'));
    assert.equal(calls, 1);
  }
});

test('tool output, secret echo, wrong shape and invalid UTF-8 are rejected', async () => {
  const tool = frame({ choices: [{ index: 0, delta: { tool_calls: [{ id: 'no-tools' }] } }] }) + 'data: [DONE]\n\n';
  const streams = [tool, fixture({ body: KEY }), fixture({ body: '正文', extra: true }), fixture({ body: ' ' }),
    new Uint8Array([100, 97, 116, 97, 58, 32, 0xff, 10, 10])];
  for (const stream of streams) {
    await assert.rejects(reviseMoment({}, '合成旧稿', '删套话', { apiKey: KEY, fetcher: async () => response(stream) }), modelError('MODEL_FORMAT'));
  }
  const bad = [
    { variants: [], note: '' },
    { ...variants(), extra: true },
    { ...variants(), variants: variants().variants.slice(0, 2) },
    { ...variants(), variants: [{ ...variants().variants[0], title: '长'.repeat(101) }, ...variants().variants.slice(1)] },
    { ...variants(), variants: [{ ...variants().variants[0], tags: [] }, ...variants().variants.slice(1)] },
  ];
  for (const result of bad) {
    await assert.rejects(generateMoments({}, '合成素材', { apiKey: KEY, fetcher: async () => response(fixture(result)) }), modelError('MODEL_FORMAT'));
  }
});

test('timeouts cover a fetch and stream that ignore AbortSignal', async () => {
  let calls = 0;
  await assert.rejects(reviseMoment({}, '旧稿', '修改', { apiKey: KEY, timeoutMs: 10, fetcher: async () => {
    calls += 1; return new Promise(() => {});
  } }), modelError('MODEL_TIMEOUT'));
  assert.equal(calls, 1);
  await assert.rejects(reviseMoment({}, '旧稿', '修改', { apiKey: KEY, timeoutMs: 10, fetcher: async () =>
    new Response(new ReadableStream({ start() {} }), { headers: { 'content-type': 'text/event-stream' } }) }), modelError('MODEL_TIMEOUT'));
});

test('SSE handles fragmented Unicode, CRLF, comments and usage frames', async () => {
  const data = ': keepalive\r\n\r\n' + fixture({ body: '中文合成正文😀' }).replaceAll('\n', '\r\n')
    .replace('data: [DONE]', `data: ${JSON.stringify({ choices: [], usage: { total_tokens: 10 } })}\r\n\r\ndata: [DONE]`);
  const result = await reviseMoment({}, '旧稿', '修改', { apiKey: KEY, fetcher: async () => response(data, 1) });
  assert.equal(result.body, '中文合成正文😀');
});

test('progress failure stops the request without leaking callback text', async () => {
  let calls = 0;
  await assert.rejects(reviseMoment({}, '旧稿', '修改', { apiKey: KEY,
    progress: async () => { throw new Error(KEY); },
    fetcher: async () => { calls += 1; return response(fixture()); },
  }), modelError('MODEL_PROGRESS'));
  assert.equal(calls, 0);
});

test('oversized stream and unexpected non-stream transport are rejected', async () => {
  await assert.rejects(reviseMoment({}, '旧稿', '修改', { apiKey: KEY,
    fetcher: async () => response(new Uint8Array(1024 * 1024 + 1), 1024 * 1024 + 1),
  }), modelError('MODEL_FORMAT'));
  await assert.rejects(reviseMoment({}, '旧稿', '修改', { apiKey: KEY,
    fetcher: async () => new Response(JSON.stringify({ body: '无流' }), { headers: { 'content-type': 'application/json' } }),
  }), modelError('MODEL_FORMAT'));
});

test('deadline includes a blocked progress write and cancellation cannot stall completion', { timeout: 500 }, async () => {
  let calls = 0;
  await assert.rejects(reviseMoment({}, '旧稿', '修改', { apiKey: KEY, timeoutMs: 10,
    progress: async () => new Promise(() => {}),
    fetcher: async () => { calls += 1; return response(fixture()); },
  }), modelError('MODEL_TIMEOUT'));
  assert.equal(calls, 0);
  const stream = new ReadableStream({
    start(controller) { controller.enqueue(encoder.encode(fixture())); },
    cancel() { return new Promise(() => {}); },
  });
  const result = await reviseMoment({}, '旧稿', '修改', { apiKey: KEY,
    fetcher: async () => new Response(stream, { headers: { 'content-type': 'text/event-stream' } }),
  });
  assert.equal(result.body, '合成修订正文');
});
