import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { bodyHash, consumeMomentsMessage, handleMomentsRequest } from '../worker/moments.mjs';
import { ModelError } from '../worker/moments-model.mjs';

const origin = 'https://chenxin.life';
const migration = readFileSync(new URL('../migrations/0011_moments_workbench.sql', import.meta.url), 'utf8');
const profile = {
  author: { identity: '合成独立顾问，帮助个人创业者梳理服务流程。' },
  audience: { people: '合成目标读者：独立经营业务的顾问。' },
  preferences: { tone: '短句，保留未知，不编造客户与成效。' },
};
const variants = () => ({
  variants: [
    { title: '过程稿', angle: '过程', body: '合成过程：今天整理了3项交付前确认事项。', tags: ['实践'] },
    { title: '判断稿', angle: '判断', body: '合成判断：先说清范围，再开始做。', tags: ['交付'] },
    { title: '读者稿', angle: '读者场景', body: '合成读者场景：如果你也在接单，可以先确认范围。', tags: ['独立经营'] },
  ],
  note: '只验证流程，不是实际客户经历。',
});

// A real SQLite engine runs the actual migration and SQL. The shim translates
// only D1's result envelope; batch executes synchronously inside one transaction.
class Statement {
  constructor(db, sql, values = []) { this.db = db; this.sql = sql; this.values = values; }
  bind(...values) { return new Statement(this.db, this.sql, values); }
  async first(column) {
    const row = this.db.sqlite.prepare(this.sql).get(...this.values);
    if (!row) return null;
    return column ? row[column] : { ...row };
  }
  async all() {
    return { success: true, results: this.db.sqlite.prepare(this.sql).all(...this.values).map(row => ({ ...row })), meta: { changes: 0 } };
  }
  execute() {
    const value = this.db.sqlite.prepare(this.sql).run(...this.values);
    return { success: true, results: [], meta: { changes: Number(value.changes), last_row_id: Number(value.lastInsertRowid) } };
  }
  async run() { await this.db.beforeRun?.(this); return this.execute(); }
}
class SqliteD1 {
  constructor() {
    this.sqlite = new DatabaseSync(':memory:');
    this.sqlite.exec('PRAGMA foreign_keys=ON');
    this.sqlite.exec(migration);
  }
  prepare(sql) { return new Statement(this, sql); }
  async batch(statements) {
    await this.beforeBatch?.(statements);
    this.sqlite.exec('BEGIN IMMEDIATE');
    try {
      const results = statements.map(statement => statement.execute());
      this.sqlite.exec('COMMIT');
      return results;
    } catch (error) { this.sqlite.exec('ROLLBACK'); throw error; }
  }
}
function setup(t, extra = {}) {
  const db = new SqliteD1();
  t.after(() => db.sqlite.close());
  const queue = [];
  const env = {
    COMMENTS_DB: db,
    MOMENTS_SESSION_SECRET: 'synthetic-session-secret-never-expose',
    MOMENTS_ZHIPU_API_KEY: 'synthetic-zhipu-key-never-expose',
    SEMANTIC_QUEUE: { send: async message => { queue.push(structuredClone(message)); } },
    ...extra,
  };
  return { db, env, queue };
}
class Visitor {
  constructor(env, ip = '192.0.2.1') { this.env = env; this.ip = ip; this.cookie = ''; this.csrf = ''; }
  async call(path = '/state', payload, override = {}) {
    const method = override.method || (payload === undefined ? 'GET' : 'POST');
    const headers = new Headers({ 'CF-Connecting-IP': this.ip });
    if (this.cookie) headers.set('cookie', this.cookie);
    if (payload !== undefined) {
      headers.set('origin', origin);
      headers.set('content-type', 'application/json');
      headers.set('x-moments-csrf', this.csrf);
    }
    for (const [name, value] of Object.entries(override.headers || {})) {
      if (value === null) headers.delete(name); else headers.set(name, value);
    }
    const response = await handleMomentsRequest(new Request(`${origin}/moments/api${path}`, {
      method, headers, ...(payload === undefined ? {} : { body: typeof payload === 'string' ? payload : JSON.stringify(payload) }),
    }), this.env);
    assert.ok(response instanceof Response);
    const body = await response.json();
    const cookie = response.headers.get('set-cookie');
    if (cookie) this.cookie = cookie.split(';')[0];
    if (body.csrf_token) this.csrf = body.csrf_token;
    return { response, body, status: response.status };
  }
  async start() { const result = await this.call(); assert.equal(result.status, 200); return result; }
  async save(value = profile) { const result = await this.call('/profile', value); assert.equal(result.status, 200); return result; }
}
const queueMessage = body => ({ body, acknowledged: 0, ack() { this.acknowledged += 1; } });
async function consume(env, body, api = {}) {
  const message = queueMessage(body);
  const modelApi = { generateMoments: async () => variants(), reviseMoment: async () => ({ body: '合成修订正文。' }), ...api };
  assert.equal(await consumeMomentsMessage(message, env, { modelApi }), true);
  assert.equal(message.acknowledged, 1);
  return message;
}
async function generated(visitor, queue, api = {}) {
  const result = await visitor.call('/generate', { material: '合成素材：今天整理3项交付前事项，下周先试1次，暂时没有效果数据。' });
  assert.equal(result.status, 202);
  await consume(visitor.env, queue.at(-1), api);
  const current = await visitor.call();
  assert.equal(current.body.round.notes.length, 3);
  return { job: result.body.job_id, round: current.body.round, state: current.body };
}

