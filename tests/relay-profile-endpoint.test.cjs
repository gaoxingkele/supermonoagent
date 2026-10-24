const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const C = require('../src/supermonoagent/static/terminal-client.js');
const source = fs.readFileSync(require.resolve('../src/supermonoagent/static/workspace.js'), 'utf8');
const body = source.slice(source.indexOf('  function profileEndpoint('), source.indexOf('  function renderRelayProfiles('));
const endpoint = vm.runInNewContext(body + '\nprofileEndpoint', {URL, C, location: {origin: 'http://127.0.0.1'}});

test('relay address fields preserve embedded ports, paths and IPv6 identities', () => {
  for (const [address, port, expected] of [
    ['example.test', '443', 'wss://example.test/ws'],
    ['https://example.test:8443/proxy/ws', '443', 'wss://example.test:8443/proxy/ws'],
    ['https://example.test:443/ws', '8443', 'wss://example.test/ws'],
    ['example.test:8443', '443', 'wss://example.test:8443/ws'],
    ['127.0.0.1', '6769', 'ws://127.0.0.1:6769/ws'],
    ['::1', '6769', 'ws://[::1]:6769/ws'],
    ['2001:db8::1', '8443', 'wss://[2001:db8::1]:8443/ws'],
    ['[2001:db8::1]:9443', '443', 'wss://[2001:db8::1]:9443/ws'],
    ['117.50.48.22:8817', '443', 'wss://117.50.48.22/ws'],
  ]) assert.equal(endpoint(address, port), expected);
});

test('invalid relay forms cannot smuggle URL credentials or insecure public endpoints', () => {
  for (const [address, port] of [
    ['', '443'], ['example.test', '0'], ['example.test', '65536'],
    ['https://user:secret@example.test', '443'], ['http://example.test', '80'],
  ]) assert.throws(() => endpoint(address, port));
});
