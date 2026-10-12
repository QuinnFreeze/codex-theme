import test, {after} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import {createHash} from 'node:crypto';
import {build, root} from '../theme/scripts/build.mjs';
import {configDigest, documentExpression, reloadSessions} from '../theme/scripts/runtime.mjs';
import * as runtime from '../theme/scripts/runtime.mjs';

const originalWebSocket = globalThis.WebSocket;
globalThis.WebSocket = {OPEN: 1};
after(() => { globalThis.WebSocket = originalWebSocket; });

const payload = '(()=>({installed:true}))()';
const nextDigest = 'next-config';

function mockSession(id, options = {}) {
  const oldHook = `${id}-old`;
  const calls = [];
  const evaluations = [];
  const hooks = new Set(options.hooks ?? [oldHook]);
  let sequence = 0;
  const connection = {
    ws: {readyState: options.readyState ?? WebSocket.OPEN},
    async call(method, params) {
      calls.push({method, params});
      if (method === 'Page.addScriptToEvaluateOnNewDocument') {
        if (options.addError) throw Error(options.addError);
        const identifier = `${id}-new-${++sequence}`;
        hooks.add(identifier);
        return {identifier};
      }
      if (method === 'Page.removeScriptToEvaluateOnNewDocument') {
        if (options.removeErrors?.includes(params.identifier)) throw Error('cleanup unavailable');
        hooks.delete(params.identifier);
        return {};
      }
      throw Error(`Unexpected CDP method: ${method}`);
    },
    async eval(source) {
      evaluations.push(source);
      if (options.evalError) throw Error(options.evalError);
      return options.evalResult ?? {installed: true};
    },
  };
  return {
    session: {
      c: connection,
      script: oldHook,
      applied: options.applied ?? true,
      main: options.main ?? true,
      configDigest: options.configDigest ?? 'previous-config',
      ...(options.retiring ? {retiring: Promise.resolve()} : {}),
    },
    oldHook, calls, evaluations, hooks,
  };
}

function harness(entries) {
  const sessions = new Map(entries.map(([id, mock]) => [id, mock.session]));
  const registry = Object.fromEntries(entries.map(([id, mock]) => [id, [mock.oldHook]]));
  const snapshots = [];
  return {
    sessions,
    registry,
    persist: () => snapshots.push(structuredClone(registry)),
    snapshots,
  };
}

function daemonRemovalHarness(entries, pages = async () => [], Connection = class {
  constructor() { throw Error('the isolated fallback probe requires a mocked connection'); }
}) {
  const source = fs.readFileSync(path.join(root, 'scripts/runtime.mjs'), 'utf8');
  const start = source.indexOf('  const remove = async () => {');
  const end = source.indexOf('\n  try {\n    fs.unlinkSync(socketPath);', start);
  assert.ok(start !== -1 && end !== -1, 'the isolated probe must execute the real daemon cleanup');
  const state = harness(entries);
  state.persistenceOptions = [];
  const context = {
    ...state, stopping: false, browserDead: false, pages, Connection, port: 9999,
    browser: {close() {}}, removeExpression: 'mock document cleanup',
    persistRegistry: retainEmpty => { state.persistenceOptions.push(retainEmpty); state.persist(); },
    removeTargetTheme: runtime.removeTargetTheme, console,
  };
  return {state, run: () => vm.runInNewContext(source.slice(start, end) + '\nremove()', context)};
}

test('document cleanup failure remains retryable and never confirms daemon removal', async () => {
  let failDocument = true;
  const mock = mockSession('main');
  mock.session.c.close = () => {};
  mock.session.c.eval = async () => {
    if (failDocument) throw Error('temporary document cleanup failure');
    return {removed: true};
  };
  const fallback = daemonRemovalHarness([['main', mock]]);
  const failed = await fallback.run();
  assert.equal(failed.removed, false);
  assert.match(failed.errors[0].error, /document cleanup failure/);
  assert.deepEqual(fallback.state.registry.main, [], 'an empty registration marks pending document cleanup');

  failDocument = false;
  const retry = await runtime.removeTargetTheme('main', mock.session.c, fallback.state);
  assert.deepEqual(retry, []);
  assert.equal(Object.hasOwn(fallback.state.registry, 'main'), false);
});