test('moments visitor sessions use private signed cookies and expose no service key', async t => {
  const { env, db } = setup(t);
  const a = new Visitor(env); const b = new Visitor(env);
  const first = await a.start(); await b.start();
  assert.match(first.response.headers.get('set-cookie'), /Path=\/moments/);
  assert.match(first.response.headers.get('set-cookie'), /Secure; HttpOnly; SameSite=Strict/);
  assert.match(first.response.headers.get('cache-control'), /private, no-store/);
  assert.equal(first.response.headers.get('vary'), 'Cookie');
  assert.notEqual(a.cookie, b.cookie);
  assert.notEqual(a.csrf, b.csrf);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_workspaces').get().n, 0);
  assert.doesNotMatch(JSON.stringify(first.body), /synthetic-session-secret|synthetic-zhipu-key/);
  await a.save();
  assert.equal((await a.call()).body.profile_ready, true);
  assert.equal((await b.call()).body.profile_ready, false);
  assert.deepEqual((await b.call()).body.profiles.author.fields, {});
});

test('moments rejects missing or forged identity, foreign origin and CSRF', async t => {
  const { env, db } = setup(t);
  const a = new Visitor(env); await a.start();
  assert.equal((await new Visitor(env).call('/profile', profile)).status, 401);
  assert.equal((await a.call('/profile', profile, { headers: { origin: 'https://attacker.invalid' } })).status, 403);
  assert.equal((await a.call('/profile', profile, { headers: { 'x-moments-csrf': null } })).status, 403);
  assert.equal((await a.call('/profile', profile, { headers: { 'content-type': 'text/plain' } })).status, 415);
  const forged = a.cookie.replace(/\.[^.]+$/, '.' + '0'.repeat(64));
  assert.equal((await a.call('/profile', profile, { headers: { cookie: forged } })).status, 401);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_workspaces').get().n, 0);
});

test('moments requires the three profile fields while historical writing stays optional', async t => {
  const { env, queue } = setup(t);
  const a = new Visitor(env); await a.start();
  assert.equal((await a.call('/generate', { material: '今天整理事项。' })).body.code, 'PROFILE_REQUIRED');
  assert.equal((await a.call('/profile', { ...profile, preferences: { tone: '' } })).status, 400);
  await a.save();
  const { state } = await generated(a, queue, { generateMoments: async context => {
    assert.deepEqual(context.history, []);
    assert.deepEqual(context.profiles, profile);
    return variants();
  } });
  assert.equal(state.onboarding.history_count, 0);
  assert.equal(state.onboarding.history_required, false);
  assert.equal(state.onboarding.ready, true);
  assert.equal(state.materials.items.length, 0);
  assert.equal(state.quota.remaining, 4);
});

