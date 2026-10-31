import { consumeMomentsMessage, handleMomentsRequest, isMomentsMessage } from './moments.mjs';

function secure(response) {
  const headers = new Headers(response.headers);
  headers.set('referrer-policy', 'same-origin');
  headers.set('x-content-type-options', 'nosniff');
  headers.set('x-frame-options', 'DENY');
  headers.set('content-security-policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/' || url.pathname === '/moments') {
      return secure(Response.redirect(new URL('/moments/' + url.search, url), 308));
    }
    if (!url.pathname.startsWith('/moments/')) return secure(new Response('Not Found', { status: 404 }));
    const api = await handleMomentsRequest(request, env);
    if (api) return secure(api);
    if (!['GET', 'HEAD'].includes(request.method)) {
      return secure(new Response('Method Not Allowed', { status: 405, headers: { allow: 'GET, HEAD' } }));
    }
    return secure(await env.ASSETS.fetch(request));
  },
  async queue(batch, env) {
    const deadLetter = batch.queue.endsWith('-dlq');
    for (const message of batch.messages) {
      // This standalone queue serves writing only. Unknown messages never reach
      // a model, and are acknowledged to prevent a poison-message retry loop.
      if (!isMomentsMessage(message)) { message.ack(); continue; }
      if (!env.COMMENTS_DB) throw new Error('Moments database binding is unavailable.');
      await consumeMomentsMessage(message, env, { deadLetter });
    }
  },
};
