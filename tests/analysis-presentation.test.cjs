const {test}=require('node:test');
const assert=require('node:assert/strict');
const presentation=require('../web/analysis-presentation.js');
test('quality checks and crop recovery have distinct progress labels',()=>{
 assert.equal(presentation.stage({name:'quality',phase:'quality'}),'身份与接触检查');
 assert.equal(presentation.stage({name:'crop-recovery',phase:'quality'}),'触边扩框与重跟踪');
});
test('historical progress uses functional wording and retains worm identity',()=>{
 for(const engine of ['tapnext','niu','hybrid','topology'])assert.equal(presentation.stage({name:engine+'-worm-9',phase:'fit',label:'old engine'}),'姿态分析 · 线虫 9');
 assert.equal(presentation.stage({phase:'tracking',label:'SAM2 掩膜跟踪'}),'虫体识别与跟踪');
 for(const text of ['SAM2 Large','SAM2 + TAPNext++（单虫）','TAPNext++ 双向点跟踪与时序融合','NIU / WormTracer','WormTracer + NIU','twardis','taper_seed'])assert(!/sam2|tapnext|wormtracer|niu|twardis|taper_seed/i.test(presentation.text(text)));
});
test('preserves actionable plain errors and keeps tracebacks out of the interface',()=>{
 assert.equal(presentation.error('视频只有 4 帧，至少需要 5 帧'),'视频只有 4 帧，至少需要 5 帧');
 assert(!presentation.error('Traceback: SAM2 failed').includes('Traceback'));
 const long="export 失败（退出码 1）\nFileNotFoundError: [Errno 2] No such file or directory: 'C:/"+'x'.repeat(250)+"/annotated.mp4'";
 assert.match(presentation.error(long),/路径过长.*继续此任务/);
 assert.match(presentation.error('export 失败：编码器退出'),/视频导出失败/);
});
test('completed metrics become the default result without interrupting a batch or history',()=>{
 const prompt=presentation.totalsReady;
 const run={sample:'video-one',status:'complete',phenotypes:{worms:[{worm_id:1}]}};
 for(const status of ['preparing','running','failed','cancelled'])assert.equal(prompt({...run,status}),false);
 assert.equal(prompt({...run,phenotypes:null}),false);
 assert.equal(prompt({...run,batch:{running:true}}),false);
 assert.equal(prompt({...run,history_reopened:true}),false);
 assert.equal(prompt(run),true);
 assert.equal(prompt(run),true);
 assert.equal(prompt({...run,sample:'video-two'}),true);
});

test('the progress page keeps polling across chamber handoffs, failures and a reconnect',async()=>{
 const vm=require('node:vm'),fs=require('node:fs');
 const nav=require('../web/result-navigation.js');
 const nodes=new Map(),timers=new Map(),previews=[];
 function element(){return {children:[],style:{},dataset:{},firstChild:{},classList:{add(){},remove(){},toggle(){}},
  setAttribute(){},removeAttribute(){},replaceChildren(...nodes){this.children=nodes},append(...nodes){this.children.push(...nodes)}};}
 const dom=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
 const frames=[
  ['running',23,['running','pending','pending']],
  ['complete',23,['running','pending','pending']],
  ['complete',23,['complete','pending','pending'],'disk full'],
  null,
  ['preparing',24,['complete','running','pending']],
  ['failed',24,['complete','failed','pending']],
  ['running',25,['complete','failed','running']],
  ['complete',25,['complete','failed','complete']]
 ];
 let nextTimer=0,last;
 const ctx=vm.createContext({window:{LivePreview:{update(d){previews.push(d.preview_run)}}},
  document:{body:element(),querySelector:dom,createElement:element,createTextNode:String},
  AnalysisPresentation:presentation,ResultNavigation:nav,AbortController,
  setTimeout(fn,ms){const id=++nextTimer;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id)},
  async fetch(route,options){
   if(route==='/api/batch'){
    assert.deepEqual(JSON.parse(options.body),{action:'retry-dispatch'});
    assert.equal(options.headers['X-Annotation-Token'],'session-token');
    last={...last,batch:{...last.batch,running:true,dispatch_error:''}};
    return {ok:true,json:async()=>last};
   }
   const f=frames.shift();if(!f)throw Error('offline');
   const [status,id,states,dispatch_error]=f;
   const items=states.map((state,i)=>({chamber_id:23+i,sample:String(23+i),status:state,video_path:'222324.mp4'}));
   last={status,sample:String(id),token:'session-token',stages:[],active:null,percent:0,batch:{running:!!frames.length&&!dispatch_error,dispatch_error,result_items:items},preview_run:status==='preparing'?null:'run-'+id};
   return {ok:true,json:async()=>last};
  }});
 const pollSource=fs.readFileSync(require.resolve('../web/progress-poll.js'),'utf8').replace('export async', 'async');
 const pageSource=fs.readFileSync(require.resolve('../web/processing.js'),'utf8').replace(/^import .*?;\s*/, '');
 await vm.runInContext(pollSource+'\n'+pageSource,ctx);
 async function tick(){const pending=[...timers].find(([,t])=>t.ms===1000);assert.ok(pending,'must still be polling');timers.delete(pending[0]);await pending[1].fn();}
 await tick();
 assert.equal(presentation.sessionView(last),'progress');
 assert.equal(dom('#finish-session').hidden,true);assert.equal(dom('#result-card').hidden,true);
 assert.match(dom('#batch-progress-count').textContent,/1 \/ 3/);
 await tick();assert.equal(presentation.sessionView(last),'progress');
 assert.equal(dom('#dispatch-failure').hidden,false);assert.equal(dom('#dispatch-error').textContent,'disk full');
 assert.equal(dom('#finish-session').hidden,true);
 await dom('#retry-dispatch').onclick({currentTarget:dom('#retry-dispatch')});
 assert.equal(dom('#dispatch-failure').hidden,true);
 await tick();assert.equal(dom('#offline-notice').hidden,false);
 await tick();assert.match(dom('#progress-title').textContent,/槽 24/);
 assert.equal(dom('#offline-notice').hidden,true);
 await tick();assert.equal(dom('.processing-actions').hidden,true);
 await tick();assert.match(dom('#progress-title').textContent,/槽 25/);
 await tick();assert.equal(timers.size,0);
 assert.equal(dom('#result-card').hidden,false);
 assert.deepEqual(dom('#batch-progress-items').children.map(n=>n.dataset.status),['complete','failed','complete']);
 assert.deepEqual(previews,['run-23','run-23','run-23','run-23',null,'run-24','run-25','run-25']);
});