test('moments accepts a durable queue job before any model call and consumes only once', async t => {
  const { env, queue } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  const result = await a.call('/generate', { material: '合成受控素材：今天梳理交付边界。' });
  assert.equal(result.status, 202);
  assert.deepEqual(Object.keys(queue[0]).sort(), ['job_id', 'type']);
  assert.equal(queue[0].type, 'moments-writing-v1');
  const queued = (await a.call('/jobs/' + result.body.job_id)).body;
  assert.equal(queued.status, 'QUEUED'); assert.equal(queued.phase, 'queued');
  let calls = 0;
  const api = { generateMoments: async () => { calls += 1; return variants(); } };
  await consume(env, queue[0], api); await consume(env, queue[0], api);
  assert.equal(calls, 1);
  const done = (await a.call('/jobs/' + result.body.job_id)).body;
  assert.equal(done.status, 'DONE'); assert.equal(done.phase, 'done');
  assert.equal(done.provider, 'zhipu'); assert.equal(done.model, 'glm-5.3-flash');
  assert.ok(done.started_at && done.finished_at);
  assert.doesNotMatch(JSON.stringify(done), /payload_json|apiKey|synthetic-zhipu-key/);
});

test('moments reports real phases and prevents mutations or repeat model work while running', async t => {
  const { env, queue } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  const first = await generated(a, queue);
  const revision = await a.call('/revise', { round_id: first.round.round_id, variant: 2, preset: 'authentic' });
  assert.equal(revision.status, 202);
  let release; const hold = new Promise(resolve => { release = resolve; });
  let entered; const started = new Promise(resolve => { entered = resolve; });
  let calls = 0;
  const modelApi = { reviseMoment: async (_context, body, instruction, settings) => {
    calls += 1; assert.equal(body, variants().variants[1].body); assert.match(instruction, /真实/);
    await settings.progress('requesting'); entered(); await hold;
    await settings.progress('receiving'); await settings.progress('validating');
    return { body: '第二稿的合成修订正文。' };
  } };
  const running = consume(env, queue.at(-1), modelApi);
  await started;
  const status = (await a.call('/jobs/' + revision.body.job_id)).body;
  assert.equal(status.status, 'RUNNING'); assert.equal(status.phase, 'requesting'); assert.equal(status.target, 2);
  for (const [path, data] of [
    ['/profile', profile], ['/history', { filename: '新的历史.md', body: '合成历史原文。' }],
    ['/generate', { material: '不要重复调用模型。' }],
    ['/revise', { round_id: first.round.round_id, variant: 2, preset: 'colloquial' }],
    ['/final', { round_id: first.round.round_id, variant: 2, body: '不应写入的手工稿。' }],
    ['/keep', { round_id: first.round.round_id, variant: 2, keep: true }],
    ['/publish', { round_id: first.round.round_id, variant: 2, confirmed: true }],
  ]) assert.equal((await a.call(path, data)).status, 409, path);
  await consume(env, queue.at(-1), modelApi);
  assert.equal(calls, 1);
  release(); await running;
  const after = (await a.call()).body;
  assert.equal(after.active_job, null); assert.equal(after.quota.remaining, 3);
  assert.equal(after.round.notes.find(n => n.variant === 1).body, first.round.notes[0].body);
  assert.equal(after.round.notes.find(n => n.variant === 3).body, first.round.notes[2].body);
  assert.equal(after.round.notes.find(n => n.variant === 2).body, '第二稿的合成修订正文。');
});

test('moments scopes history, jobs and draft mutations to each visitor', async t => {
  const { env, queue } = setup(t);
  const a = new Visitor(env); const b = new Visitor(env, '192.0.2.2');
  await a.start(); await a.save(); await b.start(); await b.save({ ...profile, author: { identity: '第二位合成访客。' } });
  assert.equal((await a.call('/history', { filename: '私有历史.md', body: '只属于访客A的原文标记。' })).status, 200);
  const first = await generated(a, queue);
  const item = (await a.call()).body.materials.items[0];
  assert.equal((await b.call('/history/' + item.id)).status, 404);
  assert.equal((await b.call('/jobs/' + first.job)).status, 404);
  assert.equal((await b.call('/final', { round_id: first.round.round_id, variant: 2, body: '不允许跨访客覆盖。' })).status, 409);
  const stateB = (await b.call()).body;
  assert.equal(stateB.library.length, 0); assert.equal(stateB.materials.items.length, 0); assert.equal(stateB.round, null);
  assert.doesNotMatch(JSON.stringify(stateB), /只属于访客A|合成过程：/);
  assert.equal((await a.call('/history', { filename: '私有历史.md', body: '不能覆盖已有原文。' })).status, 409);
  assert.equal((await a.call('/history/' + item.id)).body.item.body, '只属于访客A的原文标记。');
});