test('failed document cleanup preserves successful removal from other sessions', async () => {
  const failed = mockSession('main');
  const successful = mockSession('detached', {main: false});
  for (const mock of [failed, successful]) mock.session.c.close = () => {};
  failed.session.c.eval = async () => { throw Error('main document unavailable'); };
  successful.session.c.eval = async () => ({removed: true});
  const probe = daemonRemovalHarness([['main', failed], ['detached', successful]]);
  const result = await probe.run();
  assert.equal(result.removed, false);
  assert.deepEqual(probe.state.registry, {main: []});
  assert.equal(successful.hooks.size, 0);
  assert.equal(failed.hooks.size, 0);
  assert.equal(result.errors.length, 1);
});

test('daemon cleanup retains empty retry state when fallback inventory is unverified', async t => {
  t.mock.method(console, 'error', () => {});
  const mock = mockSession('main');
  mock.session.c.close = () => {};
  mock.session.c.eval = async () => ({removed: true});
  const probe = daemonRemovalHarness([['main', mock]], async () => { throw Error('inventory unavailable'); });
  const result = await probe.run();
  assert.equal(result.removed, false);
  assert.match(result.errors.at(-1).error, /inventory unavailable/);
  assert.equal(probe.state.persistenceOptions.at(-1), true, 'keep an empty state file so standalone cleanup can retry');
});

test('daemon cleanup confirms removal after a temporary document failure recovers', async () => {
  const mock = mockSession('main');
  let attempts = 0;
  mock.session.c.close = () => {};
  mock.session.c.eval = async () => {
    if (++attempts === 1) throw Error('temporary document failure');
    return {removed: true};
  };
  const probe = daemonRemovalHarness([['main', mock]], async () => [{
    id: 'main', webSocketDebuggerUrl: 'ws://127.0.0.1:9999/mock',
  }], class { constructor() { return mock.session.c; } });
  const result = await probe.run();
  assert.equal(result.removed, true);
  assert.equal(attempts, 2);
  assert.deepEqual(probe.state.registry, {});
  assert.equal(result.errors.length, 1, 'report the recovered failure without turning verified removal into failure');
});

test('retirement retains document retry state when no hook registration existed', async t => {
  t.mock.method(console, 'error', () => {});
  const mock = mockSession('main');
  mock.session.script = null;
  mock.session.c.close = () => {};
  mock.session.c.eval = async () => { throw Error('document unavailable'); };
  const state = {sessions: new Map([['main', mock.session]]), registry: {}, persist() {}};
  await runtime.retireSession('main', mock.session, state);
  assert.deepEqual(state.registry.main, []);
  assert.equal(state.sessions.has('main'), false);
});

test('unconfirmed document removal retains its pending cleanup registration', async () => {
  const mock = mockSession('main');
  mock.session.c.eval = async () => ({removed: false});
  const state = harness([['main', mock]]);
  const errors = await runtime.removeTargetTheme('main', mock.session.c, state);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].stage, 'document');
  assert.deepEqual(state.registry.main, []);
});

test('standalone cleanup retries an empty pending document registration', async () => {
  const source = fs.readFileSync(path.join(root, 'scripts/runtime.mjs'), 'utf8');
  const start = source.indexOf("if (command === 'once-remove') {");
  const end = source.indexOf("\nif (command === 'daemon') {", start);
  let saved = JSON.stringify({main: []}), deleted = false, evaluated = 0;
  const replies = [];
  const context = {
    command: 'once-remove', port: 9999, registryPath: '/mock/registrations.json',
    ensureStateDir() {}, removeTargetTheme: runtime.removeTargetTheme,
    pages: async () => [{id: 'main', webSocketDebuggerUrl: 'ws://127.0.0.1:9999/mock'}],
    Connection: class {
      async call() { throw Error('an empty registration must not remove a hook'); }
      async eval() { evaluated++; return {removed: true}; }
      close() {}
    },
    fs: {existsSync: () => true, readFileSync: () => saved,
      writeFileSync: (_path, value) => { saved = value; }, rmSync: () => { deleted = true; }},
    console: {log: value => replies.push(JSON.parse(value))},
  };
  await vm.runInNewContext('(async () => {' + source.slice(start, end) + '})()', context);
  assert.equal(evaluated, 1);
  assert.equal(deleted, true);
  assert.equal(replies[0].removed, true);
});

