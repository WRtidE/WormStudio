const test=require('node:test');
const assert=require('node:assert/strict');
const {build,chambers}=require('../web/result-navigation.js');
const fs=require('node:fs'),vm=require('node:vm');
const item=(sample,status='complete')=>({sample,name:sample+'.mp4',status});

for (const useFrameCallback of [true,false]) {
  test('video and timeline paint together once per '+(useFrameCallback?'video callback':'timeupdate'),()=>{
    const nodes = new Map();
    function element(id) {
      if (!nodes.has(id)) nodes.set(id, Object.assign(new EventTarget(),{setAttribute(){}}));
      return nodes.get(id);
    }
    const video = Object.assign(element('studio-result-video'),{
      videoWidth:320, videoHeight:240, readyState:4, paused:false, currentTime:0,
      pause(){this.paused=true;}
    });
    let paints = 0, nextFrame;
    if (useFrameCallback) video.requestVideoFrameCallback = callback => {nextFrame=callback;};
    Object.assign(element('result-canvas'),{
      closest:()=>element('player'), getBoundingClientRect:()=>({width:320,height:240}),
      getContext:()=>({setTransform(){},clearRect(){},save(){},translate(){},scale(){},restore(){},drawImage(){paints++;}})
    });
    const window = {ResultViewport:require('../web/result-viewport.js')};
    vm.runInNewContext(fs.readFileSync(require.resolve('../web/result-player.js'),'utf8'),{
      window,document:{getElementById:element},ResizeObserver:class {observe(){}}
    });
    const player=window.ResultPlayer;
    let position=-1;
    player.configure({frames:[],fps:10},(time,seeked)=>{
      const index=Math.floor(time*10);
      if (position===index && !seeked) return false;
      position=index;
      player.frame({mask_bbox_crop_xyxy:[index*10,40,index*10+30,70]},index);
      return true;
    });
    player.setVisible(true);
    element('result-focus').onclick();
    paints=0;
    function advance(time) {
      video.currentTime=time;
      if (useFrameCallback) nextFrame(0,{mediaTime:time});
      else video.dispatchEvent(new Event('timeupdate'));
    }
    advance(.5); assert.equal(position,5); assert.equal(paints,1);
    advance(.6); assert.equal(position,6); assert.equal(paints,2);
    advance(.61); assert.equal(paints,3);
    video.paused=true;
    advance(.3); assert.equal(position,6); assert.equal(paints,3);
    video.dispatchEvent(new Event('seeked')); assert.equal(position,3); assert.equal(paints,4);
    player.reset();
    video.paused=false; paints=0;
    advance(.8); assert.equal(position,3); assert.equal(paints,1);
  });
}

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

test('a running or failed last chamber keeps earlier results visible, including after restart',()=>{
  const rows=[{...item('first'),chamber_id:1},{...item('second','failed'),chamber_id:2}];
  for(const status of ['running','failed','cancelled']){
    const progress={sample:'second',status,batch:{result_items:rows}};
    assert.deepEqual(chambers(progress),rows);
  }
  assert.deepEqual(chambers({sample:'',status:'ready',batch:{result_items:rows}}),rows);
  assert.deepEqual(chambers({sample:'unrelated',status:'complete',batch:{result_items:rows}}),[]);
});

function headReview(){
  const source=fs.readFileSync(require.resolve('../web/studio-results.js'),'utf8');
  const buttons=new Map(),calls=[],refreshes=[];
  const el=id=>{
    if(!buttons.has(id))buttons.set(id,{disabled:false,checked:false});
    return buttons.get(id);
  };
  const video={paused:false,seeking:false,pause(){this.paused=true;}};
  const context=vm.createContext({el,video,timeline:{sample:'first'},latest:{sample:'first'},
    worm:2,position:37,submittingHead:false,rowAt:()=>({head_review_allowed:true}),
    text:(id,value)=>{el(id).textContent=value;},
    fetch:(url,options)=>new Promise(resolve=>calls.push({url,options,resolve})),
    window:{StudioWorkspace:{refreshSession:async result=>{refreshes.push(result);}}}
  });
  vm.runInContext(source.slice(source.indexOf('function updateHeadButtons('),source.indexOf("el('head-first').onclick")),context);
  context.updateHeadButtons();
  return {context,el,calls,refreshes,video};
}

for(const [button,endpoint] of [['head-a',0],['head-b',1]]){
  test(button+' pauses immediately and posts the displayed choice despite a delayed token',async()=>{
    const {context,el,calls,refreshes,video}=headReview();
    const confirmation=el(button).onclick();
    assert.equal(video.paused,true);
    assert.deepEqual(calls.map(c=>c.url),['/api/progress']);
    context.timeline={sample:'second'};context.latest=context.timeline;context.worm=5;context.position=81;
    context.updateHeadButtons();
    assert.equal(el('head-a').disabled,true);assert.equal(el('head-b').disabled,true);
    await el('head-b').onclick();
    assert.equal(calls.length,1);
    calls[0].resolve({ok:true,json:async()=>({token:'session'})});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls[1].url,'/api/confirm-head');
    assert.equal(calls[1].options.headers['X-Annotation-Token'],'session');
    assert.deepEqual(JSON.parse(calls[1].options.body),{sample:'first',worm_id:2,frame:37,endpoint});
    calls[1].resolve({ok:true,json:async()=>({ok:true})});
    await confirmation;
    assert.equal(refreshes.length,1);assert.equal(calls.length,2);
  });
}

test('a failed head review stays paused and enables retry only on a reviewable frame',async()=>{
  const {context,el,calls,refreshes,video}=headReview();
  const confirmation=el('head-b').onclick();
  calls[0].resolve({ok:true,json:async()=>({token:'session'})});
  await new Promise(resolve=>setImmediate(resolve));
  calls[1].resolve({ok:false,json:async()=>({error:'结果会话已改变'})});
  await confirmation;
  assert.equal(video.paused,true);assert.equal(refreshes.length,0);
  assert.equal(el('head-review-message').textContent,'结果会话已改变');
  assert.equal(el('head-b').disabled,false);
  video.seeking=true;await el('head-b').onclick();
  assert.equal(calls.length,2);
  video.seeking=false;el('result-original').checked=true;context.updateHeadButtons();
  assert.equal(el('head-a').disabled,true);
  el('result-original').checked=false;context.rowAt=()=>undefined;context.updateHeadButtons();
  assert.equal(el('head-b').disabled,true);
});