test('moments never reports a successful history import when a job is accepted during hashing', async t => {
  const { env, queue, db } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  const marker = '合成历史导入与写作并发标记。';
  let entered; const hashing = new Promise(resolve => { entered = resolve; });
  let release; const hold = new Promise(resolve => { release = resolve; });
  const originalDigest = crypto.subtle.digest.bind(crypto.subtle);
  t.mock.method(crypto.subtle, 'digest', async (algorithm, input) => {
    if (new TextDecoder().decode(input) === marker) { entered(); await hold; }
    return originalDigest(algorithm, input);
  });
  const importing = a.call('/history', { filename: '并发导入.md', body: marker });
  await hashing;
  const job = await a.call('/generate', { material: '合成并发写作材料。' });
  assert.equal(job.status, 202);
  release();
  const imported = await importing;
  assert.equal(imported.status, 409);
  assert.equal(imported.body.code, 'BUSY');
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_history').get().n, 0);
  assert.equal(queue.length, 1);
});

test('moments visitor quota is consumed once for accepted writing and rejected attempts do not increment it', async t => {
  const { env, queue, db } = setup(t, { MOMENTS_VISITOR_DAILY_LIMIT: '1' });
  const a = new Visitor(env); await a.start(); await a.save(); await generated(a, queue);
  assert.equal((await a.call('/generate', { material: '第二次应受配额限制。' })).status, 429);
  assert.equal(queue.length, 1);
  assert.equal((await a.call()).body.quota.remaining, 0);
  assert.equal(db.sqlite.prepare("SELECT used FROM moments_usage WHERE bucket='global'").get().used, 1);
});

test('moments global quota atomically admits only one of concurrent visitors', async t => {
  const { env, queue, db } = setup(t, { MOMENTS_DAILY_LIMIT: '1' });
  const a = new Visitor(env); const b = new Visitor(env, '192.0.2.2');
  await a.start(); await a.save(); await b.start(); await b.save();
  const results = await Promise.all([a.call('/generate', { material: '并发访客A。' }), b.call('/generate', { material: '并发访客B。' })]);
  assert.deepEqual(results.map(r => r.status).sort(), [202, 429]);
  assert.equal(queue.length, 1);
  assert.equal(db.sqlite.prepare("SELECT used FROM moments_usage WHERE bucket='global'").get().used, 1);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_jobs').get().n, 1);
});

test('moments IP quota cannot be bypassed with a second browser identity', async t => {
  const { env, queue } = setup(t, { MOMENTS_IP_DAILY_LIMIT: '1' });
  const a = new Visitor(env); const b = new Visitor(env); const c = new Visitor(env, '192.0.2.3');
  for (const person of [a, b, c]) { await person.start(); await person.save(); }
  const results = await Promise.all([a.call('/generate', { material: '同IP第一位。' }), b.call('/generate', { material: '同IP第二位。' })]);
  assert.deepEqual(results.map(r => r.status).sort(), [202, 429]);
  assert.equal((await b.call()).body.quota.remaining, 0);
  assert.equal((await c.call('/generate', { material: '不同IP仍可使用。' })).status, 202);
  assert.equal(queue.length, 2);
});

test('moments rejects an exhausted visitor or IP without spending global quota', async t => {
  const { env, queue, db } = setup(t, { MOMENTS_VISITOR_DAILY_LIMIT: '1', MOMENTS_IP_DAILY_LIMIT: '2' });
  const a = new Visitor(env); const b = new Visitor(env); const c = new Visitor(env); const d = new Visitor(env, '192.0.2.30');
  for (const person of [a, b, c, d]) { await person.start(); await person.save(); }
  await generated(a, queue);
  const globalUsed = () => db.sqlite.prepare("SELECT used FROM moments_usage WHERE bucket='global'").get().used;
  assert.equal(globalUsed(), 1);
  assert.equal((await a.call('/generate', { material: '访客次数已满，不应扣全站次数。' })).status, 429);
  assert.equal(globalUsed(), 1);
  assert.equal((await b.call('/generate', { material: '同IP第二位正常受理。' })).status, 202);
  assert.equal(globalUsed(), 2);
  assert.equal((await c.call('/generate', { material: 'IP次数已满，不应扣全站次数。' })).status, 429);
  assert.equal(globalUsed(), 2);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_jobs').get().n, 2);
  assert.equal((await d.call('/generate', { material: '不同IP仍可使用剩余全站次数。' })).status, 202);
  assert.equal(globalUsed(), 3); assert.equal(queue.length, 3);
});

