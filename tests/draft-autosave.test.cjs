const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createDraftAutosave}=require('../web/draft-autosave.js');
const {runsForFile}=require('../web/workspace-links.js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('edits during an in-flight write are saved in order, including simultaneous flushes',async()=>{
  let value=1,release;const writes=[];
  const saver=createDraftAutosave({delay:60000,capture:()=>value,write:async body=>{writes.push(body);if(body===1)await new Promise(r=>release=r);}});
  saver.mark();const first=saver.flush();await tick();value=2;saver.mark();const second=saver.flush();
  assert.equal(saver.dirty(),true);release();await Promise.all([first,second]);
  assert.deepEqual(writes,[1,2]);assert.equal(saver.dirty(),false);saver.stop();
});
test('failed save stays dirty and explicit retry persists it',async()=>{
  let fail=true;const states=[];
  const saver=createDraftAutosave({delay:60000,capture:()=>({points:[1,2]}),write:async()=>{if(fail)throw Error('offline')},onState:s=>states.push(s.status)});
  saver.mark();await assert.rejects(saver.flush(),/offline/);assert.equal(saver.dirty(),true);
  fail=false;await saver.flush();assert.equal(saver.dirty(),false);assert.ok(states.includes('error'));saver.stop();
});
test('debounced edits are automatically saved and stop prevents later writes',async()=>{
  let saved=0;const saver=createDraftAutosave({delay:10,capture:()=>1,write:async()=>saved++});
  saver.mark();saver.mark();await new Promise(r=>setTimeout(r,40));assert.equal(saved,1);
  saver.stop();saver.mark();await saver.flush();assert.equal(saved,1);
});
test('history uses full path, handles Windows casing, and keeps newest run first',()=>{
  const runs=[{video:'D:/data/same.mp4',updated:1},{video:'D:/other/same.mp4',updated:5},{video:'d:\\data\\same.mp4',updated:3}];
  assert.deepEqual(runsForFile({path:'D:/DATA/same.mp4'},runs).map(r=>r.updated),[3,1]);
  assert.deepEqual(runsForFile({name:'same.mp4'},runs),[]);
  assert.deepEqual(runsForFile({path:'/Data/a.mp4'},[{video:'/data/a.mp4',updated:1}]),[]);
});
