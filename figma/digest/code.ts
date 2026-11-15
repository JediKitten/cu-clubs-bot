type ClubInput = {id:string;name:string};
type EventInput = {id:string;version:number;visibility:string;status:string;starts_at:string;ends_at:string;clubs:ClubInput[];title:string;description?:string;room?:string|null;public_url?:string|null};
type Snapshot = {schema_version:number;timezone:string;week_start:string;events:EventInput[]};
type Normalized = {id:string;version:number;clubs:ClubInput[];title:string;description:string;room:string;public_url:string|null;starts_at:string;ends_at:string;date:string;date_label:string;start_time:string;end_time:string;end_date:string;club_label:string;start_epoch:number};
type DisplayEvent = Omit<Normalized,'start_epoch'>;
type Projection = {schema_version:number;timezone:string;week_start:string;event_count:number;days:{date:string;count:number;rows:DisplayEvent[]}[]};
type Source = {cover:FrameNode;pages:FrameNode[];replace:SectionNode|null};
type Point = {x:number;y:number};
type FoundRow = {node:FrameNode;page:FrameNode;dateText:string;day:number;month:number;club:boolean;point:Point};
type OtherRow = FoundRow & {date:string};
type Grid = {dates:({node:TextNode}&Point)[];xs:number[];ys:number[];dx:number;dy:number;dots:({node:EllipseNode}&Point)[]};
type Plan = {data:Projection;source:Source;rows:FoundRow[];grid:Grid;other:OtherRow[];mismatch:OtherRow[]};
type Options = {font?:string;keepOtherWeeks?:boolean;descriptions?:boolean};
type Item = {date:string;club:boolean;minutes:number;order:number;node?:FrameNode;event?:DisplayEvent};
type Message = {type:string;raw?:string;height?:number;options?:Options};
/** Draft shared data core. No network, credentials, Figma writes, or selection policy. */
const DAY_MS = 86400000;
const MOSCOW_OFFSET = 180 * 60000;
const WEEKDAYS = ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'];
const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