test('moments completion invalidated before commit cannot write drafts, retirements or revision evidence', async t => {
  for (const kind of ['generate', 'revise']) {
    const { env, queue, db } = setup(t);
    const a = new Visitor(env); await a.start(); await a.save();
    const first = await generated(a, queue);
    const before = db.sqlite.prepare('SELECT * FROM moments_drafts ORDER BY id').all().map(row => ({ ...row }));
    const request = kind === 'generate'
      ? await a.call('/generate', { material: '合成下一轮，提交保存前失效。' })
      : await a.call('/revise', { round_id: first.round.round_id, variant: 2, preset: 'authentic' });
    assert.equal(request.status, 202);
    let invalidated = false;
    db.beforeBatch = async statements => {
      if (!invalidated && statements.some(statement => statement.sql.includes("SET status='DONE'"))) {
        invalidated = true;
        db.sqlite.prepare('UPDATE moments_jobs SET started_at=? WHERE id=?').run('2000-01-01T00:00:00.000Z', request.body.job_id);
        const expired = await a.call('/jobs/' + request.body.job_id);
        assert.equal(expired.body.status, 'ERROR');
        assert.equal(expired.body.error.code, 'TASK_INTERRUPTED');
      }
    };
    let calls = 0;
    await consume(env, queue.at(-1), {
      generateMoments: async () => { calls += 1; return variants(); },
      reviseMoment: async () => { calls += 1; return { body: '不应写入的失效修订稿。' }; },
    });
    assert.equal(calls, 1, kind); assert.equal(invalidated, true, kind);
    const after = db.sqlite.prepare('SELECT * FROM moments_drafts ORDER BY id').all().map(row => ({ ...row }));
    assert.deepEqual(after, before, kind);
    assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_revisions').get().n, 0, kind);
    const state = (await a.call()).body;
    assert.equal(state.active_job, null, kind);
    assert.equal(state.round.round_id, first.round.round_id, kind);
    assert.equal(state.round.notes.length, 3, kind);
    assert.equal((await a.call('/jobs/' + request.body.job_id)).body.status, 'ERROR', kind);
  }
});

test('moments leaves unpublished finals and kept drafts out of reference material', async t => {
  const { env, queue } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  const first = await generated(a, queue);
  assert.equal((await a.call('/final', { round_id: first.round.round_id, variant: 2, body: 'UNPUBLISHED_FINAL_SENTINEL\n第二行。' })).status, 200);
  assert.equal((await a.call('/keep', { round_id: first.round.round_id, variant: 3, keep: true })).status, 200);
  assert.equal((await a.call()).body.materials.items.length, 0);
  const second = await generated(a, queue, { generateMoments: async context => {
    assert.deepEqual(context.history, []);
    assert.doesNotMatch(JSON.stringify(context), /UNPUBLISHED_FINAL_SENTINEL/);
    return variants();
  } });
  assert.notEqual(second.round.round_id, first.round.round_id);
  assert.deepEqual(second.round.notes.map(n => n.variant), [1, 2, 3]);
  const oldKept = second.state.library.find(n => n.round_id === first.round.round_id);
  assert.equal(oldKept.variant, 3); assert.equal(oldKept.kept, true);
});

