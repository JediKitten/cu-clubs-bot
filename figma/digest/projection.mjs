/** Draft shared data core. No network, credentials, Figma writes, or selection policy. */
const DAY_MS = 86400000;
const MOSCOW_OFFSET = 180 * 60000;
const WEEKDAYS = ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'];
const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

function fail(message) { throw new Error(message); }
function dateParts(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) fail('Ожидается дата ГГГГ-ММ-ДД.');
  const [y, m, d] = value.split('-').map(Number);
  const epoch = Date.UTC(y, m - 1, d);
  if (y < 2000 || y > 2199 || new Date(epoch).toISOString().slice(0, 10) !== value) fail('Некорректная дата.');
  return { y, m, d, epoch };
}
function instant(value) {
  const m = typeof value === 'string' && /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,3})?)?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!m) fail('У времени должен быть указан часовой пояс.');
  dateParts(m[1]);
  if (+m[2] > 23 || +m[3] > 59 || +(m[4] || 0) > 59) fail('Некорректное время.');
  if (m[5] !== 'Z') {
    const [h, min] = m[5].slice(1).split(':').map(Number);
    if (min > 59 || h > 14 || (h === 14 && min !== 0)) fail('Некорректный часовой пояс.');
  }
  const epoch = Date.parse(value);
  if (!Number.isFinite(epoch)) fail('Некорректное время.');
  return epoch;
}
function requiredText(value, limit, label) {
  if (typeof value !== 'string' || !value.trim() || value.length > limit) fail(`Некорректное поле: ${label}.`);
  return value.trim();
}
function localParts(epoch) {
  const shifted = new Date(epoch + MOSCOW_OFFSET);
  return {
    date: shifted.toISOString().slice(0, 10),
    time: shifted.toISOString().slice(11, 16).replace(':', '.'),
    label: `${shifted.getUTCDate()} ${MONTHS[shifted.getUTCMonth()]} (${WEEKDAYS[shifted.getUTCDay()]})`,
  };
}
function safeUrl(value) {
  // Keep the core usable in the Figma sandbox without DOM/URL/Intl globals.
  if (value == null || value === '') return null;
  if (typeof value !== 'string' || value.length > 2048 || !/^https:\/\/[A-Za-z0-9.-]+(?::443)?(?:[/?#][^\s]*)?$/.test(value) || /[<>"\u0000-\u001f]/.test(value)) fail('Некорректная публичная ссылка.');
  return value;
}
function normalizeEvent(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) fail('Некорректная встреча.');
  if (raw.visibility !== 'open' || !['published', 'held'].includes(raw.status)) fail('Подборка содержит непубличную или неопубликованную встречу.');
  const id = requiredText(raw.id, 80, 'идентификатор');
  if (!Number.isInteger(raw.version) || raw.version < 1) fail('Некорректная версия встречи.');
  if (!Array.isArray(raw.clubs) || raw.clubs.length < 1 || raw.clubs.length > 30) fail('Не указаны клубы встречи.');
  const clubs = raw.clubs.map(c => ({ id: requiredText(c.id, 80, 'идентификатор клуба'), name: requiredText(c.name, 200, 'название клуба') }));
  if (new Set(clubs.map(c => c.id)).size !== clubs.length) fail('Клуб повторяется в одной встрече.');
  const start = instant(raw.starts_at), end = instant(raw.ends_at);
  if (end <= start) fail('Конец встречи должен быть позже начала.');
  const starts = localParts(start), ends = localParts(end);
  const title = requiredText(raw.title, 1000, 'название встречи');
  if (raw.description != null && (typeof raw.description !== 'string' || raw.description.length > 30000)) fail('Некорректное описание.');
  const room = raw.room == null ? '' : requiredText(raw.room, 1000, 'место');
  // Deliberately project only public display fields, never copy arbitrary payload keys.
  return { id, version: raw.version, clubs, title, description: raw.description || '', room,
    public_url: safeUrl(raw.public_url), starts_at: new Date(start).toISOString(), ends_at: new Date(end).toISOString(),
    date: starts.date, date_label: starts.label, start_time: starts.time, end_time: ends.time,
    end_date: ends.date, club_label: clubs.map(c => c.name).join(' х '), start_epoch: start };
}

export function projectWeek(snapshot) {
  if (!snapshot || snapshot.schema_version !== 1 || snapshot.timezone !== 'Europe/Moscow') fail('Неподдерживаемый формат подборки.');
  const week = dateParts(snapshot.week_start);
  if (new Date(week.epoch).getUTCDay() !== 1) fail('Неделя должна начинаться с понедельника.');
  if (!Array.isArray(snapshot.events) || snapshot.events.length > 500) fail('В подборке может быть не больше 500 встреч.');
  const byId = new Map();
  for (const raw of snapshot.events) {
    const event = normalizeEvent(raw);
    const day = (dateParts(event.date).epoch - week.epoch) / DAY_MS;
    if (day < 0 || day >= 7) fail('Встреча находится за пределами выбранной недели.');
    const previous = byId.get(event.id);
    if (previous && JSON.stringify(previous) !== JSON.stringify(event)) fail('В подборке разные версии одной встречи. Обнови данные.');
    byId.set(event.id, event);
  }
  const events = [...byId.values()].sort((a, b) => a.start_epoch - b.start_epoch || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const days = Array.from({ length: 7 }, (_, i) => {
    const date = new Date(week.epoch + i * DAY_MS).toISOString().slice(0, 10);
    const rows = events.filter(event => event.date === date).map(({ start_epoch, ...event }) => event);
    return { date, count: rows.length, rows };
  });
  return { schema_version: 1, timezone: snapshot.timezone, week_start: snapshot.week_start, event_count: events.length, days };
}

/** Mathematical calendar only; applying/changing the existing template remains a UI decision. */
export function monthCells(year, month) {
  if (!Number.isInteger(year) || year < 2000 || year > 2199 || !Number.isInteger(month) || month < 1 || month > 12) fail('Некорректный месяц.');
  const first = Date.UTC(year, month - 1, 1);
  const skip = (new Date(first).getUTCDay() + 6) % 7;
  const length = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const weeks = Math.max(5, Math.ceil((skip + length) / 7));
  return Array.from({ length: weeks * 7 }, (_, i) => {
    const date = new Date(first + (i - skip) * DAY_MS);
    return { date: date.toISOString().slice(0, 10), day: date.getUTCDate(), in_month: date.getUTCMonth() + 1 === month, column: i % 7, row: Math.floor(i / 7) };
  });
}
