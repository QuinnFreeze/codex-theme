import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {isThemeDocumentURL, documentExpression, Connection} from '../theme/scripts/runtime.mjs';

test('isThemeDocumentURL only matches strictly valid index and detached-window URLs', () => {
  // Valid URLs
  assert.equal(isThemeDocumentURL('app://-/index.html'), true);
  assert.equal(isThemeDocumentURL('app://-/detached-window.html'), true);
  assert.equal(isThemeDocumentURL('app://-/index.html?token=123'), true);
  assert.equal(isThemeDocumentURL('app://-/index.html#title'), true);

  // Invalid URLs / boundary attacks
  assert.equal(isThemeDocumentURL('app://-/index.html.evil'), false);
  assert.equal(isThemeDocumentURL('app://-/index.html/subpath'), false);
  assert.equal(isThemeDocumentURL('app://-/detached-window.html.js'), false);
  assert.equal(isThemeDocumentURL('app://-/avatar-overlay/index.html'), false);
  assert.equal(isThemeDocumentURL('app://-/settings.html'), false);
  assert.equal(isThemeDocumentURL('https://example.com/app://-/index.html'), false);
  assert.equal(isThemeDocumentURL('http://127.0.0.1:9236/index.html'), false);
  assert.equal(isThemeDocumentURL(''), false);
  assert.equal(isThemeDocumentURL(null), false);
  assert.equal(isThemeDocumentURL(undefined), false);
});

test('Connection rejects non-loopback endpoints and port mismatches', () => {
  // Non-loopback
  assert.throws(() => new Connection('ws://192.168.1.1:9236/devtools'), /loopback/);
  assert.throws(() => new Connection('http://127.0.0.1:9236/devtools'), /loopback/);
  assert.throws(() => new Connection('ws://evil.com:9236/devtools'), /loopback/);

  // Port mismatch
  assert.throws(() => new Connection('ws://127.0.0.1:9999/devtools', 9236), /port/);
});

test('documentExpression blocks execution if URL has navigated to an unauthorized document', () => {
  const code = documentExpression('(()=>{ return {applied: true}; })()');

  // Authorized
  const validContext = {location: {href: 'app://-/index.html'}};
  const validResult = vm.runInNewContext(code, validContext);
  assert.equal(validResult.applied, true);

  // Unauthorized (e.g. navigated to external site)
  const invalidContext = {location: {href: 'https://attacker.com/index.html'}};
  const invalidResult = vm.runInNewContext(code, invalidContext);
  assert.equal(invalidResult.installed, false);

  // Boundary bypass attempt
  const bypassContext = {location: {href: 'app://-/index.html.evil'}};
  const bypassResult = vm.runInNewContext(code, bypassContext);
  assert.equal(bypassResult.installed, false);
});
