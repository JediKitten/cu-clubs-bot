import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';
import { readFileSync } from 'node:fs';
const code=stripTypeScriptTypes(readFileSync(new URL('./code.ts',import.meta.url),'utf8'));

function environment(){
 let serial=0;const messages=[];const orange={r:1,g:102/255,b:44/255},green={r:.76,g:.95,b:.16};
 class Node{
  constructor(type){this.type=type;this.id=String(++serial);this.name=type;this.children=[];this.parent=null;this.x=0;this.y=0;this.w=100;this.h=100;this.rotation=0;this.visible=true;this.layoutMode='NONE';this.layoutSizingVertical='FIXED';this.fills=[];this.itemSpacing=0;this.characters='';this.fontSize=28;this.data={};this.removed=false;}
  get width(){return this.w}get height(){if(this.type==='TEXT')return Math.ceil(Math.max(1,this.characters.split('\n').length,Math.ceil(this.characters.length/(this.w/(this.fontSize*.5))))*this.fontSize*1.15);if(this.layoutSizingVertical==='HUG'&&this.children.length)return this.layoutMode==='VERTICAL'?this.children.reduce((s,n)=>s+n.height,0)+(this.children.length-1)*this.itemSpacing:Math.max(...this.children.map(n=>n.height));return this.h}
  get absoluteTransform(){const p=this.parent?.absoluteTransform||[[1,0,0],[0,1,0]];return [[1,0,p[0][2]+this.x],[0,1,p[1][2]+this.y]]}
  resize(w,h){this.w=w;this.h=h}resizeWithoutConstraints(w,h){this.resize(w,h)}
  appendChild(n){if(n.parent){const i=n.parent.children.indexOf(n);if(i>=0)n.parent.children.splice(i,1)}this.children.push(n);n.parent=this}
  remove(){if(this.parent){const i=this.parent.children.indexOf(this);if(i>=0)this.parent.children.splice(i,1)}this.parent=null;this.removed=true}
  clone(){const copy=n=>{const c=new Node(n.type);for(const k of ['name','x','y','w','h','rotation','visible','layoutMode','layoutSizingVertical','layoutSizingHorizontal','fills','itemSpacing','characters','fontSize','fontName','lineHeight','data'])c[k]=structuredClone(n[k]);for(const child of n.children)c.appendChild(copy(child));return c};const c=copy(this);this.parent?.appendChild(c);return c}
  setSharedPluginData(ns,k,v){this.data[ns+':'+k]=v}getSharedPluginData(ns,k){return this.data[ns+':'+k]||''}setRelaunchData(){}
 }
 const page=new Node('PAGE');page.selection=[];
 const create=type=>{const n=new Node(type);page.appendChild(n);return n};
 const figma={currentPage:page,root:{setRelaunchData(){}},ui:{postMessage:m=>messages.push(m),resize(){}},showUI(){},on(){},viewport:{scrollAndZoomIntoView(){}},loadFontAsync:async()=>{},createSection:()=>create('SECTION'),createFrame:()=>create('FRAME'),createText:()=>create('TEXT'),createEllipse:()=>create('ELLIPSE'),createRectangle:()=>create('RECTANGLE')};
 const ctx=vm.createContext({figma,__html__:'',console});vm.runInContext(code+'\nglobalThis.api={analyze,render,sourceFrames,selectionStatus};',ctx);
 const frame=()=>{const n=create('FRAME');n.resize(1200,1500);return n};
 const text=(parent,value,color,x=0,y=0,w=510)=>{const n=create('TEXT');parent.appendChild(n);n.characters=value;n.fills=[{type:'SOLID',color}];n.x=x;n.y=y;n.w=w;return n};
 const cover=frame();text(cover,'События недели',green,80,80);text(cover,'НОЯБРЬ',green,80,180);
 for(let i=0;i<35;i++)text(cover,String(new Date(Date.UTC(2026,10,2+i)).getUTCDate()),green,93+(i%7)*151,1033+Math.floor(i/7)*85,25);
 const dot=(color,x,y)=>{const n=create('ELLIPSE');cover.appendChild(n);n.resize(15,15);n.fills=[{type:'SOLID',color}];n.x=x;n.y=y;return n};
 dot(orange,201,1091);const preserved=dot(green,352,1091);
 const details=frame();details.x=1280;const content=create('FRAME');details.appendChild(content);content.x=80;content.y=80;
 const row=(date,color,label,y)=>{const r=create('FRAME');content.appendChild(r);r.layoutMode='HORIZONTAL';r.resize(1040,90);r.y=y;const l=create('FRAME'),right=create('FRAME');r.appendChild(l);r.appendChild(right);l.resize(510,90);right.resize(510,90);right.x=530;text(l,date,color);text(right,label,green);return r};
 row('9 ноября (понедельник)',orange,'Старая клубная встреча',0);
 const other=row('10 ноября (вторник)',green,'Другой раздел: сохранить точно',140);
 page.selection=[cover,details];
 const raw=JSON.stringify({schema_version:1,timezone:'Europe/Moscow',week_start:'2026-11-09',events:[{id:'one',version:1,visibility:'open',status:'published',starts_at:'2026-11-09T18:00:00+03:00',ends_at:'2026-11-09T19:00:00+03:00',clubs:[{id:'cinema',name:'Киноклуб'}],title:'Просмотр фильма',description:'',room:'Кинозал'}]});
 return{...ctx.api,ctx,figma,page,cover,details,other,preserved,messages,raw,Node,text,green};
}
function flat(root){const q=[root];for(let i=0;i<q.length;i++)q.push(...q[i].children);return q}
function publicState(root){return flat(root).map(n=>[n.id,n.type,n.name,n.characters,n.x,n.y,n.width,n.height,n.fills])}

