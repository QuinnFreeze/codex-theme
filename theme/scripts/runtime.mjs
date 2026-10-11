import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {root, expression, removeExpression} from './build.mjs';

export const state = path.join(root, 'state');

export function ensureStateDir() {
  fs.mkdirSync(state, {recursive: true, mode: 0o700});
}

export function verifyOwner(port, expectedPid) {
  const out = execFileSync('/usr/sbin/lsof', ['-nP', '-iTCP:' + port, '-sTCP:LISTEN', '-t'], {encoding: 'utf8'}).trim();
  const ids = [...new Set(out.split(/\s+/))].filter(Boolean);
  if (!ids.length) throw Error('No listener');

  const currentUid = typeof process.getuid === 'function' ? process.getuid() : null;
  const metaFile = path.join(state, 'application.json');
  const meta = fs.existsSync(metaFile) ? JSON.parse(fs.readFileSync(metaFile, 'utf8')) : null;
  const app = meta?.applicationPath || '/Applications/ChatGPT.app';
  const plist = path.join(app, 'Contents/Info.plist');
  const bundle = execFileSync('/usr/bin/plutil', ['-extract', 'CFBundleIdentifier', 'raw', plist], {encoding: 'utf8'}).trim();
  const name = execFileSync('/usr/bin/plutil', ['-extract', 'CFBundleExecutable', 'raw', plist], {encoding: 'utf8'}).trim();
  if (bundle !== 'com.openai.codex' || path.basename(name) !== name) throw Error('Invalid Codex bundle');
  const expected = fs.realpathSync(path.join(app, 'Contents/MacOS', name));

  const targetPid = expectedPid ?? meta?.pid;
  if (targetPid && !ids.includes(String(targetPid))) {
    throw Error(`Target application PID ${targetPid} is not listening on port ${port}`);
  }

  for (const pid of ids) {
    const psOut = execFileSync('/bin/ps', ['-p', pid, '-o', 'uid=,comm='], {encoding: 'utf8'}).trim();
    const [procUid, ...commParts] = psOut.split(/\s+/);
    if (currentUid !== null && Number(procUid) !== currentUid) {
      throw Error(`Debugger owner process UID ${procUid} does not match current user UID ${currentUid}`);
    }
    const cmd = commParts.join(' ');
    if (fs.realpathSync(cmd) !== expected) {
      throw Error('Debugger owner is not installed ChatGPT: ' + cmd);
    }
  }
  return ids;
}

export class Connection {
  constructor(url, expectedPort) {
    const u = new URL(url);
    if (u.protocol !== 'ws:' || u.hostname !== '127.0.0.1') {
      throw Error('Only loopback debugger endpoints are accepted');
    }
    if (expectedPort !== undefined && Number(u.port) !== Number(expectedPort)) {
      throw Error(`Debugger endpoint port ${u.port} does not match expected port ${expectedPort}`);
    }
    this.ws = new WebSocket(url);
    this.n = 0;
    this.pending = new Map();
    this.listeners = new Set();
    this.ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(Error('Debugger connection timed out'));
        this.ws.close();
      }, 5000);
      this.ws.onopen = () => {
        clearTimeout(timer);
        resolve();
      };
      this.ws.onerror = () => {
        clearTimeout(timer);
        reject(Error('Debugger connection failed'));
      };
      this.ws.onclose = () => {
        clearTimeout(timer);
        reject(Error('Debugger closed'));
        for (const p of this.pending.values()) {
          clearTimeout(p.timer);
          p.j(Error('Debugger closed'));
        }
        this.pending.clear();
      };
    });
    this.ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.id) {
        const p = this.pending.get(m.id);
        if (p) {
          clearTimeout(p.timer);
          this.pending.delete(m.id);
          m.error ? p.j(Error(m.error.message)) : p.r(m.result);
        }
      } else {
        for (const cb of this.listeners) cb(m);
      }
    };
  }
  async call(method, params = {}, timeoutMs = 10000) {
    await this.ready;
    if (this.ws.readyState !== WebSocket.OPEN) throw Error('Debugger closed');
    return new Promise((r, j) => {
      const id = ++this.n;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        j(Error(method + ' timed out'));
      }, timeoutMs);
      this.pending.set(id, {r, j, timer});
      try {
        this.ws.send(JSON.stringify({id, method, params}));
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        j(error);
      }
    });
  }
  async eval(expression, timeoutMs = 10000) {
    const r = await this.call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true}, timeoutMs);
    if (r.exceptionDetails) throw Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
  close() {
    this.ws.close();
  }
}

