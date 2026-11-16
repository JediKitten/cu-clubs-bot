import test from 'node:test';
import assert from 'node:assert/strict';
import { projectWeek, monthCells } from './projection.mjs';

const event = (id, starts_at, patch = {}) => ({ id, version: 1, visibility: 'open', status: 'published', starts_at, ends_at: new Date(Date.parse(starts_at) + 3600000).toISOString(), clubs: [{ id: 'cinema', name: 'Киноклуб' }], title: 'Встреча', room: 'Кинозал', ...patch });
const snapshot = events => ({ schema_version: 1, timezone: 'Europe/Moscow', week_start: '2026-11-09', events });

test('sorts Moscow time, preserves separate meetings of one club and dedupes collaboration by event ID', () => {
  const collaboration = event('collab', '2026-11-13T20:00:00+03:00', { clubs: [{ id: 'cinema', name: 'Киноклуб' }, { id: 'literature', name: 'Литературный клуб' }] });
  const input = snapshot([collaboration, event('late', '2026-11-13T19:00:00+03:00'), event('early', '2026-11-13T18:23:00+03:00'), structuredClone(collaboration)]);
  const original = JSON.stringify(input);
  const output = projectWeek(input);
  assert.equal(output.event_count, 3);
  assert.deepEqual(output.days[4].rows.map(r => r.id), ['early', 'late', 'collab']);
  assert.equal(output.days[4].count, 3);
  assert.equal(output.days[4].rows[0].start_time, '18.23');
  assert.equal(output.days[4].rows[2].club_label, 'Киноклуб х Литературный клуб');
  assert.equal(JSON.stringify(input), original);
});
test('uses Moscow date for UTC timestamps on week boundaries', () => {
  const result = projectWeek(snapshot([event('monday', '2026-11-08T22:00:00Z'), event('sunday', '2026-11-15T20:30:00Z')]));
  assert.equal(result.days[0].rows[0].start_time, '01.00');
  assert.equal(result.days[6].rows[0].end_date, '2026-11-16');
  assert.equal(result.days[6].count, 1);
  assert.throws(() => projectWeek(snapshot([event('next', '2026-11-15T21:00:00Z')])), /пределами/);
});
test('requires a consistent snapshot, never silently chooses a conflicting event version', () => {
  const first = event('same', '2026-11-09T10:00:00+03:00');
  assert.throws(() => projectWeek(snapshot([first, { ...first, version: 2 }])) , /разные версии/);
});
test('rejects invalid dates and times, timezone-free input and non-Monday weeks', () => {
  assert.throws(() => projectWeek(snapshot([event('bad', '2026-11-09T10:00:00Z', { starts_at: '2026-02-30T10:00:00Z' })])), /дата/);
  assert.throws(() => projectWeek(snapshot([event('bad', '2026-11-09T10:00:00Z', { starts_at: '2026-11-09T25:00:00Z' })])), /время/);
  assert.throws(() => projectWeek(snapshot([event('bad', '2026-11-09T10:00:00Z', { starts_at: '2026-11-09T10:00:00' })])), /пояс/);
  assert.throws(() => projectWeek({ ...snapshot([]), week_start: '2026-11-10' }), /понедельника/);
});
test('does not project closed, canceled or unpublished meetings', () => {
  for (const patch of [{ visibility: 'closed' }, { status: 'draft' }, { status: 'cancelled' }, { status: 'pending_partner' }]) {
    assert.throws(() => projectWeek(snapshot([event('private', '2026-11-09T10:00:00Z', patch)])), /непубличную/);
  }
});
test('only public display fields reach output; long text is preserved for layout validation', () => {
  const result = projectWeek(snapshot([event('one', '2026-11-09T10:00:00Z', { description: 'длинное описание\n'.repeat(200), participants: ['PRIVATE'], post_registration_text: 'PRIVATE', oauth_token: 'PRIVATE' })]));
  assert.ok(!JSON.stringify(result).includes('PRIVATE'));
  assert.equal(result.days[0].rows[0].description.length, 'длинное описание\n'.repeat(200).length);
});
test('calendar covers Sunday-start November and year transition without repeated date keys', () => {
  const november = monthCells(2026, 11);
  assert.equal(november.length, 42);
  assert.equal(november[0].date, '2026-10-26');
  assert.equal(november.filter(c => c.in_month).length, 30);
  assert.equal(new Set(november.map(c => c.date)).size, 42);
  const december = monthCells(2026, 12);
  assert.equal(december.at(-1).date, '2027-01-03');
});
test('preserves leap day and zero-event days', () => {
  assert.equal(monthCells(2028, 2).filter(c => c.in_month).length, 29);
  assert.equal(projectWeek(snapshot([])).days.length, 7);
  assert.ok(projectWeek(snapshot([])).days.every(d => d.count === 0));
});

test('accepts optional empty room from real bot export, rejects invalid room data', () => {
  for (const room of ['', '   ', null, undefined]) {
    const result = projectWeek(snapshot([event('optional', '2026-11-09T10:00:00Z', { room })]));
    assert.equal(result.days[0].rows[0].room, '');
  }
  for (const room of [42, {}, 'x'.repeat(1001)]) {
    assert.throws(() => projectWeek(snapshot([event('bad-room', '2026-11-09T10:00:00Z', { room })])), /место/);
  }
});
