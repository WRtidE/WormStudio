'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/manual_worm_annotation.js'),'utf8');
const start=source.indexOf('function frameMetricsRoi('),end=source.indexOf("$('#calculate-frame-metrics').onclick=calculateFrameMetrics;")+"$('#calculate-frame-metrics').onclick=calculateFrameMetrics;".length;
function fixture(){
 const nodes=new Map();function node(){return{children:[],value:'',checked:false,get options(){return this.children},replaceChildren(...items){this.children=items},append(...items){this.children.push(...items)}};}
 const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)};
 $('#scale').value='2';
 const worm={confirmed:true,mask:{toDataURL:()=> 'mask-png'}};
 const context=vm.createContext({$,document:{createElement:node},state:{frame:4},selected:1,objects:new Map([[1,worm]]),roi:[0,0,100,100],calibrationPending:false,step:2,busy:false,saved:false,poly:[],chamberLabel:id=>'线虫 '+id,setBusy:value=>{context.busy=value},draw(){},annotationPost:async()=>({frame:4,pose_available:true,metrics:[{key:'area',label:'当前帧面积',value:0,unit:'μm²'}]})});
 context.Option=class{constructor(text,value){this.text=text;this.value=value;}};
 vm.runInContext(source.slice(start,end),context);return{context,nodes,worm,run:code=>vm.runInContext(code,context)};
}
test('confirmed frame measurements become stale after ROI, scale, unit, frame or mask confirmation changes',async()=>{
 const f=fixture();await f.run('calculateFrameMetrics()');assert.ok(f.run('currentFrameMetrics()'));
 for(const update of ["roi=[1,0,100,100]","$('#scale').value='3'","$('#pixel-mode').checked=true","state.frame=5"]){
  const f=fixture();await f.run('calculateFrameMetrics()');f.run(update);assert.equal(f.run('currentFrameMetrics()'),null);
 }
 f.worm.confirmed=false;assert.equal(f.run('currentFrameMetrics()'),null);
});
test('late response cannot appear on a changed frame and real zero is not replaced by missing',async()=>{
 const f=fixture();await f.run('calculateFrameMetrics()');f.run('refreshFrameMetrics()');
 assert.equal(f.nodes.get('#annotation-metrics-values').children[0].children[1].textContent,'0 μm²');
 let resolve;f.context.annotationPost=()=>new Promise(r=>{resolve=r});const request=f.run('calculateFrameMetrics()');f.run('state.frame=9');resolve({frame:4,metrics:[]});await request;assert.equal(f.run('currentFrameMetrics()'),null);
});
test('unconfirmed masks do not trigger a measurement request',async()=>{
 const f=fixture();f.worm.confirmed=false;f.context.annotationPost=()=>{throw Error('should not request')};
 await f.run('calculateFrameMetrics()');f.run('refreshFrameMetrics()');assert.equal(f.nodes.get('#calculate-frame-metrics').disabled,true);
});
test('confirmation starts on the active chamber and switches measurements without switching the annotation target',async()=>{
 const f=fixture(),c=f.context,calls=[];
 c.state.chamber_mode=true;c.step=3;c.selected=24;c.objects.clear();
 for(const id of [24,22,23])c.objects.set(id,{confirmed:true,roi:[id,0,id+1,50],mask:{toDataURL:()=>String(id)}});
 c.roi=c.objects.get(24).roi;
 let fail=true;
 c.annotationPost=async(_url,body)=>{
  calls.push(body.worm_id);
  assert.equal(body.mask,String(body.worm_id));assert.equal(body.roi,c.objects.get(body.worm_id).roi);
  if(fail&&body.worm_id===23)throw Error('轮廓超出区域');
  return {metrics:[{key:'area',label:'当前帧面积',value:body.worm_id,unit:'μm²'}]};
 };
 f.run('refreshFrameMetrics()');
 const picker=f.nodes.get('#annotation-metrics-chamber');
 const choose=async value=>{picker.value=value;await picker.onchange();};
 const cells=id=>f.nodes.get(id).children[0].children.map(n=>n.textContent);
 assert.equal(picker.value,'24');assert.deepEqual(picker.options.map(o=>o.value),['22','23','24','all']);
 await f.run('calculateFrameMetrics({missingOnly:true})');
 assert.deepEqual(calls,[24]);assert.deepEqual(cells('#annotation-metrics-values'),['面积','24 μm²']);
 await choose('22');assert.deepEqual(cells('#annotation-metrics-values'),['面积','22 μm²']);
 await choose('23');assert.equal(f.nodes.get('#annotation-metrics-table').hidden,true);
 assert.match(f.nodes.get('#annotation-metrics-status').textContent,/轮廓超出区域/);
 await choose('24');assert.doesNotMatch(f.nodes.get('#annotation-metrics-status').textContent,/超出区域/);
 assert.deepEqual(calls,[24,22,23]);assert.equal(c.selected,24);assert.equal(c.roi,c.objects.get(24).roi);
 fail=false;await choose('all');
 assert.deepEqual(calls,[24,22,23,23]);assert.deepEqual(cells('#annotation-metrics-columns'),['指标','槽 22','槽 23','槽 24']);
 assert.deepEqual(cells('#annotation-metrics-values'),['面积','22','23','24']);
 await choose('23');assert.deepEqual(cells('#annotation-metrics-values'),['面积','23 μm²']);
 assert.equal(f.nodes.get('#annotation-metrics-columns').children.length,0);
 c.step=2;f.run('refreshFrameMetrics()');assert.equal(f.nodes.get('#annotation-metrics-picker').hidden,true);
 assert.deepEqual(cells('#annotation-metrics-values'),['面积','24 μm²']);
});