test('moments only confirmed published final with matching body hash becomes reference material', async t => {
  const { env, queue, db } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  const first = await generated(a, queue); const round_id = first.round.round_id;
  await a.call('/final', { round_id, variant: 2, body: '最终确认的合成正文。\n保留第二行。' });
  await a.call('/keep', { round_id, variant: 3, keep: true });
  assert.equal((await a.call('/publish', { round_id, variant: 2, confirmed: false })).status, 400);
  assert.equal((await a.call()).body.materials.items.length, 0);
  assert.equal((await a.call('/publish', { round_id, variant: 2, confirmed: true })).status, 200);
  const published = (await a.call()).body;
  assert.deepEqual(published.round.notes.map(n => n.variant), [2, 3]);
  assert.equal(published.materials.items.length, 1);
  assert.equal(published.materials.items[0].kind, 'final');
  assert.equal(published.materials.items[0].status, '已发布');
  assert.equal(published.materials.items[0].reference_eligible, true);
  const draft = db.sqlite.prepare('SELECT * FROM moments_drafts WHERE round_id=? AND variant=2').get(round_id);
  assert.equal(draft.published_body_hash, await bodyHash(draft.body));
  assert.equal(db.sqlite.prepare('SELECT retired FROM moments_drafts WHERE round_id=? AND variant=1').get(round_id).retired, 1);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_revisions').get().n, 1);
  assert.equal((await a.call('/revise', { round_id, variant: 2, preset: 'professional' })).status, 409);
  assert.equal((await a.call('/final', { round_id, variant: 1, body: '已收尾稿不能恢复覆盖。' })).status, 404);
  db.sqlite.prepare('UPDATE moments_drafts SET body=? WHERE id=?').run('后来被外部改过的正文。', draft.id);
  assert.equal((await a.call()).body.materials.items.length, 0);
});

test('moments saves exact manual text and repeated identical final does not duplicate its revision evidence', async t => {
  const { env, queue, db } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  const { round } = await generated(a, queue);
  const body = '\n手工终稿第一行。\n\n第二行与末尾换行。\n';
  const payload = { round_id: round.round_id, variant: 2, body };
  assert.equal((await a.call('/final', payload)).status, 200);
  assert.equal((await a.call('/final', payload)).status, 200);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_revisions').get().n, 1);
  const state = (await a.call()).body;
  assert.equal(state.round.notes.find(n => n.variant === 2).body, body);
  assert.equal(state.materials.items.length, 0);
  const imported = '\n这是合成历史原文。\r\n末尾有换行。\n';
  assert.equal((await a.call('/history', { filename: '原字节历史.md', body: imported })).status, 200);
  assert.equal((await a.call()).body.materials.items[0].body, imported);
});

test('moments refuses a stale publish confirmation after a newer manual final wins', async t => {
  const { env, queue, db } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  const { round } = await generated(a, queue);
  const original = round.notes.find(n => n.variant === 2).body;
  let entered; const hashing = new Promise(resolve => { entered = resolve; });
  let release; const hold = new Promise(resolve => { release = resolve; });
  const digest = crypto.subtle.digest.bind(crypto.subtle);
  t.mock.method(crypto.subtle, 'digest', async (algorithm, input) => {
    if (new TextDecoder().decode(input) === original) { entered(); await hold; }
    return digest(algorithm, input);
  });
  const publishing = a.call('/publish', { round_id: round.round_id, variant: 2, confirmed: true });
  await hashing;
  const nextBody = '另一个页面刚保存的新终稿。';
  assert.equal((await a.call('/final', { round_id: round.round_id, variant: 2, body: nextBody })).status, 200);
  release();
  assert.equal((await publishing).status, 409);
  const state = (await a.call()).body;
  assert.equal(state.round.notes.length, 3);
  assert.equal(state.round.notes.find(n => n.variant === 2).body, nextBody);
  assert.equal(state.round.notes.find(n => n.variant === 2).status, '待发布');
  assert.equal(state.materials.items.length, 0);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_revisions').get().n, 1);
});

