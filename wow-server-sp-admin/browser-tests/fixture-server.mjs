/**
 * Disposable browser-test server for the checked-in admin templates/assets.
 * It deliberately avoids the FastAPI runtime and all stack credentials; the
 * browser executes the real CSS, HTMX, and page scripts against deterministic
 * endpoint responses.
 */
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize, resolve } from 'node:path';

const root = resolve(process.cwd());
const staticRoot = resolve(root, 'app/static');
let status = 'running';

const keys = [
  {
    key: 'AiPlayerbot.MinRandomBots', default: '50',
    comment: 'Target number of random playerbots', env_var: 'AC_PLAYERBOT_MIN_RANDOM_BOTS',
    effective_value: '50', source: 'admin', source_file: 'playerbots.conf.dist', read_only: false,
  },
  {
    key: 'WorldServerPort', default: '8085', comment: 'World server port',
    env_var: 'AC_WORLDSERVER_PORT', effective_value: '8085', source: 'default',
    source_file: 'worldserver.conf.dist', read_only: true, read_only_reason: 'installer-managed',
  },
];

function send(response, code, body, contentType = 'text/html; charset=utf-8') {
  response.writeHead(code, { 'content-type': contentType, 'cache-control': 'no-store' });
  response.end(body);
}

function json(response, body, code = 200) {
  send(response, code, JSON.stringify(body), 'application/json; charset=utf-8');
}

async function render(name) {
  const [base, page] = await Promise.all([
    readFile(join(root, 'app/templates/base.html'), 'utf8'),
    readFile(join(root, 'app/templates', name), 'utf8'),
  ]);
  const main = page
    .replace(/^\{% extends "base.html" %\}\s*\{% block main %\}/, '')
    .replace(/\{% endblock %\}\s*$/, '');
  return base
    .replace('{% block main %}{% endblock %}', main)
    .replace(/\{\{[^}]+\}\}/g, 'browser')
    .replace(/\{%[^%]+%\}/g, '');
}

const contentTypes = {
  '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.png': 'image/png', '.svg': 'image/svg+xml',
};

const server = createServer(async (request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1:4173');
  const { pathname } = url;
  try {
    if (pathname.startsWith('/static/')) {
      const filename = pathname === '/static/htmx.min.js'
        ? resolve(root, 'node_modules/htmx.org/dist/htmx.min.js')
        : resolve(staticRoot, normalize(pathname.slice('/static/'.length)));
      if (!filename.startsWith(`${staticRoot}/`) && filename !== resolve(root, 'node_modules/htmx.org/dist/htmx.min.js')) return send(response, 403, 'forbidden');
      return send(response, 200, await readFile(filename), contentTypes[extname(filename)] || 'application/octet-stream');
    }
    if (pathname === '/') return send(response, 200, await render('dashboard.html'));
    if (pathname === '/settings') return send(response, 200, await render('settings.html'));
    if (pathname === '/api/keys') return json(response, keys);
    if (pathname === '/api/status') {
      return send(response, 200, `<div class="stat-card"><div class="stat-label">Server Status</div><div class="stat-value status-${status}">${status.toUpperCase()}</div></div>`);
    }
    if (pathname.startsWith('/__fixture/status/') && request.method === 'POST') {
      const candidate = pathname.split('/').at(-1);
      if (!['running', 'exited', 'created', 'paused', 'restarting', 'dead', 'removing', 'missing', 'unknown'].includes(candidate)) return json(response, { detail: 'unsupported fixture status' }, 400);
      status = candidate;
      return json(response, { status });
    }
    if (['/api/stats', '/api/players', '/api/backups', '/api/logs'].includes(pathname)) {
      return send(response, 200, '<div class="stat-card"><div class="stat-label">Fixture data</div></div>');
    }
    if (['/api/settings/apply', '/api/settings/rollback'].includes(pathname) && request.method === 'POST') {
      return json(response, { id: 'browser-action', status: 'running' });
    }
    if (pathname === '/api/action/stream') {
      response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store', connection: 'keep-alive' });
      return setTimeout(() => {
        response.write('event: progress\ndata: <li class="step step-restart">restart started</li>\n\n');
        setTimeout(() => response.end('event: done\ndata: <li class="action-done action-ok" data-status="ok">done</li>\n\n'), 50);
      }, 50);
    }
    return send(response, 404, 'not found');
  } catch (error) {
    return send(response, 500, String(error));
  }
});

server.listen(4173, '127.0.0.1');
