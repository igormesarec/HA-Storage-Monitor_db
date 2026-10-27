import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { JSDOM, VirtualConsole } from 'jsdom';

const cardPath = fileURLToPath(new URL(
  '../../custom_components/ha_storage_monitor/frontend/storage-monitor-card.js',
  import.meta.url,
));
const cardSource = readFileSync(cardPath, 'utf8');

const summary = {
  samples: [{
    source: 'disk_free',
    size: 20 * 1024 ** 3,
    status: 'ok',
    method: 'disk_usage',
    measured_at: 1_799_424_000,
  }],
  alerts: { disk_low: { active: 0 } },
  growth: {},
  writers: { status: 'disabled', reason: 'Recorder writer diagnostics disabled' },
  changes: { disk_free: { delta6h: 0, delta24h: 0 } },
  now: 1_799_424_000,
  scope: 'Test scope',
  writer_diagnostics_enabled: false,
};

test('card constructs through the Custom Elements registry and survives lifecycle', async () => {
  const runtimeErrors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => runtimeErrors.push(error));
  const dom = new JSDOM('<!doctype html><body></body>', {
    runScripts: 'outside-only',
    url: 'http://homeassistant.local/',
    virtualConsole,
  });
  const { window } = dom;
  const intervals = [];
  const clearedIntervals = [];
  window.setInterval = (callback, milliseconds) => {
    const id = intervals.length + 1;
    intervals.push({ id, callback, milliseconds });
    return id;
  };
  window.clearInterval = id => clearedIntervals.push(id);

  window.eval(cardSource);
  assert.equal(
    typeof window.customElements.get('ha-storage-monitor-card'),
    'function',
    'the Lovelace card must register',
  );

  const card = window.document.createElement('ha-storage-monitor-card');
  assert.equal(card.localName, 'ha-storage-monitor-card');
  assert.equal(typeof card.setConfig, 'function');
  assert.equal(card.attributes.length, 0, 'constructor must not create attributes');
  assert.ok(card.shadowRoot, 'constructor must attach the render root');

  card.setConfig({ language: 'sl', sql_entity: 'sensor.existing_size' });
  assert.equal(card._language, 'sl');
  assert.equal(card._config.sql_entity, 'sensor.existing_size');
  assert.equal(card.hasAttribute('lang'), false);

  const websocketCalls = [];
  card.hass = {
    states: {},
    callWS: async message => {
      websocketCalls.push(message);
      return message.type === 'ha_storage_monitor/summary'
        ? summary
        : { source: message.source, samples: [] };
    },
  };
  await card.reload();
  assert.equal(card._started, true);
  assert.ok(websocketCalls.some(call => call.type === 'ha_storage_monitor/summary'));
  assert.ok(websocketCalls.some(call => call.type === 'ha_storage_monitor/history'));
  assert.match(card.shadowRoot.textContent, /Nadzor prostora podatkovnih baz/);
  assert.match(card.shadowRoot.textContent, /20\.00 GiB/);
  const tabs = [...card.shadowRoot.querySelectorAll('[data-tab]')];
  assert.deepEqual(tabs.map(tab => tab.dataset.tab), [
    'overview', 'databases', 'charts', 'writers', 'disk', 'alerts', 'settings',
  ]);
  tabs[1].click();
  assert.equal(card._tab, 'databases');

  window.document.body.append(card);
  assert.deepEqual(intervals.map(item => item.milliseconds), [300_000]);

  card._tab = 'settings';
  card.render();
  const languageSelect = card.shadowRoot.querySelector('#language');
  assert.ok(languageSelect);
  languageSelect.value = 'en';
  languageSelect.dispatchEvent(new window.Event('change'));
  assert.equal(card._language, 'en');
  assert.match(card.shadowRoot.textContent, /Home Assistant Storage Monitor/);

  card.remove();
  assert.deepEqual(clearedIntervals, [1]);
  window.document.body.append(card);
  assert.deepEqual(intervals.map(item => item.milliseconds), [300_000, 300_000]);
  card.remove();
  assert.deepEqual(clearedIntervals, [1, 2]);

  assert.deepEqual(runtimeErrors, [], 'custom-element construction must be clean');
  dom.window.close();
});
