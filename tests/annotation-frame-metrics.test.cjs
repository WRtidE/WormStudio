'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/manual_worm_annotation.js'),'utf8');
const start=source.indexOf('function frameMetricsContext()'),end=source.indexOf("$('#calculate-frame-metrics').onclick=calculateFrameMetrics;")+"$('#calculate-frame-metrics').onclick=calculateFrameMetrics;".length;
function fixture(){
 const nodes=new Map();function node(){return{children:[],value:'',checked:false,replaceChildren(){this.children=[]},append(...items){this.children.push(...items)}};}
 const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)};
 $('#scale').value='2';
 const worm={confirmed:true,mask:{toDataURL:()=> 'mask-png'}};
 const context=vm.createContext({$,document:{createElement:node},state:{frame:4},selected:1,roi:[0,0,100,100],calibrationPending:false,step:2,busy:false,saved:false,poly:[],active:()=>worm,setBusy:value=>{context.busy=value},draw(){},say(){},annotationPost:async()=>({frame:4,pose_available:true,metrics:[{key:'area',label:'当前帧面积',value:0,unit:'μm²'}]})});
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