test('preview does not modify canvas or invalidate itself through repeated selection messages',async()=>{
 const e=environment(),before=JSON.stringify(publicState(e.page));
 e.selectionStatus(true);e.messages.length=0;
 await e.figma.ui.onmessage({type:'preview',raw:e.raw});
 assert.equal(JSON.stringify(publicState(e.page)),before);
 assert.deepEqual(e.messages.map(m=>m.type),['preview']);
 assert.equal(e.messages[0].events,1);
});
test('render creates a separate copy, preserves other rows/markers and repeat run replaces only owned copy',async()=>{
 const e=environment();const before=JSON.stringify(publicState(e.cover));const otherText=e.other.children[1].children[0].characters;
 await e.render(e.analyze(e.raw),{font:'Inter'});
 const section=e.page.selection[0];assert.equal(section.type,'SECTION');assert.equal(section.getSharedPluginData('cuClubsDigest','owner'),'cu-clubs-digest-v1');
 assert.equal(JSON.stringify(publicState(e.cover)),before);assert.ok(!e.details.removed);
 const output=flat(section);assert.ok(output.some(n=>n.characters===otherText));assert.ok(output.some(n=>n.characters==='Киноклуб'));assert.ok(!output.some(n=>n.characters==='Старая клубная встреча'));
 const oldMark=output.find(n=>n.type==='ELLIPSE'&&n.fills[0]?.color.g===e.green.g);assert.equal(oldMark.x,e.preserved.x);assert.equal(oldMark.y,e.preserved.y);
 await e.render(e.analyze(e.raw),{font:'Inter'});
 assert.ok(section.removed);assert.equal(e.page.children.filter(n=>n.type==='SECTION').length,1);assert.ok(!e.cover.removed);
});
test('font failure and oversized event rollback do not leave partial output',async()=>{
 const e=environment(),before=e.page.children.length;e.figma.loadFontAsync=async()=>{throw Error('missing')};
 await assert.rejects(e.render(e.analyze(e.raw),{}),/Недоступен/);assert.equal(e.page.children.length,before);
 e.figma.loadFontAsync=async()=>{};const payload=JSON.parse(e.raw);payload.events[0].description='Очень длинный текст. '.repeat(1000);
 await assert.rejects(e.render(e.analyze(JSON.stringify(payload)),{font:'Inter',descriptions:true}),/не помещается/);
 assert.equal(e.page.children.length,before);
});
test('manual non-row text would not be silently removed',()=>{
 const e=environment();const root=e.details.children[0];e.text(root,'Ручная заметка вне строки',e.green);
 assert.throws(()=>e.analyze(e.raw),/ручной текст/);
});
test('calendar overflow fails without dropping markers or changing original frames',async()=>{
 const e=environment(),data=JSON.parse(e.raw);data.events=Array.from({length:15},(_,i)=>({...data.events[0],id:`event-${i}`}));
 const before=e.page.children.length;
 await assert.rejects(e.render(e.analyze(JSON.stringify(data)),{font:'Inter'}),/не помещаются/);
 assert.equal(e.page.children.length,before);
});
