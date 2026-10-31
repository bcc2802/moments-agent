import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import worker from '../worker/index.mjs';
import { assertDeployable } from '../scripts/deploy.mjs';

const origin = 'https://moments.example';
test('standalone entry redirects root and bare mount, preserving query strings', async () => {
  for (const path of ['/', '/moments']) {
    const response = await worker.fetch(new Request(origin + path + '?from=bookmark'), {});
    assert.equal(response.status, 308);
    assert.equal(response.headers.get('location'), origin + '/moments/?from=bookmark');
  }
});

test('standalone entry restricts static routes and adds the browser security policy', async () => {
  let seen;
  const env = { ASSETS: { fetch: async request => { seen = request.url; return new Response('asset', { headers: { 'content-type': 'text/css' } }); } } };
  const response = await worker.fetch(new Request(origin + '/moments/style.css'), env);
  assert.equal(seen, origin + '/moments/style.css');
  assert.equal(await response.text(), 'asset');
  assert.equal(response.headers.get('content-type'), 'text/css');
  assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
  assert.equal(response.headers.get('x-frame-options'), 'DENY');
  assert.match(response.headers.get('content-security-policy'), /connect-src 'self'/);
  assert.match(response.headers.get('content-security-policy'), /frame-ancestors 'none'/);
  assert.equal((await worker.fetch(new Request(origin + '/_internal/private'), env)).status, 404);
  assert.equal((await worker.fetch(new Request(origin + '/moments/style.css', { method: 'POST' }), env)).status, 405);
});

test('standalone API failures stay JSON and never fall through to static assets', async () => {
  let assets = 0;
  const response = await worker.fetch(new Request(origin + '/moments/api/state'), { ASSETS: { fetch: async () => { assets += 1; return new Response('wrong'); } } });
  assert.equal(response.status, 503);
  assert.equal((await response.json()).code, 'SERVICE_UNAVAILABLE');
  assert.equal(response.headers.get('cache-control'), 'private, no-store');
  assert.equal(assets, 0);
});

test('standalone queue ignores other workloads without invoking a database or model', async () => {
  let acknowledged = 0;
  await worker.queue({ queue: 'moments-writing', messages: [{ body: { type: 'unrelated' }, ack: () => { acknowledged += 1; } }] }, {});
  assert.equal(acknowledged, 1);
});

test('standalone queue acknowledges a previously completed writing task', async () => {
  let acknowledged = 0;
  const env = { COMMENTS_DB: { prepare: () => ({ bind: () => ({ first: async () => ({ status: 'DONE' }) }) }) } };
  await worker.queue({ queue: 'moments-writing', messages: [{ body: { type: 'moments-writing-v1', job_id: 'a'.repeat(32) }, ack: () => { acknowledged += 1; } }] }, env);
  assert.equal(acknowledged, 1);
});

test('standalone DLQ records an interrupted job without invoking the model', async () => {
  let acknowledged = 0; let writes;
  const env = { COMMENTS_DB: {
    prepare: sql => ({ bind: (...values) => ({ sql, values, first: async () => ({ status: 'QUEUED', owner_id: 'synthetic-owner' }) }) }),
    batch: async statements => { writes = statements; return []; },
  } };
  await worker.queue({ queue: 'moments-writing-dlq', messages: [{ body: { type: 'moments-writing-v1', job_id: 'b'.repeat(32) }, ack: () => { acknowledged += 1; } }] }, env);
  assert.equal(acknowledged, 1);
  assert.equal(writes.length, 2);
  assert.equal(writes[0].values[0], 'QUEUE_INTERRUPTED');
});

test('standalone queue fails safely when its required database binding is absent', async () => {
  await assert.rejects(worker.queue({ queue: 'moments-writing', messages: [{ body: { type: 'moments-writing-v1', job_id: 'c'.repeat(32) } }] }, {}), /database binding is unavailable/);
});

test('public template isolates database, queue and assets and blocks placeholder deployment', () => {
  const config = JSON.parse(readFileSync(new URL('../wrangler.jsonc', import.meta.url), 'utf8'));
  assert.equal(config.assets.directory, './public');
  assert.equal(config.assets.run_worker_first, true);
  assert.equal(config.d1_databases.length, 1);
  assert.equal(config.d1_databases[0].binding, 'COMMENTS_DB');
  assert.match(config.d1_databases[0].database_id, /^[0-9a-f-]{36}$/i);
  assert.equal(config.queues.producers[0].binding, 'SEMANTIC_QUEUE');
  assert.equal(config.queues.consumers.length, 2);
  assert.equal(config.queues.consumers[0].dead_letter_queue, config.queues.consumers[1].queue);
  assert.ok(config.queues.consumers[1].queue.endsWith('-dlq'));
  assert.equal(config.routes, undefined);
  assert.equal(config.ai, undefined);
  assert.equal(config.triggers, undefined);
  const placeholder = structuredClone(config);
  placeholder.d1_databases[0].database_id = '00000000-0000-0000-0000-000000000000';
  assert.throws(() => assertDeployable(placeholder), /replace the placeholder/);
  placeholder.d1_databases[0].database_id = 'not-a-uuid';
  assert.throws(() => assertDeployable(placeholder), /replace the placeholder/);
  placeholder.d1_databases[0].database_id = '12345678-1234-1234-1234-123456789abc';
  assert.doesNotThrow(() => assertDeployable(placeholder));
});