test('standalone cleanup does not confirm removal when target inventory fails', async () => {
  const source = fs.readFileSync(path.join(root, 'scripts/runtime.mjs'), 'utf8');
  const start = source.indexOf("if (command === 'once-remove') {");
  const end = source.indexOf("\nif (command === 'daemon') {", start);
  let deleted = false;
  const replies = [];
  const context = {
    command: 'once-remove', port: 9999, registryPath: '/mock/registrations.json',
    ensureStateDir() {}, removeTargetTheme: runtime.removeTargetTheme,
    pages: async () => { throw Error('inventory unavailable'); },
    fs: {existsSync: () => true, readFileSync: () => '{}',
      writeFileSync() {}, rmSync: () => { deleted = true; }},
    console: {log: value => replies.push(JSON.parse(value))},
  };
  await vm.runInNewContext('(async () => {' + source.slice(start, end) + '})()', context);
  assert.equal(replies[0].removed, false);
  assert.equal(deleted, false);
  assert.match(replies[0].errors[0].error, /inventory unavailable/);
});

function fixture(t) {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-theme-build-'));
  const themeRoot = path.join(parent, 'theme');
  fs.mkdirSync(path.join(themeRoot, 'wallpaper'), {recursive: true});
  fs.mkdirSync(path.join(themeRoot, 'styles'));
  fs.copyFileSync(path.join(root, 'wallpaper/current.jpg'), path.join(themeRoot, 'wallpaper/current.jpg'));
  for (const filename of ['wallpaper.css', 'liquid-glass.css', 'codex-overrides.css']) {
    fs.copyFileSync(path.join(root, 'styles', filename), path.join(themeRoot, 'styles', filename));
  }
  const settings = JSON.parse(fs.readFileSync(path.join(root, 'settings.json'), 'utf8'));
  fs.writeFileSync(path.join(themeRoot, 'settings.json'), JSON.stringify(settings));
  t.after(() => fs.rmSync(parent, {recursive: true, force: true}));
  return {parent, themeRoot, settings};
}

test('hot reload replaces current CSS and leaves exactly one future-document hook', async () => {
  const mock = mockSession('main');
  const state = harness([['main', mock]]);
  const result = await reloadSessions(state, payload, nextDigest);

  assert.equal(result.applied, true);
  assert.equal(result.sessions, 1);
  assert.equal(result.shellSessions, 1);
  assert.deepEqual(result.errors, []);
  assert.equal(mock.evaluations.length, 1);
  assert.deepEqual(state.registry.main, ['main-new-1']);
  assert.deepEqual([...mock.hooks], ['main-new-1']);
  assert.equal(mock.session.script, 'main-new-1');
  assert.equal(mock.session.configDigest, nextDigest);
  assert.deepEqual(state.snapshots[0].main, ['main-old', 'main-new-1']);

  const repeated = await reloadSessions(state, payload, 'another-config');
  assert.equal(repeated.applied, true);
  assert.deepEqual(state.registry.main, ['main-new-2']);
  assert.deepEqual([...mock.hooks], ['main-new-2']);
  assert.equal(mock.evaluations.length, 2);
});

test('already closed, retiring, and unapplied sessions are untouched', async () => {
  const entries = [
    ['closed', mockSession('closed', {readyState: 3})],
    ['retiring', mockSession('retiring', {retiring: true})],
    ['unapplied', mockSession('unapplied', {applied: false})],
  ];
  const state = harness(entries);
  const result = await reloadSessions(state, payload, nextDigest);

  assert.equal(result.applied, false);
  assert.equal(result.sessions, 0);
  assert.equal(result.shellSessions, 0);
  assert.deepEqual(result.errors, []);
  assert.equal(state.snapshots.length, 0);
  for (const [id, mock] of entries) {
    assert.deepEqual(mock.calls, []);
    assert.deepEqual(mock.evaluations, []);
    assert.deepEqual(state.registry[id], [mock.oldHook]);
  }
});