function fail(message:string):never { throw new Error(message); }
function dateParts(value:string) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) fail('Ожидается дата ГГГГ-ММ-ДД.');
  const [y, m, d] = value.split('-').map(Number);
  const epoch = Date.UTC(y, m - 1, d);
  if (y < 2000 || y > 2199 || new Date(epoch).toISOString().slice(0, 10) !== value) fail('Некорректная дата.');
  return { y, m, d, epoch };
}
function instant(value:string) {
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
function requiredText(value:string, limit:number, label:string) {
  if (typeof value !== 'string' || !value.trim() || value.length > limit) fail(`Некорректное поле: ${label}.`);
  return value.trim();
}
function localParts(epoch:number) {
  const shifted = new Date(epoch + MOSCOW_OFFSET);
  return {
    date: shifted.toISOString().slice(0, 10),
    time: shifted.toISOString().slice(11, 16).replace(':', '.'),
    label: `${shifted.getUTCDate()} ${MONTHS[shifted.getUTCMonth()]} (${WEEKDAYS[shifted.getUTCDay()]})`,
  };
}
function safeUrl(value:string|null|undefined):string|null {
  // Keep the core usable in the Figma sandbox without DOM/URL/Intl globals.
  if (value == null || value === '') return null;
  if (typeof value !== 'string' || value.length > 2048 || !/^https:\/\/[A-Za-z0-9.-]+(?::443)?(?:[/?#][^\s]*)?$/.test(value) || /[<>"]/.test(value) || [...value].some(c=>c.charCodeAt(0)<32)) fail('Некорректная публичная ссылка.');
  return value;
}
function normalizeEvent(raw:EventInput):Normalized {
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

function projectWeek(snapshot:Snapshot):Projection {
  if (!snapshot || snapshot.schema_version !== 1 || snapshot.timezone !== 'Europe/Moscow') fail('Неподдерживаемый формат подборки.');
  const week = dateParts(snapshot.week_start);
  if (new Date(week.epoch).getUTCDay() !== 1) fail('Неделя должна начинаться с понедельника.');
  if (!Array.isArray(snapshot.events) || snapshot.events.length > 500) fail('В подборке может быть не больше 500 встреч.');
  const byId = new Map<string,Normalized>();
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
    const rows = events.filter(event => event.date === date).map(({ start_epoch: _start_epoch, ...event }) => event);
    return { date, count: rows.length, rows };
  });
  return { schema_version: 1, timezone: snapshot.timezone, week_start: snapshot.week_start, event_count: events.length, days };
}

const OWNER = 'cu-clubs-digest-v1';
const ORANGE = { r: 1, g: 102 / 255, b: 44 / 255 };
const TEXT_COLOR = { r: 230 / 255, g: 230 / 255, b: 230 / 255 };
const MONTH_LABELS = ['ЯНВАРЬ','ФЕВРАЛЬ','МАРТ','АПРЕЛЬ','МАЙ','ИЮНЬ','ИЮЛЬ','АВГУСТ','СЕНТЯБРЬ','ОКТЯБРЬ','НОЯБРЬ','ДЕКАБРЬ'];
const COMMAND = 'dfa2caf2-10bd-4d25-a85e-900859efa279';
let busy = false;
let lastSelection = '';


function nodes(root:SceneNode):SceneNode[] {
  const result = [], queue = [root];
  for (let i = 0; i < queue.length; i++) {
    if (i > 25000) fail('Слишком большой макет. Выдели один недельный выпуск.');
    const n = queue[i];
    if (n.visible === false) continue;
    result.push(n);
    if ('children' in n) queue.push(...n.children);
  }
  return result;
}
function paintColor(n:SceneNode):RGB|null {
  if (!('fills' in n) || !Array.isArray(n.fills)) return null;
  const p = n.fills.find(p => p.type === 'SOLID' && p.visible !== false);
  return p ? p.color : null;
}
function orange(n:SceneNode) {
  const c = paintColor(n);
  return !!c && Math.abs(c.r - ORANGE.r) < .012 && Math.abs(c.g - ORANGE.g) < .012 && Math.abs(c.b - ORANGE.b) < .012;
}
function absoluteIn(node:SceneNode, frame:FrameNode):Point {
  const a = node.absoluteTransform, b = frame.absoluteTransform;
  const determinant = b[0][0] * b[1][1] - b[0][1] * b[1][0];
  if (!determinant) fail('Некорректное преобразование фрейма.');
  const dx = a[0][2] - b[0][2], dy = a[1][2] - b[1][2];
  return { x: (b[1][1] * dx - b[0][1] * dy) / determinant, y: (-b[1][0] * dx + b[0][0] * dy) / determinant };
}
function sourceFrames():Source {
  let selected = [...figma.currentPage.selection];
  let replace:SectionNode|null = null;
  if (selected.length === 1 && selected[0].type === 'SECTION') {
    const section = selected[0];
    if (section.getSharedPluginData('cuClubsDigest','owner') === OWNER) replace = section;
    selected = section.children.filter(n => n.type === 'FRAME');
  }
  if (selected.length < 2 || selected.length > 30 || selected.some(n => n.type !== 'FRAME' || Math.abs(n.width - 1200) > 2 || Math.abs(n.height - 1500) > 2)) {
    fail('Выдели обложку и страницы одного дайджеста 1200×1500 либо созданную плагином секцию.');
  }
  const covers = selected.filter(n => nodes(n).some(t => t.type === 'TEXT' && /события\s+недели/i.test(t.characters)));
  if (covers.length !== 1) fail('В выделении должна быть одна обложка «События недели».');
  const cover = covers[0] as FrameNode;
  const pages = (selected as FrameNode[]).filter(n => n !== cover).sort((a,b) => a.y - b.y || a.x - b.x);
  if ((selected as FrameNode[]).some(n => n.rotation && Math.abs(n.rotation) > .01)) fail('Убери поворот фреймов перед генерацией.');
  return { cover, pages, replace };
}
function readRows(pages:FrameNode[]):FoundRow[] {
  const rows:FoundRow[] = [], seen = new Set<string>();
  for (const page of pages) {
    for (const text of nodes(page)) {
      if (text.type !== 'TEXT' || !/^\d{1,2}\s+[а-яё]+\s*\((?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)\)$/i.test(text.characters.trim())) continue;
      let row:BaseNode|null = text.parent;
      while (row && row !== page && !(row.type === 'FRAME' && row.layoutMode === 'HORIZONTAL' && Math.abs(row.width - 1040) < 3 && row.children.length >= 2)) row = row.parent;
      if (!row || row === page || row.type !== 'FRAME' || seen.has(row.id)) continue;
      seen.add(row.id);
      const date = text.characters.trim(), parts = date.match(/^(\d+)\s+([^\s]+)/);
      const month = MONTHS.indexOf(parts![2].toLowerCase());
      if (month < 0) fail('Не удалось распознать месяц строки.');
      const point = absoluteIn(row, page);
      rows.push({ node: row, page, dateText: date, day: +parts![1], month: month + 1, club: orange(text), point });
    }
  }
  return rows;
}
function rowDate(row:FoundRow, weekStart:string) {
  const year = +weekStart.slice(0, 4), start = dateParts(weekStart).epoch;
  let best:{epoch:number;date:string}|null = null;
  for (const candidateYear of [year - 1, year, year + 1]) {
    const epoch = Date.UTC(candidateYear, row.month - 1, row.day);
    if (!best || Math.abs(epoch - start) < Math.abs(best.epoch - start)) best = { epoch, date: new Date(epoch).toISOString().slice(0, 10) };
  }
  if(!best)fail('Не распознана дата строки.');return best.date;
}
function calendar(cover:FrameNode):Grid {
  const dates = nodes(cover).filter((n):n is TextNode => n.type === 'TEXT' && /^\d{1,2}$/.test(n.characters.trim())).map(node => ({ node, ...absoluteIn(node,cover) })).filter(n=>n.y > 450);
  if (![35,42].includes(dates.length)) fail('Не найдена календарная сетка на 35 или 42 даты.');
  dates.sort((a,b) => Math.abs(a.y-b.y) > 12 ? a.y-b.y : a.x-b.x);
  const xs = dates.slice(0,7).map(d=>d.x), ys:number[] = [];
  for (let i=0;i<dates.length;i+=7) ys.push(dates[i].y);
  if (xs.some((x,i)=>i>0&&x<=xs[i-1]) || ys.some((y,i)=>i>0&&y<=ys[i-1])) fail('Неоднозначная сетка календаря.');
  const dx = (xs[6]-xs[0])/6, dy=(ys[ys.length-1]-ys[0])/(ys.length-1);
  const dots = nodes(cover).filter((n):n is EllipseNode=>n.type==='ELLIPSE' && n.width>=10 && n.width<=20 && n.height>=10 && n.height<=20).map(node=>({node,...absoluteIn(node,cover)})).filter(d=>d.x>=xs[0]-20&&d.x<xs[6]+dx&&d.y>ys[0]-dy&&d.y<ys[ys.length-1]+dy);
  return {dates,xs,ys,dx,dy,dots};
}
function analyze(raw:string):Plan {
  if (typeof raw !== 'string' || raw.length > 1100000) fail('Нужен JSON-файл CU Clubs до 1 МБ.');
  const data = projectWeek(JSON.parse(raw));
  const source = sourceFrames(), rows=readRows(source.pages), grid=calendar(source.cover);
  if (!rows.length) fail('В выделении не найдены строки встреч.');
  for(const page of source.pages){
    const roots=pageContentRoots(page,rows);
    for(const root of page.children.filter(n=>roots.has(n.id))){
      for(const text of nodes(root).filter(n=>n.type==='TEXT')){
        let n:BaseNode|null=text;let known=false;while(n&&n!==root.parent){if(rows.some(r=>r.node===n)){known=true;break;}n=n.parent;}
        if(!known)fail('В блоке встреч есть отдельный ручной текст. Перенеси его за пределы списка перед генерацией, чтобы он не потерялся.');
      }
    }
  }
  const end = new Date(dateParts(data.week_start).epoch+7*DAY_MS).toISOString().slice(0,10);
  const other=rows.filter(r=>!r.club).map(r=>({...r,date:rowDate(r,data.week_start)}));
  const mismatch=other.filter(r=>r.date<data.week_start||r.date>=end);
  return {data,source,rows,grid,other,mismatch};
}
function solid(color:RGB):SolidPaint[] { return [{type:'SOLID',color}]; }
function textNode(parent:FrameNode, value:string, size:number, style:string, color:RGB, font:string, width:number) {
  const n=figma.createText(); parent.appendChild(n); n.fontName={family:font,style}; n.fontSize=size;
  n.lineHeight={unit:'PERCENT',value:115};n.fills=solid(color);n.characters=value;
  n.textAutoResize='HEIGHT';n.resize(width,n.height);return n;
}
function column(parent:FrameNode) {
  const n=figma.createFrame(); parent.appendChild(n); n.name='Колонка';n.fills=[];n.layoutMode='VERTICAL';n.itemSpacing=20;
  n.resize(510,1);n.layoutSizingHorizontal='FIXED';n.layoutSizingVertical='HUG';return n;
}
function makeClubRow(parent:SectionNode,event:DisplayEvent,font:string,descriptions:boolean) {
  const row=figma.createFrame();parent.appendChild(row);row.name=`CU Clubs · ${event.id}`;row.fills=[];row.layoutMode='HORIZONTAL';row.itemSpacing=20;
  row.resize(1040,1);row.layoutSizingHorizontal='FIXED';row.layoutSizingVertical='HUG';
  const left=column(row),right=column(row);
  textNode(left,event.date_label,32,font==='Inter'?'Medium':'Medium',ORANGE,font,510);
  const endParts=dateParts(event.end_date);
  const after=event.end_date!==event.date?` (${endParts.d} ${MONTHS[endParts.m-1]})`:'';
  textNode(left,`${event.start_time} – ${event.end_time}${after}${event.room?' | '+event.room:''}`,28,'Regular',TEXT_COLOR,font,510);
  textNode(right,event.club_label,32,'Medium',TEXT_COLOR,font,510);
  const details=[event.title,descriptions?event.description:''].filter(Boolean).join('\n');
  textNode(right,details,28,'Regular',TEXT_COLOR,font,510);
  row.setSharedPluginData('cuClubsDigest','eventId',event.id);return row;
}
function pageContentRoots(page:FrameNode, rows:FoundRow[]):Set<string> {
  const roots=new Set<string>();
  for(const row of rows.filter(r=>r.page===page)){
    let parent:BaseNode=row.node;while(parent.parent&&parent.parent!==page)parent=parent.parent;
    if(parent.parent===page)roots.add(parent.id);
  }
  return roots;
}
function dayOrder(a:Item,b:Item) {return a.date<b.date?-1:a.date>b.date?1:(a.club===b.club?(a.minutes-b.minutes||a.order-b.order):(a.club?-1:1));}
async function render(plan:Plan, opts:Options) {
  const font=opts.font==='Inter'?'Inter':'Graphik LC';
  try {await figma.loadFontAsync({family:font,style:'Regular'});await figma.loadFontAsync({family:font,style:'Medium'});}
  catch {fail(`Недоступен ${font} Regular/Medium. Установи шрифт или выбери Inter для черновика. Макет не изменён.`);}
  if(plan.mismatch.length&&!opts.keepOtherWeeks)fail('Другие категории содержат даты вне выбранной недели. Обнови их или явно разреши сохранение этих строк.');
  const section=figma.createSection();section.name=`CU Clubs · ${plan.data.week_start}`;
  section.setSharedPluginData('cuClubsDigest','owner',OWNER);section.setSharedPluginData('cuClubsDigest','week',plan.data.week_start);
  const anchor=plan.source.replace||plan.source.cover;
  section.x=plan.source.replace?anchor.x:anchor.x;section.y=plan.source.replace?anchor.y:anchor.y+anchor.height+240;
  try {
    const cover=plan.source.cover.clone();section.appendChild(cover);cover.x=0;cover.y=0;cover.name='Дайджест · обложка';
    const grid=calendar(cover), year=+plan.data.week_start.slice(0,4), month=+plan.data.week_start.slice(5,7);
    // Preserve the existing calendar's actual date grid. A new month requires an updated cover.
    const monthNode=nodes(cover).find((n):n is TextNode=>n.type==='TEXT'&&MONTH_LABELS.includes(n.characters.trim().toUpperCase()));
    if(monthNode && monthNode.characters.trim().toUpperCase()!==MONTH_LABELS[month-1]) fail('Месяц обложки отличается от недели файла. Сначала подготовь обложку нужного месяца.');
    const firstDay=+grid.dates[0].node.characters.trim();
    let first:number|null=null;
    for(const delta of [-1,0,1]){
      const candidate=Date.UTC(year,month-1+delta,firstDay);
      if(new Date(candidate).getUTCDay()!==1)continue;
      if(grid.dates.every((d,i)=>new Date(candidate+i*DAY_MS).getUTCDate()===+d.node.characters.trim())){first=candidate;break;}
    }
    if(first===null)fail('Не удалось однозначно распознать даты календаря.');
    const clubsDots=grid.dots.filter(d=>orange(d.node));
    const preserved=grid.dots.filter(d=>!orange(d.node));
    for(const d of clubsDots)d.node.remove();
    for(const day of plan.data.days){
      if(!day.count)continue;
      const index=(dateParts(day.date).epoch-first)/DAY_MS;
      if(!Number.isInteger(index)||index<0||index>=grid.dates.length)fail('Неделя не помещается в календарь обложки.');
      const cell=grid.dates[index], col=index%7;
      const occupied=preserved.filter(d=>d.y>=cell.y-grid.dy*.6&&d.y<cell.y+grid.dy*.4&&d.x>=cell.x-13&&d.x<cell.x+grid.dx-13);
      const spots=[];
      const dateBox={x:cell.x,y:cell.y,w:cell.node.width,h:cell.node.height};
      for(let row=0;row<3;row++)for(let c=0;c<4;c++){
        const x=grid.xs[col]+grid.dx-43-(3-c)*28,y=cell.y-27+row*22;
        const hitDate=x<dateBox.x+dateBox.w+3&&x+15>dateBox.x-3&&y<dateBox.y+dateBox.h+3&&y+15>dateBox.y-3;
        const hitOther=occupied.some(d=>Math.abs(d.x-x)<18&&Math.abs(d.y-y)<18);
        if(!hitDate&&!hitOther)spots.push({x,y});
      }
      if(day.count>spots.length)fail(`Для ${day.date} не помещаются ${day.count} точек. Увеличь ячейку календаря; точки не обрезаны.`);
      // Right-align partial rows while keeping occupied non-club markers fixed.
      let remaining=day.count;
      for(let row=0;row<3&&remaining;row++){
        const group=spots.filter(s=>Math.abs(s.y-(cell.y-27+row*22))<1),used=group.slice(-Math.min(remaining,group.length));
        for(const p of used){const dot=figma.createEllipse();cover.appendChild(dot);dot.name=`CU Clubs · ${day.date}`;dot.resize(15,15);dot.fills=solid(ORANGE);dot.x=p.x;dot.y=p.y;dot.setSharedPluginData('cuClubsDigest','owner',OWNER);}remaining-=used.length;
      }
    }
    const items:Item[]=plan.other.map((r,i)=>({...r,club:false,minutes:0,order:i}));
    for(const day of plan.data.days)for(const [i,e] of day.rows.entries())items.push({date:day.date,club:true,event:e,minutes:+e.start_time.slice(0,2)*60+(+e.start_time.slice(3)),order:i});
    items.sort(dayOrder);
    let current:FrameNode|null=null,y=80,pageIndex=0;
    const newPage=()=>{
      const template=plan.source.pages[Math.min(pageIndex,plan.source.pages.length-1)];
      const page=template.clone();section.appendChild(page);
      const roots=pageContentRoots(template,plan.rows);
      for(let i=page.children.length-1;i>=0;i--){const original=template.children[i];if(original&&roots.has(original.id))page.children[i].remove();}
      page.name=`Дайджест · ${pageIndex+2}`;page.x=(pageIndex+1)*1280;page.y=0;page.layoutMode='NONE';page.clipsContent=true;
      pageIndex++;current=page;y=80;
    };
    for(const item of items){
      const row=item.club?makeClubRow(section,item.event!,font,!!opts.descriptions):item.node!.clone();section.appendChild(row);
      if(row.height>1290)fail('Одна встреча не помещается на страницу. Сократи публичное описание или выключи его добавление.');
      if(!current||y+row.height+25>1420)newPage();
      const target:FrameNode|null=current as FrameNode|null;if(!target)fail('Страница не создана.');
      const line=figma.createRectangle();target.appendChild(line);line.name='Разделитель';line.resize(1040,1);line.fills=solid({r:68/255,g:68/255,b:68/255});line.x=80;line.y=y;
      target.appendChild(row);row.layoutSizingHorizontal='FIXED';row.x=80;row.y=y+25;y+=row.height+50;
    }
    let lastDecorated=-1;
    for(const [i,p] of plan.source.pages.entries()){const roots=pageContentRoots(p,plan.rows);if(p.children.some(n=>n.visible!==false&&!roots.has(n.id)&&nodes(n).some(c=>c.type==='TEXT')))lastDecorated=i;}
    while(pageIndex<=lastDecorated)newPage();
    if(!items.length&&!pageIndex)newPage();
    section.resizeWithoutConstraints((pageIndex+1)*1280-80,1500);
    section.setRelaunchData({[COMMAND]:'Обновить дайджест'});
    if(plan.source.replace)plan.source.replace.remove();
    figma.currentPage.selection=[section];figma.viewport.scrollAndZoomIntoView([section]);
    return {pages:pageIndex+1,events:plan.data.event_count,other:plan.other.length};
  }catch(error){section.remove();throw error;}
}
function selectionStatus(force=false){
  const signature=figma.currentPage.selection.map(n=>n.id).join(',');
  if(!force&&signature===lastSelection)return;lastSelection=signature;
  try {const s=sourceFrames();figma.ui.postMessage({type:'selection',ok:true,message:`Выбрано ${s.pages.length+1} страниц. ${s.replace?'Будет обновлена копия плагина.':'Будет создана отдельная копия.'}`});}
  catch(e){figma.ui.postMessage({type:'selection',ok:false,message:e instanceof Error?e.message:'Не удалось проверить выделение.'});}
}
figma.showUI(__html__,{width:390,height:680,themeColors:true});
figma.root.setRelaunchData({[COMMAND]:'Открыть дайджест'});
figma.on('selectionchange',()=>{if(!busy)selectionStatus();});
figma.ui.onmessage=async(message:Message)=>{
  if(message.type==='ready'){selectionStatus(true);return;}
  if(message.type==='resize'){figma.ui.resize(390,Math.max(480,Math.min(860,message.height||680)));return;}
  if(busy)return;
  if(message.type==='preview'||message.type==='generate'){
    busy=true;
    try{
      const plan=analyze(message.raw||'');
      if(message.type==='preview')figma.ui.postMessage({type:'preview',events:plan.data.event_count,days:plan.data.days.map(d=>({date:d.date,count:d.count})),other:plan.other.length,mismatches:plan.mismatch.length,update:!!plan.source.replace});
      else {const result=await render(plan,message.options||{});figma.ui.postMessage({type:'success',...result});}
    }catch(e){figma.ui.postMessage({type:'error',message:e instanceof Error?e.message:'Не удалось подготовить дайджест.'});}
    finally{busy=false;selectionStatus();}
  }
};
