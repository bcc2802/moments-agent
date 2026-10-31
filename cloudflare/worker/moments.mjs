import { ModelError, compileRevisionInstruction, generateMoments, reviseMoment, rewritePresetOptions } from './moments-model.mjs';

const COOKIE = '__Secure-moments_session';
const MAX_BODY = 128 * 1024;
const QUEUE_TYPE = 'moments-writing-v1';
const PHASES = { queued: '任务已接收，等待执行。', preparing: '正在整理本次材料。', requesting: '请求已交给智谱，正在等待响应。',
  receiving: '已收到模型响应，正在接收文案。', validating: '正在检查稿件是否完整。', saving: '正在保存文案并回读。', done: '稿件已保存并回读。' };
const encoder = new TextEncoder();
const model = { provider: 'zhipu', model: 'glm-5.3-flash', label: '智谱 GLM-5.3-Flash', transport: 'zhipu-api' };
const options = [{ id: 'zhipu', label: model.label, model: model.model }];

class RequestError extends Error {
  constructor(code, message, status = 400) { super(message); this.code = code; this.status = status; }
}
const fail = (code, message, status) => { throw new RequestError(code, message, status); };
const now = () => new Date().toISOString();
const day = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
const parse = (value, fallback = {}) => { try { return JSON.parse(value); } catch { return fallback; } };
const hex = bytes => [...new Uint8Array(bytes)].map(x => x.toString(16).padStart(2, '0')).join('');
export const bodyHash = async value => hex(await crypto.subtle.digest('SHA-256', encoder.encode(value)));
async function signature(secret, value) {
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return hex(await crypto.subtle.sign('HMAC', key, encoder.encode(value)));
}
function equal(a, b) { if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false; let x = 0; for (let i = 0; i < a.length; i++) x |= a.charCodeAt(i) ^ b.charCodeAt(i); return x === 0; }
function json(data, status = 200, cookie = null) {
  const headers = { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'private, no-store', 'x-content-type-options': 'nosniff',
    'x-robots-tag': 'noindex, nofollow', 'referrer-policy': 'same-origin', vary: 'Cookie' };
  if (cookie) headers['set-cookie'] = cookie;
  return new Response(JSON.stringify(data), { status, headers });
}
function limits(env) {
  const number = (value, fallback, max) => Number.isInteger(Number(value)) && Number(value) >= 1 && Number(value) <= max ? Number(value) : fallback;
  return { visitor: number(env.MOMENTS_VISITOR_DAILY_LIMIT, 5, 100), ip: number(env.MOMENTS_IP_DAILY_LIMIT, 20, 1000), global: number(env.MOMENTS_DAILY_LIMIT, 100, 10000) };
}
async function identity(request, env) {
  if (!env.MOMENTS_SESSION_SECRET || !env.COMMENTS_DB) fail('SERVICE_UNAVAILABLE', '写作服务正在准备，请稍后再试。', 503);
  const cookie = (request.headers.get('cookie') || '').split(';').map(x => x.trim()).find(x => x.startsWith(COOKIE + '='))?.slice(COOKIE.length + 1) || '';
  const [expires, nonce, mac] = cookie.split('.');
  let id = null;
  if (/^\d{10}$/.test(expires || '') && /^[a-f0-9-]{36}$/.test(nonce || '') && Number(expires) > Date.now() / 1000
      && equal(mac, await signature(env.MOMENTS_SESSION_SECRET, expires + '.' + nonce))) id = nonce;
  const fresh = !id;
  if (fresh && request.method !== 'GET') fail('SESSION_REQUIRED', '访问身份已过期，请刷新页面再继续。', 401);
  if (!id) id = crypto.randomUUID();
  const payload = `${Math.floor(Date.now() / 1000) + 30 * 86400}.${id}`;
  return { id, fresh, csrf: await signature(env.MOMENTS_SESSION_SECRET, 'csrf:' + id),
    cookie: `${COOKIE}=${payload}.${await signature(env.MOMENTS_SESSION_SECRET, payload)}; Path=/moments; Max-Age=2592000; Secure; HttpOnly; SameSite=Strict` };
}
function validateOrigin(request, person) {
  if (request.headers.get('origin') !== new URL(request.url).origin || !equal(request.headers.get('x-moments-csrf'), person.csrf)) {
    fail('INVALID_ORIGIN', '操作来源无法确认，请从本站工作台重新打开。', 403);
  }
  if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) fail('INVALID_INPUT', '请使用JSON提交。', 415);
}
export function validateText(value, label, max = 2000, empty = false) {
  if (typeof value !== 'string' || value.length > max || value.includes('\0') || (!empty && !value.trim())) fail('INVALID_INPUT', `${label}为空或超出长度限制。`);
  if (/-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,})\b|(?:api[_ -]?key|access[_ -]?token|密钥)\s*[:=：]\s*[A-Za-z0-9._-]{16,}/i.test(value)) {
    fail('CREDENTIAL_LIKE_SOURCE', '内容疑似包含凭证，请移除后再提交。');
  }
  return value.trim().replace(/\r\n?/g, '\n');
}
function fields(data, allowed, required = allowed) {
  if (!data || typeof data !== 'object' || Array.isArray(data) || Object.keys(data).some(k => !allowed.includes(k)) || required.some(k => !(k in data))) fail('INVALID_INPUT', '请求字段不完整或含不支持字段。');
}
async function input(request) {
  if (Number(request.headers.get('content-length') || 0) > MAX_BODY) fail('INPUT_TOO_LARGE', '本次提交过长。', 413);
  const reader = request.body?.getReader(); const parts = []; let length = 0;
  if (reader) {
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      length += value.byteLength;
      if (length > MAX_BODY) { void reader.cancel().catch(() => {}); fail('INPUT_TOO_LARGE', '本次提交过长。', 413); }
      parts.push(value);
    }
  }
  const bytes = new Uint8Array(length); let offset = 0;
  for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
  let raw; try { raw = new TextDecoder('utf-8', { fatal: true }).decode(bytes); } catch { fail('INVALID_INPUT', '提交内容不是有效UTF-8。'); }
  try { const value = JSON.parse(raw); if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(); return value; }
  catch { fail('INVALID_INPUT', '提交内容不是有效JSON。'); }
}
const stmt = (db, sql, ...values) => db.prepare(sql).bind(...values);
const rows = async statement => (await statement.all()).results || [];
const workspace = (db, id) => stmt(db, 'SELECT * FROM moments_workspaces WHERE id=?', id).first();
const profileReady = p => ['identity', 'people', 'tone'].every((key, i) => typeof p[['author', 'audience', 'preferences'][i]]?.[key] === 'string' && p[['author', 'audience', 'preferences'][i]][key].trim());
async function ipBucket(request, env) { return 'ip:' + await signature(env.MOMENTS_SESSION_SECRET, 'ip:' + (request.headers.get('CF-Connecting-IP') || 'unknown')); }
async function ensureWorkspace(db, id, ip) {
  if (await workspace(db, id)) return;
  const date = day(); const time = now(); const buckets = ['new-global', 'new-' + ip];
  await db.batch(buckets.map(b => stmt(db, 'INSERT OR IGNORE INTO moments_usage(day,bucket,used) VALUES(?,?,0)', date, b)));
  // The birth request is a unique guard for both rate counters in one transaction.
  const birth = crypto.randomUUID();
  await db.batch([
    stmt(db, `INSERT OR IGNORE INTO moments_workspaces(id,profiles_json,created_at,updated_at)
      SELECT ?,?,?,? WHERE (SELECT used FROM moments_usage WHERE day=? AND bucket=?)<500
      AND (SELECT used FROM moments_usage WHERE day=? AND bucket=?)<20`, id, JSON.stringify({ birth }), time, time, date, buckets[0], date, buckets[1]),
    ...buckets.map(b => stmt(db, `UPDATE moments_usage SET used=used+1 WHERE day=? AND bucket=?
      AND EXISTS(SELECT 1 FROM moments_workspaces WHERE id=? AND json_extract(profiles_json,'$.birth')=?)`, date, b, id, birth)),
  ]);
  if (!await workspace(db, id)) fail('STORAGE_LIMIT', '今天的新工作区已达上限，请明天再来。', 429);
}
async function expireJob(db, id) {
  const w = await workspace(db, id);
  if (!w?.active_job_id) return;
  const job = await stmt(db, 'SELECT * FROM moments_jobs WHERE id=? AND owner_id=?', w.active_job_id, id).first();
  const cutoff = new Date(Date.now() - 5 * 60000).toISOString();
  if (!job || ['DONE', 'ERROR'].includes(job.status)) {
    await stmt(db, 'UPDATE moments_workspaces SET active_job_id=NULL WHERE id=? AND active_job_id=?', id, w.active_job_id).run();
  } else if ((job.started_at || job.created_at) < cutoff) {
    await jobFailed(db, job.id, id, 'TASK_INTERRUPTED', '任务等待超时或被中断；未自动重新调用模型，请核对后手动重试。');
  }
}
async function admitWrite(db, owner, ip) {
  // Storage writes are bounded as well as paid calls. Counters may be charged
  // on a rejected request; they never grant a request beyond a limit.
  const date = day();
  // Narrow buckets go first: an already blocked visitor/IP must never spend
  // the remaining global allowance merely by repeating rejected requests.
  for (const [bucket, max] of [['write-' + owner, 200], ['write-' + ip, 500], ['write-global', 5000]]) {
    await stmt(db, 'INSERT OR IGNORE INTO moments_usage(day,bucket,used) VALUES(?,?,0)', date, bucket).run();
    const result = await stmt(db, 'UPDATE moments_usage SET used=used+1 WHERE day=? AND bucket=? AND used<?', date, bucket, max).run();
    if (!result.meta.changes) fail('WRITE_LIMIT', '今天的保存操作较多，请明天再继续。', 429);
  }
}
function note(row) {
  return { id: row.id, round_id: row.round_id, variant: row.variant, title: row.title, angle: row.angle, body: row.body,
    tags: [row.status, ...parse(row.tags_json, [])], status: row.status, kept: Boolean(row.kept), published_at: row.published_at,
    relative_path: row.id, final: Boolean(row.is_final) };
}
async function currentRound(db, owner, round) {
  if (!round) return null;
  return { round_id: round, notes: (await rows(stmt(db, 'SELECT * FROM moments_drafts WHERE owner_id=? AND round_id=? AND retired=0 ORDER BY variant', owner, round))).map(note) };
}
async function materialRows(db, owner) {
  const history = await rows(stmt(db, 'SELECT * FROM moments_history WHERE owner_id=? ORDER BY created_at DESC LIMIT 21', owner));
  const published = await rows(stmt(db, `SELECT * FROM moments_drafts WHERE owner_id=? AND status='已发布' AND is_final=1 ORDER BY updated_at DESC LIMIT 31`, owner));
  const items = history.slice(0, 20).map(h => ({ id: h.id, kind: 'history', relative_path: h.filename, title: h.filename, body: h.body, excerpt: h.body.slice(0, 240), reference_eligible: true }));
  for (const d of published.slice(0, 30)) {
    if (d.published_body_hash === await bodyHash(d.body)) items.push({ ...note(d), kind: 'final', excerpt: d.body.slice(0, 240), reference_eligible: true });
  }
  return { items, total: items.length, history_limit: 20, draft_limit: 30, coverage_complete: true, has_more: history.length > 20 || published.length > 30 };
}
async function quota(db, person, ip, env) {
  const bound = limits(env); const date = day();
  const get = async bucket => Number((await stmt(db, 'SELECT used FROM moments_usage WHERE day=? AND bucket=?', date, bucket).first())?.used || 0);
  const [visitor, network, global] = await Promise.all([get('visitor:' + person.id), get(ip), get('global')]);
  return { daily_limit: bound.visitor, remaining: Math.max(0, Math.min(bound.visitor - visitor, bound.ip - network, bound.global - global)), global_daily_limit: bound.global };
}
function modelState(env) { return { ...model, available: Boolean(env.MOMENTS_ZHIPU_API_KEY), authenticated: Boolean(env.MOMENTS_ZHIPU_API_KEY),
  message: env.MOMENTS_ZHIPU_API_KEY ? '由本站提供智谱额度，生成和润色共用每日次数。' : '模型连接正在准备，请稍后再试。' }; }
