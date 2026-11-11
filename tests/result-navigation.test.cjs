const test=require('node:test');
const assert=require('node:assert/strict');
const {build}=require('../web/result-navigation.js');
const item=(sample,status='complete')=>({sample,name:sample+'.mp4',status});

test('navigation stays in the submitted order and excludes old, failed and pending jobs',()=>{
  const progress={status:'complete',sample:'first',batch:{items:[item('old'),item('first'),item('last')],
    result_items:[item('first'),item('failed','failed'),item('pending','pending'),item('last')]}};
  const first=build(progress);
  assert.deepEqual(first.items.map(i=>i.sample),['first','last']);
  assert.equal(first.previous,null);assert.equal(first.next.sample,'last');
  const last=build({...progress,sample:'last'});
  assert.equal(last.previous.sample,'first');assert.equal(last.next,null);
});

test('opening unrelated history never borrows neighbours from a previous batch',()=>{
  const result=build({status:'complete',sample:'history',video_name:'history.mp4',
    batch:{result_items:[item('first'),item('last')]}});
  assert.deepEqual(result.items.map(i=>i.sample),['history']);
  assert.equal(result.grouped,false);assert.equal(result.previous,null);assert.equal(result.next,null);
});

test('empty and legacy sessions do not fall back to the global completed catalogue',()=>{
  assert.equal(build(null).index,-1);
  const result=build({status:'running',sample:'active',batch:{items:[item('old')]}});
  assert.equal(result.items.length,0);assert.equal(result.next,null);
  const single=build({status:'complete',sample:'one',video_name:'one.mp4'});
  assert.equal(single.index,0);assert.equal(single.items.length,1);
});