test('moments refuses a stale manual edit or keep after another request has published the draft', async t => {
  const { env, queue, db } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  const { round } = await generated(a, queue);
  let entered; const waiting = new Promise(resolve => { entered = resolve; });
  let release; const hold = new Promise(resolve => { release = resolve; });
  db.beforeBatch = async statements => {
    if (statements.some(s => s.sql.includes('moments_revisions') && s.values.includes('不应覆盖已发稿的旧编辑。'))) {
      entered(); await hold;
    }
  };
  const staleEdit = a.call('/final', { round_id: round.round_id, variant: 2, body: '不应覆盖已发稿的旧编辑。' });
  await waiting;
  assert.equal((await a.call('/publish', { round_id: round.round_id, variant: 2, confirmed: true })).status, 200);
  release();
  assert.equal((await staleEdit).status, 409);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_revisions').get().n, 0);
  assert.equal((await a.call()).body.materials.items[0].body, variants().variants[1].body);
  db.beforeBatch = undefined;
  const second = await generated(a, queue);
  let keepEntered; const keepWaiting = new Promise(resolve => { keepEntered = resolve; });
  let keepRelease; const keepHold = new Promise(resolve => { keepRelease = resolve; });
  db.beforeRun = async statement => {
    if (statement.sql.includes('SET kept=')) { keepEntered(); await keepHold; }
  };
  const staleKeep = a.call('/keep', { round_id: second.round.round_id, variant: 2, keep: true });
  await keepWaiting;
  assert.equal((await a.call('/publish', { round_id: second.round.round_id, variant: 2, confirmed: true })).status, 200);
  keepRelease();
  assert.equal((await staleKeep).status, 409);
  const published = (await a.call()).body.round.notes.find(n => n.variant === 2);
  assert.equal(published.status, '已发布'); assert.equal(published.kept, false);
});

test('moments selects one imported work and one confirmed final without crossing visitor boundaries', async t => {
  const { env, queue } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  for (let i = 0; i < 3; i++) assert.equal((await a.call('/history', { filename: `参考${i}.md`, body: `VISITOR_A_HISTORY_${i}` })).status, 200);
  const first = await generated(a, queue);
  assert.equal((await a.call('/final', { round_id: first.round.round_id, variant: 2, body: 'VISITOR_A_CONFIRMED_FINAL' })).status, 200);
  assert.equal((await a.call('/publish', { round_id: first.round.round_id, variant: 2, confirmed: true })).status, 200);
  await generated(a, queue, { generateMoments: async context => {
    assert.equal(context.history.length, 2);
    assert.deepEqual(context.history.map(row => row.kind).sort(), ['final', 'history']);
    assert.ok(context.history.some(row => row.body === 'VISITOR_A_CONFIRMED_FINAL'));
    return variants();
  } });
  const b = new Visitor(env, '192.0.2.10'); await b.start(); await b.save();
  await generated(b, queue, { generateMoments: async context => {
    assert.deepEqual(context.history, []);
    assert.doesNotMatch(JSON.stringify(context), /VISITOR_A_/);
    return variants();
  } });
});

test('moments bounds history and new workspace storage before accepting extra writes', async t => {
  const { env, db } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  for (let i = 0; i < 20; i++) assert.equal((await a.call('/history', { filename: `原文${i}.md`, body: '合成存储上限验证。' })).status, 200);
  assert.equal((await a.call('/history', { filename: '第21篇.md', body: '不应多写。' })).status, 429);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_history').get().n, 20);
  db.sqlite.prepare("UPDATE moments_usage SET used=20 WHERE bucket LIKE 'new-ip:%'").run();
  const sameIp = new Visitor(env); await sameIp.start();
  assert.equal((await sameIp.call('/profile', profile)).status, 429);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_workspaces').get().n, 1);
  const anotherIp = new Visitor(env, '192.0.2.9'); await anotherIp.start(); await anotherIp.save();
  db.sqlite.prepare("UPDATE moments_usage SET used=500 WHERE bucket='new-global'").run();
  const blocked = new Visitor(env, '192.0.2.11'); await blocked.start();
  assert.equal((await blocked.call('/profile', profile)).status, 429);
  assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM moments_workspaces').get().n, 2);
});