async function state(db, person, ip, env) {
  await expireJob(db, person.id);
  const w = await workspace(db, person.id); const saved = parse(w?.profiles_json || '{}'); const ready = Boolean(profileReady(saved));
  const profiles = Object.fromEntries(['author', 'audience', 'preferences'].map(k => [k, { fields: saved[k] || {}, body: '' }]));
  const materials = await materialRows(db, person.id);
  return { status: 'OK', demo: false, cloud: true, csrf_token: person.csrf, profiles, profile_ready: ready,
    onboarding: { profile_ready: ready, ready, history_required: false, history_ready: materials.items.some(x => x.kind === 'history'), history_count: materials.items.filter(x => x.kind === 'history').length },
    history: { items: materials.items.filter(x => x.kind === 'history') }, materials, model: modelState(env), model_options: options, rewrite_presets: rewritePresetOptions(),
    active_job: w?.active_job_id || null, round: await currentRound(db, person.id, w?.active_round_id),
    library: (await rows(stmt(db, 'SELECT * FROM moments_drafts WHERE owner_id=? AND retired=0 ORDER BY updated_at DESC LIMIT 50', person.id))).map(note), library_limit: 50,
    quota: await quota(db, person, ip, env), notice: '资料和稿件仅当前浏览器身份可访问，清理Cookie后无法自动找回。本站不会操作微信。' };
}
async function saveProfile(db, person, ip, data) {
  fields(data, ['author', 'audience', 'preferences']);
  const p = {};
  for (const [group, key, label] of [['author', 'identity', '身份与业务'], ['audience', 'people', '目标读者'], ['preferences', 'tone', '表达偏好']]) {
    fields(data[group], [key]); p[group] = { [key]: validateText(data[group][key], label) };
  }
  await ensureWorkspace(db, person.id, ip);
  const changed = await stmt(db, 'UPDATE moments_workspaces SET profiles_json=?,updated_at=? WHERE id=? AND active_job_id IS NULL', JSON.stringify(p), now(), person.id).run();
  if (!changed.meta.changes) fail('BUSY', 'AI正在工作，请完成后再保存资料。', 409);
  return { status: 'OK' };
}
async function idleWorkspace(db, id) {
  await expireJob(db, id);
  const w = await workspace(db, id);
  if (!w || !profileReady(parse(w.profiles_json))) fail('PROFILE_REQUIRED', '请先保存个人说明书的三项必填内容。');
  if (w.active_job_id) fail('BUSY', 'AI正在工作，请等待本次任务完成。', 409);
  return w;
}
async function target(db, owner, data, w) {
  if (typeof data.round_id !== 'string' || !Number.isInteger(data.variant) || ![1, 2, 3].includes(data.variant) || data.round_id !== w.active_round_id) fail('ROUND_CHANGED', '稿件轮次已变化，请刷新后重新选择。', 409);
  const d = await stmt(db, 'SELECT * FROM moments_drafts WHERE owner_id=? AND round_id=? AND variant=? AND retired=0', owner, data.round_id, data.variant).first();
  if (!d) fail('DRAFT_NOT_FOUND', '这条稿件已收尾，请刷新查看当前内容。', 404);
  return d;
}
async function submit(db, person, ip, env, kind, data) {
  if (!env.MOMENTS_ZHIPU_API_KEY || !env.SEMANTIC_QUEUE?.send) fail('MODEL_UNAVAILABLE', '写作连接正在准备，请稍后再试。', 503);
  fields(data, kind === 'generate' ? ['material'] : ['round_id', 'variant', 'instruction', 'preset'], kind === 'generate' ? ['material'] : ['round_id', 'variant']);
  const w = await idleWorkspace(db, person.id); let text; let original = null;
  if (kind === 'generate') {
    text = validateText(data.material, '本次材料', 8000);
    if (/^(https?:\/\/\S+\s*)+$/.test(text)) fail('SOURCE_TEXT_REQUIRED', '请粘贴原文要点并补充本人观点；页面不会自动读取链接。');
    if (Number((await stmt(db, 'SELECT COUNT(*) AS n FROM moments_drafts WHERE owner_id=?', person.id).first()).n) >= 300) fail('STORAGE_LIMIT', '当前工作区稿件已达上限，请复制需要的正文另存，暂不生成新一轮。', 429);
  } else {
    original = await target(db, person.id, data, w);
    if (original.status === '已发布') fail('ALREADY_PUBLISHED', '已发稿不能直接作为普通AI改稿目标。', 409);
    text = compileRevisionInstruction(data.preset, validateText(data.instruction || '', '修改意见', 4000, true));
  }
  const materials = await materialRows(db, person.id);
  const originals = materials.items.filter(x => x.kind === 'history'); const published = materials.items.filter(x => x.kind === 'final');
  const selected = originals.length && published.length ? [originals[0], published[0]] : (originals.length ? originals : published).slice(0, 2);
  const context = { profiles: parse(w.profiles_json), history: selected.map(x => ({ body: x.body.slice(0, 6000), kind: x.kind, use: '只参考写法，不搬用经历或观点' })), demo: false,
    link_access: { status: 'NOT_FETCHED' } };
  const id = crypto.randomUUID().replaceAll('-', ''); const time = now(); const date = day(); const bound = limits(env);
  const buckets = [['global', bound.global], ['visitor:' + person.id, bound.visitor], [ip, bound.ip]];
  await db.batch(buckets.map(([bucket]) => stmt(db, 'INSERT OR IGNORE INTO moments_usage(day,bucket,used) VALUES(?,?,0)', date, bucket)));
  const payload = { context, text, original: original ? { id: original.id, hash: await bodyHash(original.body), body: original.body } : null, expected_round: w.active_round_id };
  try {
    await db.batch([
      stmt(db, `INSERT INTO moments_jobs(id,owner_id,kind,target,status,phase,payload_json,created_at)
        SELECT ?,?,?,?,'QUEUED','queued',?,? WHERE
        (SELECT active_job_id FROM moments_workspaces WHERE id=?) IS NULL
        AND COALESCE((SELECT active_round_id FROM moments_workspaces WHERE id=?),'')=?
        AND ${buckets.map(() => '(SELECT used FROM moments_usage WHERE day=? AND bucket=?)<?').join(' AND ')}`,
      id, person.id, kind, original?.variant || null, JSON.stringify(payload), time, person.id, person.id, w.active_round_id || '', ...buckets.flatMap(([b, max]) => [date, b, max])),
      ...buckets.map(([b]) => stmt(db, 'UPDATE moments_usage SET used=used+1 WHERE day=? AND bucket=? AND EXISTS(SELECT 1 FROM moments_jobs WHERE id=?)', date, b, id)),
      stmt(db, 'UPDATE moments_workspaces SET active_job_id=?,updated_at=? WHERE id=? AND EXISTS(SELECT 1 FROM moments_jobs WHERE id=?)', id, time, person.id, id),
    ]);
  } catch { fail('BUSY', '当前有另一项写作任务，请刷新查看。', 409); }
  if (!await stmt(db, 'SELECT id FROM moments_jobs WHERE id=?', id).first()) {
    if ((await workspace(db, person.id))?.active_job_id) fail('BUSY', 'AI正在工作，请勿重复提交。', 409);
    fail('DAILY_LIMIT', '今天可用的AI次数已用完，请明天再来。', 429);
  }
  try { await env.SEMANTIC_QUEUE.send({ type: QUEUE_TYPE, job_id: id }); }
  catch { await jobFailed(db, id, person.id, 'QUEUE_UNAVAILABLE', '任务暂时未能进入队列；未自动重试，请稍后手动重试。'); fail('QUEUE_UNAVAILABLE', '任务未能入队，请稍后重试。', 503); }
  return { status: 'QUEUED', job_id: id, context_warnings: [] };
}
async function jobFailed(db, id, owner, code, message) {
  await db.batch([stmt(db, `UPDATE moments_jobs SET status='ERROR',phase='error',error_code=?,error_message=?,finished_at=? WHERE id=? AND owner_id=? AND status IN ('QUEUED','RUNNING')`, code, message, now(), id, owner),
    stmt(db, 'UPDATE moments_workspaces SET active_job_id=NULL WHERE id=? AND active_job_id=?', owner, id)]);
}
export const isMomentsMessage = message => message?.body?.type === QUEUE_TYPE;
export async function consumeMomentsMessage(message, env, { deadLetter = false, modelApi } = {}) {
  if (!isMomentsMessage(message)) return false;
  const db = env.COMMENTS_DB; const id = message.body.job_id;
  if (!/^[a-f0-9]{32}$/.test(id || '')) { message.ack(); return true; }
  const job = await stmt(db, 'SELECT * FROM moments_jobs WHERE id=?', id).first();
  if (!job || ['DONE', 'ERROR', 'RUNNING'].includes(job.status)) { message.ack(); return true; }
  if (deadLetter) { await jobFailed(db, id, job.owner_id, 'QUEUE_INTERRUPTED', '后台任务被中断；未重新调用模型，请稍后重试。'); message.ack(); return true; }
  const claimed = await stmt(db, `UPDATE moments_jobs SET status='RUNNING',phase='preparing',started_at=? WHERE id=? AND status='QUEUED'`, now(), id).run();
  if (!claimed.meta.changes) { message.ack(); return true; }
  try {
    const payload = parse(job.payload_json); const progress = async phase => {
      if (PHASES[phase]) await stmt(db, `UPDATE moments_jobs SET phase=? WHERE id=? AND status='RUNNING'`, phase, id).run();
    };
    const settings = { apiKey: env.MOMENTS_ZHIPU_API_KEY, progress };
    const api = modelApi || { generateMoments, reviseMoment };
    const result = job.kind === 'generate' ? await api.generateMoments(payload.context, payload.text, settings)
      : await api.reviseMoment(payload.context, payload.original.body, payload.text, settings);
    await progress('saving');
    const w = await workspace(db, job.owner_id);
    if (w?.active_job_id !== id || w.active_round_id !== payload.expected_round) fail('ROUND_CHANGED', '任务期间稿件发生变化，未覆盖已有内容。', 409);
    const time = now(); const writes = [];
    let guard = `EXISTS(SELECT 1 FROM moments_workspaces w JOIN moments_jobs j ON j.id=w.active_job_id
      WHERE w.id=? AND w.active_job_id=? AND COALESCE(w.active_round_id,'')=? AND j.status='RUNNING')`;
    const args = [job.owner_id, id, payload.expected_round || ''];
    let nextRound = w.active_round_id;
    if (job.kind === 'generate') {
      const round = crypto.randomUUID().replaceAll('-', '');
      nextRound = round;
      writes.push(stmt(db, `UPDATE moments_drafts SET retired=1 WHERE owner_id=? AND status='待发布' AND kept=0 AND ${guard}`, job.owner_id, ...args));
      for (let i = 0; i < 3; i++) {
        const d = result.variants[i];
        writes.push(stmt(db, `INSERT INTO moments_drafts(id,owner_id,round_id,variant,title,angle,body,tags_json,created_at,updated_at) SELECT ?,?,?,?,?,?,?,?,?,? WHERE ${guard}`,
          crypto.randomUUID(), job.owner_id, round, i + 1, d.title, d.angle, d.body, JSON.stringify(d.tags), time, time, ...args));
      }
    } else {
      const d = await stmt(db, 'SELECT * FROM moments_drafts WHERE owner_id=? AND id=?', job.owner_id, payload.original.id).first();
      if (!d || await bodyHash(d.body) !== payload.original.hash || d.status !== '待发布') fail('DRAFT_CHANGED', '原稿发生变化，未覆盖已有内容。', 409);
      guard += ` AND EXISTS(SELECT 1 FROM moments_drafts WHERE id=? AND owner_id=? AND body=? AND status='待发布' AND retired=0)`;
      args.push(d.id, job.owner_id, d.body);
      writes.push(stmt(db, `INSERT INTO moments_revisions(id,owner_id,draft_id,prior_body,revised_body,source,created_at) SELECT ?,?,?,?,?,'ai',? WHERE ${guard}`, crypto.randomUUID(), job.owner_id, d.id, d.body, result.body, time, ...args));
      // Complete the job while the old-body CAS still matches, then replace
      // the canonical body in the same atomic batch with a DONE job guard.
    }
    writes.push(stmt(db, `UPDATE moments_jobs SET status='DONE',phase='done',finished_at=? WHERE id=? AND ${guard}`, time, id, ...args));
    if (job.kind === 'revise') writes.push(stmt(db, `UPDATE moments_drafts SET body=?,updated_at=?,is_final=0,published_body_hash=NULL WHERE id=? AND owner_id=? AND body=? AND status='待发布' AND retired=0
      AND EXISTS(SELECT 1 FROM moments_workspaces w JOIN moments_jobs j ON j.id=w.active_job_id WHERE w.id=? AND w.active_job_id=? AND j.status='DONE')`, result.body, time, payload.original.id, job.owner_id, payload.original.body, job.owner_id, id));
    writes.push(stmt(db, `UPDATE moments_workspaces SET active_round_id=?,active_job_id=NULL,updated_at=? WHERE id=? AND active_job_id=? AND COALESCE(active_round_id,'')=? AND EXISTS(SELECT 1 FROM moments_jobs WHERE id=? AND status='DONE')`, nextRound, time, job.owner_id, id, payload.expected_round || '', id));
    const saved = await db.batch(writes);
    if (!saved.at(-1).meta.changes) fail('ROUND_CHANGED', '任务状态已变化，未覆盖已有稿件。', 409);
  } catch (error) {
    const known = error instanceof ModelError || error instanceof RequestError;
    await jobFailed(db, id, job.owner_id, known ? error.code : 'TASK_FAILED', known ? error.message : '本次写作未完成，未自动重试或切换模型。');
  }
  message.ack(); // A paid model request is never automatically retried.
  return true;
}
async function readJob(db, owner, id) {
  if (!/^[a-f0-9]{32}$/.test(id)) fail('JOB_NOT_FOUND', '任务不存在。', 404);
  const j = await stmt(db, 'SELECT * FROM moments_jobs WHERE owner_id=? AND id=?', owner, id).first();
  if (!j) fail('JOB_NOT_FOUND', '任务不存在。', 404);
  return { job_id: j.id, kind: j.kind, target: j.target, status: j.status, phase: j.phase, message: PHASES[j.phase] || j.error_message,
    created_at: j.created_at, started_at: j.started_at, finished_at: j.finished_at, ...model,
    ...(j.status === 'ERROR' ? { error: { code: j.error_code, message: j.error_message } } : {}), ...(j.status === 'DONE' ? { result: { context_warnings: [] } } : {}) };
}
async function mutateDraft(db, person, action, data) {
  fields(data, action === 'final' ? ['round_id', 'variant', 'body'] : action === 'keep' ? ['round_id', 'variant', 'keep'] : ['round_id', 'variant', 'confirmed']);
  const w = await idleWorkspace(db, person.id); const d = await target(db, person.id, data, w); const time = now();
  if (action === 'publish' && data.confirmed !== true) fail('CONFIRMATION_REQUIRED', '请明确确认本人已经实际发出这条朋友圈。');
  if (action === 'keep' && typeof data.keep !== 'boolean') fail('INVALID_INPUT', '保留状态不正确。');
  if (d.status === '已发布') fail('ALREADY_PUBLISHED', '已发布稿保持只读。', 409);
  const guard = ` AND status='待发布' AND retired=0 AND body=? AND EXISTS(SELECT 1 FROM moments_workspaces WHERE id=? AND active_job_id IS NULL AND active_round_id=?)`;
  const guardArgs = [d.body, person.id, d.round_id];
  if (action === 'final') {
    validateText(data.body, '终稿', 8000); const body = data.body;
    if (body !== d.body && Number((await stmt(db, 'SELECT COUNT(*) AS n FROM moments_revisions WHERE owner_id=?', person.id).first()).n) >= 300) fail('STORAGE_LIMIT', '当前工作区改稿记录已达上限，暂不继续增加版本。', 429);
    const writes = [];
    if (body !== d.body) writes.push(stmt(db, `INSERT INTO moments_revisions(id,owner_id,draft_id,prior_body,revised_body,source,created_at)
      SELECT ?,?,?,?,?,'manual',? WHERE EXISTS(SELECT 1 FROM moments_drafts WHERE id=? AND owner_id=? ${guard})`, crypto.randomUUID(), person.id, d.id, d.body, body, time, d.id, person.id, ...guardArgs));
    writes.push(stmt(db, 'UPDATE moments_drafts SET body=?,is_final=1,published_body_hash=NULL,updated_at=? WHERE id=? AND owner_id=?' + guard, body, time, d.id, person.id, ...guardArgs));
    const results = await db.batch(writes);
    if (!results.at(-1).meta.changes) fail('DRAFT_CHANGED', '稿件状态已变化，请刷新后重新确认。', 409);
  } else if (action === 'keep') {
    const result = await stmt(db, 'UPDATE moments_drafts SET kept=?,updated_at=? WHERE id=? AND owner_id=?' + guard, Number(data.keep), time, d.id, person.id, ...guardArgs).run();
    if (!result.meta.changes) fail('BUSY', 'AI正在工作，请稍后再操作。', 409);
  } else {
    const results = await db.batch([
      stmt(db, `UPDATE moments_drafts SET status='已发布',is_final=1,published_body_hash=?,published_at=?,updated_at=? WHERE id=? AND owner_id=?` + guard, await bodyHash(d.body), time, time, d.id, person.id, ...guardArgs),
      stmt(db, `UPDATE moments_drafts SET retired=1 WHERE owner_id=? AND round_id=? AND variant<>? AND status='待发布' AND kept=0
        AND EXISTS(SELECT 1 FROM moments_drafts d WHERE d.id=? AND d.status='已发布' AND d.body=? AND d.published_body_hash=?)
        AND EXISTS(SELECT 1 FROM moments_workspaces WHERE id=? AND active_job_id IS NULL AND active_round_id=?)`, person.id, d.round_id, d.variant, d.id, d.body, await bodyHash(d.body), person.id, d.round_id),
    ]);
    if (!results[0].meta.changes) fail('BUSY', 'AI正在工作，请稍后再确认。', 409);
  }
  return { status: 'OK' };
}
async function importHistory(db, person, data) {
  fields(data, ['filename', 'body']); await idleWorkspace(db, person.id);
  const name = validateText(data.filename, '文件名', 120); validateText(data.body, '历史正文', 60000); const body = data.body;
  if (!/\.(?:md|txt)$/i.test(name) || /[\\/\0]|^\./.test(name)) fail('INVALID_INPUT', '请使用普通.md或.txt文件名。');
  if (Number((await stmt(db, 'SELECT COUNT(*) AS n FROM moments_history WHERE owner_id=?', person.id).first()).n) >= 20) fail('STORAGE_LIMIT', '当前工作区最多保存20篇导入作品。', 429);
  try {
    const saved = await stmt(db, `INSERT INTO moments_history(id,owner_id,filename,body,body_hash,created_at) SELECT ?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM moments_workspaces WHERE id=? AND active_job_id IS NULL)
      AND (SELECT COUNT(*) FROM moments_history WHERE owner_id=?)<20`,
      crypto.randomUUID(), person.id, name, body, await bodyHash(body), now(), person.id, person.id).run();
    if (!saved.meta.changes) fail('BUSY', '工作区状态已变化，请刷新后再保存。', 409);
  } catch (error) { if (error instanceof RequestError) throw error; fail('HISTORY_EXISTS', '同名作品已存在，不会覆盖原文。', 409); }
  return { status: 'OK' };
}
export async function handleMomentsRequest(request, env) {
  const path = new URL(request.url).pathname;
  if (!path.startsWith('/moments/api/')) return null;
  let person;
  try {
    person = await identity(request, env); const db = env.COMMENTS_DB; const ip = await ipBucket(request, env);
    const endpoint = path.slice('/moments/api'.length);
    if (request.method === 'GET') {
      if (endpoint === '/state') return json(await state(db, person, ip, env), 200, person.cookie);
      if (endpoint.startsWith('/jobs/')) { await expireJob(db, person.id); return json(await readJob(db, person.id, endpoint.slice(6)), 200, person.cookie); }
      if (endpoint.startsWith('/history/')) {
        const h = await stmt(db, 'SELECT * FROM moments_history WHERE owner_id=? AND id=?', person.id, endpoint.slice(9)).first();
        if (!h) fail('HISTORY_NOT_FOUND', '作品不存在。', 404);
        return json({ status: 'OK', item: { id: h.id, relative_path: h.filename, body: h.body } }, 200, person.cookie);
      }
      fail('NOT_FOUND', '接口不存在。', 404);
    }
    if (request.method !== 'POST') fail('METHOD_NOT_ALLOWED', '请求方法不支持。', 405);
    validateOrigin(request, person); const data = await input(request); await admitWrite(db, person.id, ip); let result;
    if (endpoint === '/profile') result = await saveProfile(db, person, ip, data);
    else if (endpoint === '/history') result = await importHistory(db, person, data);
    else if (['/generate', '/revise'].includes(endpoint)) result = await submit(db, person, ip, env, endpoint.slice(1), data);
    else if (['/final', '/keep', '/publish'].includes(endpoint)) result = await mutateDraft(db, person, endpoint.slice(1), data);
    else if (endpoint === '/model') { fields(data, ['provider']); if (data.provider !== 'zhipu') fail('INVALID_PROVIDER', '线上工作台目前使用智谱GLM-5.3-Flash。'); result = { status: 'OK', model: modelState(env), model_options: options }; }
    else fail('NOT_FOUND', '接口不存在。', 404);
    return json(result, result.status === 'QUEUED' ? 202 : 200, person.cookie);
  } catch (error) {
    const known = error instanceof RequestError || error instanceof ModelError;
    return json({ status: 'ERROR', code: known ? error.code : 'SERVICE_UNAVAILABLE', message: known ? error.message : '写作服务暂时不可用，请稍后再试。' }, known ? (error.status || 400) : 503, person?.cookie);
  }
}