test('failed hook cleanup retains its registration and reports a partial reload', async () => {
  const mock = mockSession('main', {removeErrors: ['main-old']});
  const state = harness([['main', mock]]);
  const result = await reloadSessions(state, payload, nextDigest);

  assert.equal(result.applied, false);
  assert.deepEqual(result.errors, [{target: 'main', error: 'cleanup unavailable'}]);
  assert.deepEqual(state.registry.main, ['main-old', 'main-new-1']);
  assert.deepEqual([...mock.hooks], ['main-old', 'main-new-1']);
  assert.equal(mock.session.script, 'main-new-1');
  assert.equal(mock.session.configDigest, nextDigest);
});

test('failed current-document evaluation retains hooks and does not report new configuration applied', async () => {
  const mock = mockSession('main', {evalError: 'renderer disconnected'});
  const state = harness([['main', mock]]);
  const result = await reloadSessions(state, payload, nextDigest);

  assert.equal(result.applied, false);
  assert.equal(result.sessions, 0);
  assert.equal(result.shellSessions, 0);
  assert.deepEqual(result.errors, [{target: 'main', error: 'renderer disconnected'}]);
  assert.deepEqual(state.registry.main, ['main-old', 'main-new-1']);
  assert.equal(mock.session.configDigest, 'previous-config');
});

test('failed new hook registration preserves the original future-document hook', async () => {
  const mock = mockSession('main', {addError: 'debugger disconnected'});
  const state = harness([['main', mock]]);
  const result = await reloadSessions(state, payload, nextDigest);

  assert.equal(result.applied, false);
  assert.deepEqual(state.registry.main, ['main-old']);
  assert.deepEqual([...mock.hooks], ['main-old']);
  assert.equal(mock.session.script, 'main-old');
  assert.equal(mock.session.configDigest, 'previous-config');
  assert.deepEqual(mock.evaluations, []);
});

test('a document that navigated away is not left marked as an active theme session', async () => {
  const mock = mockSession('main', {evalResult: {installed: false}});
  const state = harness([['main', mock]]);
  const result = await reloadSessions(state, payload, nextDigest);

  assert.equal(result.applied, false);
  assert.equal(result.sessions, 0);
  assert.equal(result.shellSessions, 0);
  assert.match(result.errors[0].error, /document changed/);
  assert.equal(mock.session.configDigest, 'previous-config');
  assert.equal(mock.session.applied, false, 'later status must not count the external document as themed');
});

test('reload counts all successful windows and distinguishes main shell sessions', async () => {
  const entries = [
    ['main-a', mockSession('main-a')],
    ['main-b', mockSession('main-b')],
    ['detached', mockSession('detached', {main: false})],
  ];
  const state = harness(entries);
  const result = await reloadSessions(state, payload, nextDigest);

  assert.equal(result.applied, true);
  assert.equal(result.sessions, 3);
  assert.equal(result.shellSessions, 2);
  assert.equal(result.configSha256, nextDigest);
  for (const [id, mock] of entries) {
    assert.deepEqual(state.registry[id], [`${id}-new-1`]);
    assert.equal(mock.evaluations.length, 1);
  }
});

test('a detached window alone does not claim the main Codex shell was updated', async () => {
  const mock = mockSession('detached', {main: false});
  const result = await reloadSessions(harness([['detached', mock]]), payload, nextDigest);
  assert.equal(result.applied, false);
  assert.equal(result.sessions, 1);
  assert.equal(result.shellSessions, 0);
});

test('document expression never evaluates wallpaper payload on an external page', () => {
  const context = {location: {href: 'https://example.com'}, touched: false};
  const result = vm.runInNewContext(documentExpression('(()=>{touched=true;return {installed:true}})()'), context);
  assert.equal(result.installed, false);
  assert.equal(context.touched, false);
});

