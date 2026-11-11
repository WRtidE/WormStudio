const {test}=require('node:test');
const assert=require('node:assert/strict');
const presentation=require('../web/analysis-presentation.js');
test('historical progress uses functional wording and retains worm identity',()=>{
 for(const engine of ['tapnext','niu','hybrid','topology'])assert.equal(presentation.stage({name:engine+'-worm-9',phase:'fit',label:'old engine'}),'姿态分析 · 线虫 9');
 assert.equal(presentation.stage({phase:'tracking',label:'SAM2 掩膜跟踪'}),'虫体识别与跟踪');
 for(const text of ['SAM2 Large','SAM2 + TAPNext++（单虫）','TAPNext++ 双向点跟踪与时序融合','NIU / WormTracer','WormTracer + NIU','twardis','taper_seed'])assert(!/sam2|tapnext|wormtracer|niu|twardis|taper_seed/i.test(presentation.text(text)));
});
test('preserves actionable plain errors and keeps tracebacks out of the interface',()=>{
 assert.equal(presentation.error('视频只有 4 帧，至少需要 5 帧'),'视频只有 4 帧，至少需要 5 帧');
 assert(!presentation.error('Traceback: SAM2 failed').includes('Traceback'));
});
