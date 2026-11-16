import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

test('a valid preview removes the custom button disabled attribute, then invalidates on selection change', () => {
  const html = readFileSync(new URL('./ui.html', import.meta.url), 'utf8');
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', attrs: new Set(id === 'run' ? ['disabled'] : []), listeners: {},
      addEventListener(type, fn) { this.listeners[type] = fn; },
      toggleAttribute(name, value) { if (value) this.attrs.add(name); else this.attrs.delete(name); },
    });
    return elements.get(id);
  };
  const messages = [], handlers = {};
  vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], {
    document: { getElementById: element },
    parent: { postMessage: value => messages.push(value.pluginMessage) },
    window: { addEventListener: (type, fn) => { handlers[type] = fn; } },
    localStorage: { getItem() { return null; } },
  });
  const receive = value => handlers.message({ data: { pluginMessage: value } });
  receive({ type: 'selection', ok: true, message: '6 pages' });
  element('data').value = '{}';
  element('data').listeners.change();
  assert.equal(element('check').attrs.has('disabled'), false);
  assert.equal(element('run').attrs.has('disabled'), true);
  element('check').listeners.click();
  assert.equal(element('check').attrs.has('disabled'), true);
  assert.equal(messages.at(-1).type, 'preview');
  receive({ type: 'preview', events: 2, other: 11, days: [], mismatches: 0, update: false });
  assert.equal(element('run').attrs.has('disabled'), false);
  assert.equal(element('check').attrs.has('disabled'), false);
  element('font').value = 'Inter';
  element('run').listeners.click();
  assert.equal(messages.at(-1).type, 'generate');
  assert.equal(messages.at(-1).options.font, 'Inter');
  receive({ type: 'success', pages: 2, events: 2 });
  assert.equal(element('run').attrs.has('disabled'), true);
  receive({ type: 'selection', ok: false, message: 'Select a source' });
  assert.equal(element('check').attrs.has('disabled'), true);
});