test('moments queue/model failures are redacted, release the lock and never retry model calls', async t => {
  const { env, queue } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  const request = await a.call('/generate', { material: '合成失败场景。' });
  let calls = 0;
  const api = { generateMoments: async () => { calls += 1; throw new Error('PRIVATE_ERROR synthetic-zhipu-key-never-expose'); } };
  await consume(env, queue[0], api); await consume(env, queue[0], api);
  assert.equal(calls, 1);
  const status = (await a.call('/jobs/' + request.body.job_id)).body;
  assert.equal(status.status, 'ERROR'); assert.equal(status.error.code, 'TASK_FAILED');
  assert.doesNotMatch(JSON.stringify(status), /PRIVATE_ERROR|synthetic-zhipu-key/);
  const state = (await a.call()).body;
  assert.equal(state.active_job, null); assert.equal(state.library.length, 0); assert.equal(state.quota.remaining, 4);
  const known = await a.call('/generate', { material: '合成受控模型错误。' });
  await consume(env, queue.at(-1), { generateMoments: async () => { throw new ModelError('MODEL_AUTH', '智谱认证暂不可用。'); } });
  const knownError = (await a.call('/jobs/' + known.body.job_id)).body;
  assert.equal(knownError.error.code, 'MODEL_AUTH');
  assert.doesNotMatch(JSON.stringify(knownError), /synthetic-zhipu-key/);
  env.SEMANTIC_QUEUE.send = async () => { throw new Error('PRIVATE_QUEUE_ERROR'); };
  const failedQueue = await a.call('/generate', { material: '合成入队失败场景。' });
  assert.equal(failedQueue.status, 503); assert.equal(failedQueue.body.code, 'QUEUE_UNAVAILABLE');
  assert.doesNotMatch(JSON.stringify(failedQueue.body), /PRIVATE_QUEUE_ERROR/);
  assert.equal((await a.call()).body.active_job, null);
});

test('moments stale/expired jobs and dead letters never trigger paid replay', async t => {
  const { env, queue, db } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  const request = await a.call('/generate', { material: '合成中断场景。' });
  db.sqlite.prepare('UPDATE moments_jobs SET created_at=? WHERE id=?').run('2000-01-01T00:00:00.000Z', request.body.job_id);
  const expired = (await a.call('/jobs/' + request.body.job_id)).body;
  assert.equal(expired.status, 'ERROR'); assert.equal(expired.error.code, 'TASK_INTERRUPTED');
  let calls = 0;
  await consume(env, queue[0], { generateMoments: async () => { calls += 1; return variants(); } });
  assert.equal(calls, 0);
  const second = await a.call('/generate', { material: '合成DLQ场景。' });
  const dead = queueMessage(queue.at(-1));
  await consumeMomentsMessage(dead, env, { deadLetter: true, modelApi: { generateMoments: async () => { calls += 1; return variants(); } } });
  assert.equal(calls, 0); assert.equal(dead.acknowledged, 1);
  assert.equal((await a.call('/jobs/' + second.body.job_id)).body.error.code, 'QUEUE_INTERRUPTED');
});

test('moments rejects unsupported routes, fields, credentials, oversized input and unknown presets', async t => {
  const { env, queue } = setup(t);
  const a = new Visitor(env); await a.start(); await a.save();
  assert.equal(await handleMomentsRequest(new Request(origin + '/api/other'), env), null);
  assert.equal((await a.call('/unknown')).status, 404);
  assert.equal((await a.call('/unknown', {})).status, 404);
  assert.equal((await a.call('/state', undefined, { method: 'PUT' })).status, 405);
  assert.equal((await a.call('/model', { provider: 'codex' })).status, 400);
  assert.equal((await a.call('/model', { provider: 'zhipu', api_key: 'SHOULD_NOT_BE_ACCEPTED' })).status, 400);
  assert.equal((await a.call('/model', { provider: 'zhipu' })).status, 200);
  assert.equal((await a.call('/generate', { material: '材料', owner_id: 'not-allowed' })).status, 400);
  assert.equal((await a.call('/generate', { material: 'https://example.com' })).body.code, 'SOURCE_TEXT_REQUIRED');
  assert.equal((await a.call('/generate', { material: 'API key: synthetic-credential-value-0123456789' })).body.code, 'CREDENTIAL_LIKE_SOURCE');
  assert.equal((await a.call('/profile', '{broken JSON')).status, 400);
  assert.equal((await a.call('/profile', 'x'.repeat(128 * 1024 + 1))).status, 413);
  const first = await generated(a, queue);
  assert.equal((await a.call('/revise', { round_id: first.round.round_id, variant: 2, preset: 'run-arbitrary-command' })).status, 400);
  assert.equal((await a.call('/keep', { round_id: first.round.round_id, variant: 2, keep: 'yes' })).status, 400);
  assert.equal((await a.call('/history', { filename: '../escape.md', body: '合成原文。' })).status, 400);
  assert.equal(queue.length, 1);
  assert.equal((await a.call()).body.quota.remaining, 4);
});
