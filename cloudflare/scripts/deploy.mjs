import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

export function assertDeployable(config) {
  const database = config.d1_databases?.find(item => item.binding === 'COMMENTS_DB');
  if (!database || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(database.database_id || '')
      || database.database_id === '00000000-0000-0000-0000-000000000000') {
    throw new Error('Deployment stopped: create your own D1 database and replace the placeholder database_id in cloudflare/wrangler.jsonc.');
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = fileURLToPath(new URL('../', import.meta.url));
  // The distributed template is valid JSON as well as JSONC. Keep it that way:
  // parsing strictly prevents a deployment guard from silently skipping a config.
  const config = JSON.parse(readFileSync(new URL('../wrangler.jsonc', import.meta.url), 'utf8'));
  try { assertDeployable(config); }
  catch (error) { console.error(error.message); process.exit(1); }
  const wrangler = fileURLToPath(new URL('../node_modules/wrangler/bin/wrangler.js', import.meta.url));
  const result = spawnSync(process.execPath, [wrangler, 'deploy', '--strict'], { cwd: root, stdio: 'inherit' });
  if (result.error) { console.error('Wrangler could not start. Run npm ci in cloudflare first.'); process.exit(1); }
  process.exit(result.status ?? 1);
}