export function isThemeDocumentURL(url) {
  return typeof url === 'string' &&
    !url.includes('avatar-overlay') &&
    /^app:\/\/-\/(index|detached-window)\.html([?#].*)?$/.test(url);
}

export function isThemeTarget(t) {
  return t?.type === 'page' && isThemeDocumentURL(t.url);
}

export function shouldDiscoverTarget(t, tracked, retiring = false) {
  return isThemeTarget(t) ? (!tracked || retiring) : (tracked && !retiring);
}

export function documentExpression(payload) {
  return `(()=>{if(!(${isThemeDocumentURL.toString()})(location.href))return {installed:false};return (${payload})})()`;
}

export function documentSource(payload) {
  return documentExpression(`(()=>{const apply=()=>(${payload});if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',apply,{once:true})}else{return apply()}})()`);
}

export function configDigest() {
  return createHash('sha256').update(fs.readFileSync(path.join(root, 'settings.json'))).digest('hex');
}

// Replace both the current style and the future-document hook without restarting
// the application or its runtime connection. Registry entries track partial cleanup.
export async function reloadSessions({sessions, registry, persist}, payload, digest) {
  const errors = [];
  await Promise.all([...sessions].map(async ([id, session]) => {
    if (session.retiring || !session.applied || session.c.ws.readyState !== WebSocket.OPEN) return;
    let added;
    try {
      const previous = [...new Set([...(registry[id] || []), session.script].filter(Boolean))];
      added = (await session.c.call('Page.addScriptToEvaluateOnNewDocument', {source: documentSource(payload)})).identifier;
      if (sessions.get(id) !== session || session.retiring) {
        await session.c.call('Page.removeScriptToEvaluateOnNewDocument', {identifier: added});
        return;
      }
      registry[id] = [...previous, added];
      persist();
      session.script = added;
      const result = await session.c.eval(documentExpression(payload));
      if (result?.installed !== true) {
        session.applied = false;
        throw Error('Codex document changed during wallpaper reload');
      }
      if (sessions.get(id) !== session || session.retiring) return;
      session.configDigest = digest;
      for (const identifier of previous) {
        await session.c.call('Page.removeScriptToEvaluateOnNewDocument', {identifier});
        if (registry[id]) {
          registry[id] = registry[id].filter(v => v !== identifier);
          persist();
        }
      }
    } catch (e) {
      errors.push({target: id, error: e.message});
    }
  }));
  const loaded = [...sessions.values()].filter(s => s.applied && !s.retiring && s.configDigest === digest && s.c.ws.readyState === WebSocket.OPEN);
  return {reloaded: true, applied: loaded.some(s => s.main) && errors.length === 0, sessions: loaded.length, shellSessions: loaded.filter(s => s.main).length, configSha256: digest, errors};
}

export const SHELL_WAIT_MS = 90000, SHELL_EVAL_TIMEOUT_MS = 95000;

export function waitForShell(timeoutMs = SHELL_WAIT_MS) {
  return new Promise(resolve => {
    let observer, timer, settled = false;
    const ready = () => !!(document.querySelector('.app-shell-left-panel') && document.querySelector('[data-app-shell-main-surface]'));
    const finish = value => {
      if (settled) return;
      settled = true;
      observer?.disconnect();
      if (timer !== undefined) clearTimeout(timer);
      resolve(value);
    };
    if (ready()) {
      finish(true);
      return;
    }
    observer = new MutationObserver(() => {
      if (ready()) finish(true);
    });
    observer.observe(document, {childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'data-app-shell-main-surface']});
    timer = setTimeout(() => finish(false), timeoutMs);
    // Recheck after observation starts so a mount at the boundary is not missed.
    if (ready()) finish(true);
  });
}

export function shellReadyExpression() {
  return `(${waitForShell.toString()})(${SHELL_WAIT_MS})`;
}

// A surviving target that leaves the app must lose its future-document hook
// before its connection is discarded. Keep it tracked until cleanup completes.
export function retireSession(id, session, {sessions, registry, persist}) {
  if (session.retiring) return session.retiring;
  session.applied = false;
  const identifiers = [...new Set([...(registry[id] || []), session.script].filter(Boolean))];
  session.retiring = Promise.resolve().then(async () => {
    const removed = new Set();
    try {
      for (const identifier of identifiers) {
        if (sessions.get(id) !== session) break;
        try {
          await session.c.call('Page.removeScriptToEvaluateOnNewDocument', {identifier});
          removed.add(identifier);
        } catch (e) {
          console.error('Pending hook retained for cleanup: ' + id + ' (' + e.message + ')');
        }
      }
      if (sessions.get(id) === session) await session.c.eval(removeExpression).catch(() => {});
    } finally {
      session.c.close();
      if (sessions.get(id) === session) sessions.delete(id);
      if (registry[id]) {
        registry[id] = registry[id].filter(identifier => !removed.has(identifier));
        if (!registry[id].length) delete registry[id];
      }
      persist();
    }
  });
  return session.retiring;
}

// Keep target bursts to one in-flight inventory and at most one queued refresh.
// Ownership is still checked for every inventory that actually runs.
export function createDiscoveryScheduler(run) {
  let current = null, queued = false;
  return () => {
    queued = true;
    if (!current) current = Promise.resolve().then(async () => {
      try {
        while (queued) {
          queued = false;
          await run();
        }
      } finally {
        current = null;
      }
    });
    return current;
  };
}

export async function pages(port, expectedPid) {
  verifyOwner(port, expectedPid);
  return (await (await fetch('http://127.0.0.1:' + port + '/json/list', {signal: AbortSignal.timeout(3000)})).json()).filter(isThemeTarget);
}

export async function applyOnce(port, expectedPid) {
  const results = [];
  for (const t of await pages(port, expectedPid)) {
    const c = new Connection(t.webSocketDebuggerUrl, port);
    try {
      results.push(await c.eval(documentExpression(expression())));
    } finally {
      c.close();
    }
  }
  return results;
}

const isCLI = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
const command = isCLI ? (process.argv[2] || 'daemon') : null;
const port = Number(process.argv[3] || 9236);
const socketPath = path.join(state, 'control.sock');
const registryPath = path.join(state, 'registrations.json');

if (command === 'once') {
  ensureStateDir();
  console.log(JSON.stringify(await applyOnce(port)));
}

if (command === 'once-remove') {
  ensureStateDir();
  const registry = fs.existsSync(registryPath) ? JSON.parse(fs.readFileSync(registryPath, 'utf8')) : {};
  const errors = [];
  try {
    for (const t of await pages(port)) {
      const c = new Connection(t.webSocketDebuggerUrl, port);
      try {
        for (const id of [...(registry[t.id] || [])]) {
          try {
            await c.call('Page.removeScriptToEvaluateOnNewDocument', {identifier: id});
            if (registry[t.id]) {
              registry[t.id] = registry[t.id].filter(v => v !== id);
              if (!registry[t.id].length) delete registry[t.id];
            }
          } catch (err) {
            errors.push({target: t.id, hook: id, error: err.message});
          }
        }
        await c.eval(removeExpression).catch(() => {});
      } finally {
        c.close();
      }
    }
  } catch (err) {
    errors.push({target: 'pages', error: err.message});
  }

  if (Object.keys(registry).length === 0) {
    fs.rmSync(registryPath, {force: true});
    console.log(JSON.stringify({removed: true}));
  } else {
    fs.writeFileSync(registryPath, JSON.stringify(registry));
    console.log(JSON.stringify({removed: false, errors, remaining: registry}));
  }
}

if (command === 'daemon') {
  ensureStateDir();
  verifyOwner(port);
  const info = await (await fetch('http://127.0.0.1:' + port + '/json/version')).json();
  const browser = new Connection(info.webSocketDebuggerUrl, port);
  await browser.ready;

  const sessions = new Map();
  const registry = fs.existsSync(registryPath) ? JSON.parse(fs.readFileSync(registryPath, 'utf8')) : {};
  let stopping = false;
  let browserDead = false;

  const persistRegistry = () => {
    if (Object.keys(registry).length === 0) {
      fs.rmSync(registryPath, {force: true});
    } else {
      fs.writeFileSync(registryPath, JSON.stringify(registry));
    }
  };

  const inject = async t => {
    if (stopping || sessions.has(t.id) || !isThemeTarget(t)) return;
    const c = new Connection(t.webSocketDebuggerUrl, port);
    const session = {c, script: null, applied: false, main: t.url.includes('/index.html')};
    sessions.set(t.id, session);
    try {
      if (session.main) {
        const compatible = await c.eval(shellReadyExpression(), SHELL_EVAL_TIMEOUT_MS);
        if (!compatible) throw Error('Codex shell selectors changed; inspect this updated build before applying.');
      }
      if (stopping || sessions.get(t.id) !== session) {
        c.close();
        return;
      }
      await c.call('Page.enable');
      // Retry any hook whose earlier navigation cleanup failed before adding another.
      const previous = registry[t.id] || [];
      for (const identifier of previous) {
        if (stopping || sessions.get(t.id) !== session) {
          c.close();
          return;
        }
        await c.call('Page.removeScriptToEvaluateOnNewDocument', {identifier});
      }
      if (registry[t.id]) {
        registry[t.id] = registry[t.id].filter(identifier => !previous.includes(identifier));
        if (!registry[t.id].length) delete registry[t.id];
        persistRegistry();
      }
      if (stopping || sessions.get(t.id) !== session) {
        c.close();
        return;
      }
      const payload = expression(), digest = configDigest();
      const r = await c.call('Page.addScriptToEvaluateOnNewDocument', {source: documentSource(payload)});
      if (stopping || sessions.get(t.id) !== session || session.retiring) {
        await c.call('Page.removeScriptToEvaluateOnNewDocument', {identifier: r.identifier}).catch(() => {});
        c.close();
        return;
      }
      session.script = r.identifier;
      registry[t.id] = [r.identifier];
      persistRegistry();
      const result = await c.eval(documentExpression(payload));
      if (sessions.get(t.id) === session && !session.retiring) {
        session.applied = result?.installed === true;
        session.configDigest = digest;
      }
    } catch (e) {
      if (session.script) {
        try {
          await c.call('Page.removeScriptToEvaluateOnNewDocument', {identifier: session.script});
          registry[t.id] = (registry[t.id] || []).filter(id => id !== session.script);
          if (!registry[t.id].length) delete registry[t.id];
          persistRegistry();
        } catch {
          console.error('Pending hook retained for cleanup: ' + t.id);
        }
      }
      if (sessions.get(t.id) === session) sessions.delete(t.id);
      c.close();
      console.error(e.message);
    }
  };

  const forget = id => {
    if (!sessions.has(id) && !registry[id]) return;
    const session = sessions.get(id);
    sessions.delete(id);
    delete registry[id];
    session?.c.close();
    persistRegistry();
  };

  const discover = createDiscoveryScheduler(async () => {
    if (stopping) return;
    const current = await pages(port);
    if (stopping) return;
    const ids = new Set(current.map(t => t.id));
    await Promise.all([...sessions].filter(([id]) => !ids.has(id)).map(([id, session]) => retireSession(id, session, {sessions, registry, persist: persistRegistry})));
    if (stopping) return;
    await Promise.all(current.map(inject));
  });

  browser.listeners.add(m => {
    if (stopping) return;
    if (m.method === 'Target.targetDestroyed') {
      forget(m.params.targetId);
      return;
    }
    if (m.method === 'Target.targetCreated' || m.method === 'Target.targetInfoChanged') {
      const target = m.params.targetInfo;
      if (shouldDiscoverTarget(target, sessions.has(target.targetId), !!sessions.get(target.targetId)?.retiring)) {
        discover().catch(e => console.error(e.message));
      }
    }
  });

  const remove = async () => {
    stopping = true;
    const errors = [];
    for (const [id, session] of sessions.entries()) {
      if (session.retiring) {
        await session.retiring.catch(() => {});
        continue;
      }
      const {c, script} = session;
      try {
        if (script) {
          try {
            await c.call('Page.removeScriptToEvaluateOnNewDocument', {identifier: script});
            if (registry[id]) {
              registry[id] = registry[id].filter(v => v !== script);
              if (!registry[id].length) delete registry[id];
              persistRegistry();
            }
          } catch (err) {
            errors.push({target: id, hook: script, error: err.message});
          }
        }
        await c.eval(removeExpression).catch(() => {});
      } catch {}
      c.close();
    }
    sessions.clear();

    if (!browserDead) {
      try {
        for (const t of await pages(port)) {
          if (!registry[t.id] || !registry[t.id].length) continue;
          const c = new Connection(t.webSocketDebuggerUrl, port);
          try {
            for (const id of [...registry[t.id]]) {
              try {
                await c.call('Page.removeScriptToEvaluateOnNewDocument', {identifier: id});
                registry[t.id] = registry[t.id].filter(v => v !== id);
                if (!registry[t.id].length) delete registry[t.id];
                persistRegistry();
              } catch (err) {
                errors.push({target: t.id, hook: id, error: err.message});
              }
            }
            await c.eval(removeExpression).catch(() => {});
          } finally {
            c.close();
          }
        }
      } catch (e) {
        if (!browserDead) console.error(e.message);
      }
    }

    persistRegistry();
    browser.close();
    return {
      removed: Object.keys(registry).length === 0,
      errors,
      remaining: registry
    };
  };

  try {
    fs.unlinkSync(socketPath);
  } catch (e) {
    if (e.code !== 'ENOENT') throw e;
  }

  let reloadPending = Promise.resolve();
  const server = net.createServer(s => {
    s.on('error', () => {});
    s.once('data', async b => {
      try {
        const cmd = b.toString().trim();
        if (cmd === 'remove') {
          await reloadPending;
          const result = await remove();
          s.end(JSON.stringify(result));
          server.close();
          fs.rmSync(socketPath, {force: true});
          fs.rmSync(path.join(state, 'daemon.json'), {force: true});
        } else if (cmd === 'reload') {
          const reload = reloadPending.then(async () => {
            if (stopping || browserDead) throw Error('Codex is closing');
            const payload = expression();
            return reloadSessions({sessions, registry, persist: persistRegistry}, payload, configDigest());
          });
          reloadPending = reload.catch(() => {});
          s.end(JSON.stringify(await reload));
        } else if (cmd === 'status') {
          const loaded = [...sessions.values()].filter(v => v.applied && !v.retiring && v.c.ws.readyState === WebSocket.OPEN);
          const main = loaded.filter(v => v.main);
          const digests = [...new Set(main.map(v => v.configDigest))];
          s.end(JSON.stringify({port, sessions: loaded.length, shellSessions: main.length, browserDead, appliedConfigSha256: digests.length === 1 ? digests[0] : null}));
        } else {
          s.end(JSON.stringify({error: 'Unknown command'}));
        }
      } catch (e) {
        if (!s.writableEnded) s.end(JSON.stringify({error: e.message}));
      }
    });
  });

  server.listen(socketPath, () => fs.chmodSync(socketPath, 0o600));
  fs.writeFileSync(path.join(state, 'daemon.json'), JSON.stringify({pid: process.pid, port, started: new Date().toISOString()}));

  const quit = async () => {
    if (stopping) return;
    stopping = true;
    await remove();
    server.close();
    fs.rmSync(socketPath, {force: true});
    fs.rmSync(path.join(state, 'daemon.json'), {force: true});
    if (browserDead) {
      fs.writeFileSync(path.join(state, 'autostart-status.json'), JSON.stringify({active: false, needsRestart: false, message: '应用已退出；下次从主题入口启动时自动加载。'}));
    }
    process.exit(0);
  };

  process.on('SIGTERM', quit);
  process.on('SIGINT', quit);
  browser.ws.addEventListener('close', () => {
    browserDead = true;
    if (!stopping) quit();
  });

  if (!server.listening) {
    await new Promise((resolve, reject) => {
      server.once('listening', resolve);
      server.once('error', reject);
    });
  }
  if (!stopping) {
    await browser.call('Target.setDiscoverTargets', {discover: true});
    if (!stopping) await discover();
  }
}

if (command === 'control') {
  const cmd = process.argv[3] || 'status';
  const response = await new Promise((r, j) => {
    const s = net.connect(socketPath, () => s.write(cmd));
    let out = '';
    s.on('data', b => out += b);
    s.on('end', () => r(out));
    s.on('error', j);
  });
  console.log(response);
}
