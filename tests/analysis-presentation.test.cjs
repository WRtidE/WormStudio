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