test('config digest describes the exact current configuration bytes', () => {
  const expected = createHash('sha256').update(fs.readFileSync(path.join(root, 'settings.json'))).digest('hex');
  assert.equal(configDigest(), expected);
  assert.match(configDigest(), /^[a-f0-9]{64}$/);
});

test('legacy settings build with compatible wallpaper defaults', t => {
  const {themeRoot} = fixture(t);
  const result = build(themeRoot);
  assert.equal(result.cfg.fit, 'cover');
  assert.equal(result.cfg.wallpaperBrightness, 1);
  assert.equal(result.cfg.wallpaperBlur, 0);
  assert.match(result.css, /data:image\/jpeg;base64,/);
  assert.match(result.css, /--wynn-wallpaper-fit:cover;/);
  assert.match(result.css, /--wynn-wallpaper-brightness:1;/);
  assert.match(result.css, /--wynn-wallpaper-blur:0px;/);
});

test('custom fit, position, brightness, and blur reach the generated stylesheet', t => {
  const {themeRoot, settings} = fixture(t);
  const result = build(themeRoot, {...settings, fit: 'contain', position: '0% 100%', wallpaperBrightness: 0.25, wallpaperBlur: 24});
  assert.match(result.css, /--wynn-wallpaper-fit:contain;/);
  assert.match(result.css, /--wynn-position:0% 100%;/);
  assert.match(result.css, /--wynn-wallpaper-brightness:0.25;/);
  assert.match(result.css, /--wynn-wallpaper-blur:24px;/);
  assert.doesNotThrow(() => build(themeRoot, {...settings, wallpaperBrightness: 2, wallpaperBlur: 0}));
});

test('invalid wallpaper controls and opacity values fail before application', t => {
  const {themeRoot, settings} = fixture(t);
  for (const [field, invalidValues] of [
    ['fit', ['stretch', '', null]],
    ['position', ['101% 0%', '0% 100.1%', '-1% 50%', 'center', '50% 50%; color:red']],
    ['wallpaperBrightness', [0.249, 2.001, NaN, '1']],
    ['wallpaperBlur', [-0.1, 24.1, Infinity, '0']],
    ['mainAlpha', [-0.1, 1.1, '0.5']],
  ]) {
    for (const value of invalidValues) {
      assert.throws(() => build(themeRoot, {...settings, [field]: value}), undefined, `${field}=${String(value)}`);
    }
  }
});

test('wallpaper paths cannot escape the theme directory directly or through symlinks', t => {
  const {parent, themeRoot, settings} = fixture(t);
  const outside = path.join(parent, 'outside.jpg');
  fs.copyFileSync(path.join(themeRoot, 'wallpaper/current.jpg'), outside);
  const escapedLink = path.join(themeRoot, 'wallpaper/escaped.jpg');
  fs.symlinkSync(outside, escapedLink);
  for (const wallpaper of ['../outside.jpg', outside, 'wallpaper/escaped.jpg']) {
    assert.throws(() => build(themeRoot, {...settings, wallpaper}), /must stay inside/);
  }
  assert.doesNotThrow(() => build(themeRoot, {...settings, wallpaper: 'wallpaper/../wallpaper/current.jpg'}));
});

test('oversized and unsupported wallpaper files are rejected', t => {
  const {themeRoot, settings} = fixture(t);
  fs.writeFileSync(path.join(themeRoot, 'wallpaper/unsupported.svg'), '<svg/>');
  assert.throws(() => build(themeRoot, {...settings, wallpaper: 'wallpaper/unsupported.svg'}), /jpg\/png\/webp/);
  const oversized = path.join(themeRoot, 'wallpaper/oversized.jpg');
  const file = fs.openSync(oversized, 'w');
  fs.ftruncateSync(file, 20 * 1024 * 1024 + 1);
  fs.closeSync(file);
  assert.throws(() => build(themeRoot, {...settings, wallpaper: 'wallpaper/oversized.jpg'}), /20 MiB/);
});
