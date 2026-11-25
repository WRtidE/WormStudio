import {annotationReadiness, measureCalibration, detectScaleBar} from './annotation-readiness.js';
const CURRENT_HEAD_DETECTION='taper_seed';
function analysisMethod(){return state?.analysis_method||'body_sequence';}
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const canvas=$('#overlay'),ctx=canvas.getContext('2d'),base=$('#base'),viewport=$('#viewport');
let state,W,H,step=1,mode='view',selected=1,objects=new Map(),history=[],roi=null,calibration=null,scalePoints=[],poly=[],drag=null,last=null,boxDrag=null,busy=false,saved=false,zoom=1;
const scratch=document.createElement('canvas');
let scalePointMethod='manual';
let calibrationPending=false,repairReturnStep=null,draftSaver=null,allowLeave=false;
function readiness(){
 const algorithmIssue=analysisMethod()!=='body_sequence'?'':!state.chamber_mode&&objects.size!==1?'当前每次分析一条线虫，请保留需要分析的一条。':state.sam2_model!=='large'?'此任务的分析配置需要更新，请从文件列表重新开始标注。':state.algorithm?.checking?'正在检查分析组件，请稍候。':state.algorithm?.available!==true?'分析组件暂不可用，请检查本地安装后重新检查。':'';
 const check=annotationReadiness({scale:$('#scale').value,pixelMode:$('#pixel-mode').checked,calibrationPending,worms:[...objects.values()],method:analysisMethod(),chamberMode:state.chamber_mode,algorithmIssue});
 if(!state.chamber_mode&&analysisMethod()==='body_sequence'&&objects.size>1){const blocker=check.blockers.find(b=>b.key==='method');blocker.step=2;blocker.action='返回选择线虫';}
 return {...check,algorithmIssue:check.blockers.find(b=>b.key==='method')?.detail || algorithmIssue};
}
function resolveBlocker(blocker){
 if(!blocker||busy||saved)return;
 if(step===3&&blocker.step!==3)repairReturnStep=3;
 if(blocker.key==='masks'){const pending=[...objects].find(([,o])=>!o.confirmed||o.area<20);if(pending)selectObject(pending[0])}
 showStep(blocker.step);
 const target=blocker.key==='scale'?$('#scale-length'):blocker.step===2?$('#worm-list'):$('#analysis-status');
 target.scrollIntoView({block:'center'});target.focus({preventScroll:true});
 say(blocker.detail||blocker.title);
}
function refreshGuidance(check){
 const rows=[['scale',1,$('#pixel-mode').checked?'仅像素单位':'尺寸标定',check.scaleReady],['masks',2,`虫体掩膜 · ${check.confirmed}/${objects.size} 已确认`,check.masksReady],['method',3,'分析准备',check.methodReady]];
 const list=$('#readiness-list');list.replaceChildren();
 for(const [key,n,label,complete] of rows){
  const row=document.createElement('button');row.className='readiness-row '+(complete?'complete':'incomplete');row.type='button';
  const text=document.createElement('span');text.textContent=`${complete?'✓':'!'} ${String(n).padStart(2,'0')} ${label}`;
  const status=document.createElement('b');status.textContent=complete?'已完成':'去完成 →';row.append(text,status);
  row.onclick=()=>resolveBlocker(check.blockers.find(b=>b.key===key));row.disabled=complete||busy||saved;list.append(row);
 }
 $('#readiness-count').textContent=check.ready?'全部完成':`还差 ${check.blockers.length} 项`;
 const blocker=step===1?check.blockers.find(b=>b.key==='scale'):step===2?check.blockers.find(b=>b.key==='masks'):check.blockers[0];
 const title=saved?'标注已保存':busy?'正在处理，请稍候…':blocker?(step===3?`暂不能开始：${blocker.title}`:blocker.title):step===3?'准备完成，可以开始处理':step===1?($('#pixel-mode').checked?'像素单位已设置':'尺寸标定已完成'):'虫体掩膜已全部确认';
 $('#action-title').textContent=title;
 $('.annotation-footer').classList.toggle('needs-attention',!!blocker&&!busy&&!saved);
 $('#resolve-blocker').hidden=!blocker||busy||saved||(step===2&&blocker.key==='masks');$('#resolve-blocker').textContent=blocker?.action||'';$('#resolve-blocker').onclick=()=>resolveBlocker(blocker);
 $('#next').innerHTML=(repairReturnStep?'返回处理前检查':step===1?'下一步：点选线虫':'下一步：确认并开始')+' <span>→</span>';
 $$('[data-step]').forEach(e=>{const n=+e.dataset.step,complete=[false,check.scaleReady,check.masksReady,check.ready][n];e.classList.toggle('done',complete&&n!==step);e.classList.toggle('needs-attention',!complete&&n<step);e.setAttribute('aria-current',n===step?'step':'false');e.querySelector('small').textContent=n===1?(check.scaleReady?($('#pixel-mode').checked?'像素单位已设置':'尺寸已标定'):'尺寸标定待完成'):n===2?(check.masksReady?'掩膜已全部确认':'生成并确认虫体掩膜'):(check.ready?'可以保存并开始处理':`还有 ${check.blockers.length} 项待完成`)});
}
function calibrationText(result){
 if(result.calibration)return `${result.calibration.length_um} μm ÷ ${result.distance.toFixed(1)} px = ${Number(result.scale.toPrecision(6))} μm/px`;
 if(result.distance){
  const length=$('#scale-length');
  return length.validity.badInput||length.value!==''?`${result.distance.toFixed(1)} px · 实际长度须大于 0`:`${result.distance.toFixed(1)} px`;
 }
 if(mode==='scale_auto'&&!scalePoints.length)return '点击画面中的标尺线。';
 return result.error;
}
function updateCalibration(message){
 const result=measureCalibration(scalePoints,$('#scale-length').value);
 calibration=result.calibration||null;
 if(calibration&&scalePointMethod==='auto')calibration.method='auto';
 calibrationPending=!calibration;
 $('#scale').value=calibration?result.scale.toPrecision(8):'';
 $('#calibration-info').textContent=message||calibrationText(result);
 refresh();draw();
}
function isScaleTool(value){return value==='scale_auto'||value==='scale';}
function startCalibration(tool){
 setMode(tool);scalePoints=[];updateCalibration();
}
function pickScale(point){
 if(mode==='scale_auto'){
  // The displayed frame and the overlay are separate. Read only the source
  // image so a previous endpoint, ROI or worm mask cannot affect detection.
  const context=scratch.getContext('2d');
  context.drawImage(base,0,0,W,H);
  scalePoints=detectScaleBar(context.getImageData(0,0,W,H),point)||[];
  if(!scalePoints.length){
   setMode('scale');scalePointMethod='manual';
   updateCalibration('已切换为手动标定，请先点标尺的一端，再点另一端。');
  }else{scalePointMethod='auto';updateCalibration();}
 }else{
  scalePointMethod='manual';
  if(scalePoints.length===2)scalePoints=[];
  scalePoints.push(point);updateCalibration();
 }
}
const modes={head:['标记头部','点击当前线虫的头端；此点不参与分割'],view:['查看画面','先设置分析范围与尺寸'],roi:['框选分析区域','拖动框选，覆盖整段活动范围'],scale_auto:['单击识别','点击标尺线'],scale:['手动选两端','依次点击标尺两端'],sam_positive:['点选虫体','点击属于当前线虫的区域 · 1'],sam_negative:['排除提示','点击误选区域 · 2'],sam_box:['框住虫体','拖动框选当前线虫'],paint:['涂画补全','按住拖动补全虫体'],erase:['擦除多余','按住拖动精确删除像素 · 3'],polygon:['多边形','依次点击边界，再闭合填充']};
function say(text,kind=''){const el=$('#status');el.textContent=AnalysisPresentation.text(text);el.className='status-line'+(kind?' '+kind:'')}
function color(id){return ['#43d39b','#d976ed','#58c8ef','#ff9585'][id-1]||`hsl(${id*137.508%360} 70% 65%)`}
function empty(){const c=document.createElement('canvas');c.width=W;c.height=H;return c}
function active(){return objects.get(selected)}

const sampler=$('#head-sampler'), sampleCanvas=$('#sample-canvas');
const sampleView=$('#sample-view');
let sampleFrames=[], sampleRequest=0, sampleReady=false, sampleBounds;
let sampleTransform=null, sampleGesture=null;
function fittedSample(){
 return ResultViewport.fit(sampleCanvas.width,sampleCanvas.height,sampleView.clientWidth,sampleView.clientHeight);
}
function positionSample(transform){
 const {width,height}=sampleCanvas;
 sampleTransform=ResultViewport.constrain(transform,width,height,sampleView.clientWidth,sampleView.clientHeight);
 const {x,y,s}=sampleTransform;
 sampleCanvas.style.transform=`translate(${x}px,${y}px) scale(${s})`;
 const ratio=s/fittedSample().s;
 $('#sample-zoom').textContent=Number(ratio.toFixed(2))+'×';
 $('#sample-out').disabled=ratio<=1.001;
 $('#sample-in').disabled=ratio>=15.999;
}
function zoomSample(factor,x=sampleView.clientWidth/2,y=sampleView.clientHeight/2){
 if(!sampleReady)return;
 const fit=fittedSample();
 positionSample(ResultViewport.zoomAt(sampleTransform,factor,x,y,fit.s,fit.s*16));
}
$('#sample-in').onclick=()=>zoomSample(1.5);
$('#sample-out').onclick=()=>zoomSample(1/1.5);
$('#sample-fit').onclick=()=>{if(sampleReady)positionSample(fittedSample())};
sampleView.addEventListener('wheel',e=>{
 e.preventDefault();
 const rect=sampleView.getBoundingClientRect();
 const pixels=e.deltaY*(e.deltaMode===1 ? 16 : e.deltaMode===2 ? rect.height : 1);
 const factor=Math.exp(-Math.max(-250,Math.min(250,pixels))*.002);
 zoomSample(factor,e.clientX-rect.left,e.clientY-rect.top);
},{passive:false});
new ResizeObserver(()=>{
 if(sampleReady&&sampler.open)positionSample(fittedSample());
}).observe(sampleView);
function cancelSampleGesture(){
 sampleGesture=null;
 sampleView.classList.remove('dragging');
}
function endSampling(){
 sampler.close();
 refresh();
}
async function showSample(){
 const request=++sampleRequest;
 sampleReady=false;
 cancelSampleGesture();
 sampleCanvas.hidden=true;
 if(!sampleFrames.length){endSampling();return}
 const frame=sampleFrames[0];
 $('#sample-title').textContent=`${state.chamber_mode?'槽':'线虫'} ${selected} · F${frame}`;
 $('#sample-status').textContent='正在读取画面…';
 try{
  const response=await fetch('/api/head-preview?frame='+frame,{headers:{'X-Annotation-Token':state.token}});
  if(!response.ok)throw Error('读取失败，可以跳过此帧。');
  const bitmap=await createImageBitmap(await response.blob());
  try{
   if(request!==sampleRequest||!sampler.open)return;
   const [x,y,x1,y1]=sampleBounds;
   sampleCanvas.width=x1-x;sampleCanvas.height=y1-y;
   sampleCanvas.getContext('2d').drawImage(bitmap,x,y,x1-x,y1-y,0,0,x1-x,y1-y);
  }finally{bitmap.close()}
  sampleReady=true;
  positionSample(sampleTransform||fittedSample());
  sampleCanvas.hidden=false;
  $('#sample-status').textContent=`滚轮缩放，拖动画面，单击头端进入下一帧。还剩 ${sampleFrames.length} 帧。`;
 }catch(error){if(request===sampleRequest)$('#sample-status').textContent=error.message}
}
for(const button of document.querySelectorAll('[data-sample-heads]')){
 button.onclick=()=>{
  if(!active()||busy||saved)return;
  const used=new Set([state.frame,...(active().headPoints||[]).map(a=>a.frame)]);
  const available=Array.from({length:state.frame_count},(_,i)=>i).filter(i=>!used.has(i));
  const count=Math.min(Number(button.dataset.sampleHeads),available.length);
  if(!count){say('所有可抽取帧都已标记。');return}
  sampleFrames=Array.from({length:count},(_,i)=>{
   const start=Math.floor(i*available.length/count),end=Math.floor((i+1)*available.length/count);
   return available[start+Math.floor(Math.random()*(end-start))];
  });
  sampleBounds=(roi||[0,0,W,H]).slice();
  sampleTransform=null;
  sampler.showModal();void showSample();
 };
}
sampleView.onpointerdown=e=>{
 if(!sampleReady||e.button!==0||!e.isPrimary||sampleGesture)return;
 sampleView.setPointerCapture(e.pointerId);
 sampleGesture={id:e.pointerId,x:e.clientX,y:e.clientY,transform:sampleTransform,dragged:false};
};
sampleView.onpointermove=e=>{
 const g=sampleGesture;
 if(!g||g.id!==e.pointerId)return;
 const dx=e.clientX-g.x,dy=e.clientY-g.y;
 if(!g.dragged&&Math.hypot(dx,dy)<4)return;
 g.dragged=true;
 sampleView.classList.add('dragging');
 positionSample({...g.transform,x:g.transform.x+dx,y:g.transform.y+dy});
};
sampleView.onlostpointercapture=sampleView.onpointercancel=e=>{
 if(sampleGesture?.id===e.pointerId)cancelSampleGesture();
};
sampleView.onpointerup=e=>{
 const g=sampleGesture;
 if(!g||g.id!==e.pointerId)return;
 cancelSampleGesture();
 if(!sampleReady||g.dragged||Math.hypot(e.clientX-g.x,e.clientY-g.y)>=4)return;
 const rect=sampleCanvas.getBoundingClientRect();
 if(e.clientX<rect.left||e.clientX>=rect.right||e.clientY<rect.top||e.clientY>=rect.bottom)return;
 const point=[Math.floor((e.clientX-rect.left)*sampleCanvas.width/rect.width)+sampleBounds[0],
              Math.floor((e.clientY-rect.top)*sampleCanvas.height/rect.height)+sampleBounds[1]];
 remember();
 active().headPoints=[...(active().headPoints||[]),{frame:sampleFrames.shift(),point}];
 refresh();void showSample();
};
$('#sample-skip').onclick=()=>{sampleFrames.shift();void showSample()};
$('#sample-close').onclick=endSampling;
$('#remove-sampled-head').onclick=()=>{
 if(busy||saved||!active()?.headPoints?.length)return;
 remember();active().headPoints=active().headPoints.slice(0,-1);refresh();
};
function changed(o){o.headPoint=null;o.confirmed=false;o.token=null;o.candidates=[];o.area=area(o.mask);refresh();draw()}
function frameMetricsRoi(id){return state.chamber_mode?objects.get(id).roi:roi;}
function frameMetricsContext(id){return JSON.stringify([state.frame,id,frameMetricsRoi(id),$('#pixel-mode').checked,$('#scale').value,calibrationPending]);}
function currentFrameMetrics(id=selected){
 const o=objects.get(id);
 return o?.confirmed&&o.frameMetricsContext===frameMetricsContext(id)?o.frameMetrics:null;
}
function frameMetricsIds(){
 if(step===3&&state.chamber_mode)return [...objects.keys()].sort((a,b)=>a-b);
 return objects.has(selected)?[selected]:[];
}
function visibleFrameMetricsIds(ids=frameMetricsIds()){
 const choice=$('#annotation-metrics-chamber').value;
 if(ids.length<2||choice==='all')return ids;
 const id=Number(choice);
 return [ids.includes(id)?id:selected];
}
function frameMetricsScaleReady(){return $('#pixel-mode').checked||(!calibrationPending&&Number($('#scale').value)>0);}
function metricCell(tag,text,scope){
 const cell=document.createElement(tag);cell.textContent=text;
 if(scope)cell.scope=scope;
 return cell;
}
function refreshFrameMetrics(){
 $('#annotation-metrics-panel').hidden=step===1;
 if(step===1)return;
 const chambers=frameMetricsIds(),ids=visibleFrameMetricsIds(chambers),compare=ids.length>1;
 const picker=$('#annotation-metrics-chamber'),canChoose=chambers.length>1;
 $('#annotation-metrics-picker').hidden=!canChoose;
 if(canChoose){
  if(picker.options.length!==chambers.length+1||chambers.some((id,i)=>picker.options[i].value!==String(id))){
   const options=chambers.map(id=>new Option('槽 '+id,String(id)));
   picker.replaceChildren(...options,new Option('全部槽对比','all'));
  }
  picker.value=compare?'all':String(ids[0]);
 }
 $('#annotation-metrics-frame').textContent=canChoose?`F${state.frame}`:`F${state.frame} · ${chamberLabel(selected)}`;
 const results=ids.map(id=>currentFrameMetrics(id));
 const available=results.filter(Boolean),schema=available[0]?.metrics||[];
 const canMeasure=ids.some(id=>objects.get(id).confirmed);
 const button=$('#calculate-frame-metrics');
 button.disabled=busy||saved||!canMeasure||!frameMetricsScaleReady()||!!poly.length;
 button.textContent=(available.length?'重新计算':'计算')+(compare?'各槽指标':canChoose?'本槽指标':'当前帧指标');
 const header=$('#annotation-metrics-columns'),body=$('#annotation-metrics-values');
 header.replaceChildren();body.replaceChildren();
 $('#annotation-metrics-table').hidden=!schema.length;
 if(compare&&schema.length){
  const row=document.createElement('tr');row.append(metricCell('th','指标','col'));
  for(const id of ids)row.append(metricCell('th','槽 '+id,'col'));
  header.append(row);
 }
 for(const metric of schema){
  const row=document.createElement('tr'),label=metric.label.replace('当前帧','');
  const name=metricCell('th',label,'row');
  if(compare&&metric.unit)name.append(metricCell('small',metric.unit));
  row.append(name);
  for(const result of results){
   const item=result?.metrics.find(entry=>entry.key===metric.key);
   const valid=Number.isFinite(item?.value);
   const number=valid?item.value.toLocaleString('zh-CN',{maximumFractionDigits:3}):'—';
   const cell=metricCell('td',number+(valid&&!compare&&item.unit?' '+item.unit:''));
   cell.title=item?.note||'';row.append(cell);
  }
  body.append(row);
 }
 const notes=[];
 for(const [index,id] of ids.entries()){
  const o=objects.get(id);
  const error=o.frameMetricsContext===frameMetricsContext(id)?o.frameMetricsError:'';
  const note=!o.confirmed?'轮廓待确认':error||results[index]?.note;
  if(note)notes.push((compare?'槽 '+id+' · ':'')+note);
 }
 const status=$('#annotation-metrics-status');
 status.textContent=notes.join('\n')||(!canMeasure?'确认虫体轮廓后自动计算。':!frameMetricsScaleReady()?'请先完成尺寸标定，或选择像素单位。':busy&&available.length<ids.length?'正在计算标注帧指标…':compare?`${available.length} / ${ids.length} 个槽已有测量值`:available.length?'白线为本帧中线。':'请计算当前帧指标。');
}
async function calculateFrameMetrics({missingOnly=false}={}){
 if(busy||saved||poly.length||!frameMetricsScaleReady())return;
 const ids=visibleFrameMetricsIds().filter(id=>objects.get(id).confirmed&&(!missingOnly||!currentFrameMetrics(id)));
 if(!ids.length)return;
 setBusy(true);
 try{
  for(const id of ids){
   const o=objects.get(id),context=frameMetricsContext(id),pixels=$('#pixel-mode').checked;
   let data=null,error='';
   try{
    data=await annotationPost('/api/annotation-metrics',{frame:state.frame,worm_id:id,confirmed:true,
     mask:encodedMask(o.mask),roi:frameMetricsRoi(id),measurement_mode:pixels?'pixels':'physical',
     scale_um_per_px:pixels?null:Number($('#scale').value)});
   }catch(e){error=e.message;}
   if(objects.get(id)!==o||!o.confirmed||frameMetricsContext(id)!==context)continue;
   Object.assign(o,{frameMetrics:data,frameMetricsContext:context,frameMetricsError:error});
   refreshFrameMetrics();draw();
  }
 }finally{setBusy(false);}
}
$('#annotation-metrics-chamber').onchange=()=>{
 refreshFrameMetrics();draw();
 return calculateFrameMetrics({missingOnly:true});
};
$('#calculate-frame-metrics').onclick=calculateFrameMetrics;
function area(c){let n=0;const a=c.getContext('2d').getImageData(0,0,W,H).data;for(let i=3;i<a.length;i+=4)if(a[i]>0)n++;return n}
function loadImage(im,url){return new Promise((resolve,reject)=>{im.onload=()=>resolve();im.onerror=()=>reject(Error('图像加载失败，请刷新页面重试'));im.src=url;if(im.complete&&im.naturalWidth)resolve()})}
const maskPNGs=new WeakMap();
function encodedMask(canvas){
 if(!maskPNGs.has(canvas))maskPNGs.set(canvas,canvas.toDataURL('image/png'));
 return maskPNGs.get(canvas);
}
function editMask(canvas){
 maskPNGs.delete(canvas);
 return canvas.getContext('2d');
}
async function decode(url){
 const im=new Image();await loadImage(im,url);
 const c=empty();c.getContext('2d').drawImage(im,0,0);
 if(url.startsWith('data:image/png;base64,')&&im.naturalWidth===c.width&&im.naturalHeight===c.height)maskPNGs.set(c,url);
 return c;
}
function remember(ids=[selected],structure=false){
 const before=Array.from(ids,id=>{
  const o=objects.get(id);
  return [id,{...o,points:o.points.slice(),mask:encodedMask(o.mask)}];
 });
 // Geometry, candidate lists and measurements are replaced as values. Only
 // the prompt list and canvas are edited in place and need independent copies.
 history.push({selected,roi,before,order:structure?[...objects.keys()]:null});
 if(history.length>12)history.shift();
 draftSaver?.mark();$('#undo').disabled=false;
}
async function undo(){
 if(busy||saved)return;
 if(poly.length){poly.pop();draftSaver?.mark();draw();return}
 const prev=history.at(-1);if(!prev)return;
 setBusy(true);
 try{
  const restored=await Promise.all(prev.before.map(async([id,o])=>{
   const canvas=objects.get(id)?.mask;
   return [id,{...o,mask:canvas&&maskPNGs.get(canvas)===o.mask?canvas:await decode(o.mask)}];
  }));
  for(const [id,o] of restored)objects.set(id,o);
  if(prev.order)objects=new Map(prev.order.map(id=>[id,objects.get(id)]));
  selected=prev.selected;roi=prev.roi;poly=[];history.pop();
  say('已撤销上一步，掩膜与提示点一起恢复。');
 }catch(e){say(e.message,'error')}finally{setBusy(false);refresh();draw()}
}
function selectObject(id){
 selected=id;
 if(state.chamber_mode)roi=active()?.roi||null;
 poly=[];
}
function chamberLabel(id){
 return state.chamber_mode?'培养槽 '+id:'线虫 '+String(id).padStart(2,'0');
}
const chamberNumber=$('#chamber-number');
chamberNumber.onchange=()=>{
 const id=chamberNumber.valueAsNumber;
 if(!state?.chamber_mode || busy || saved || id===selected)return;
 if(!Number.isInteger(id)||id<1||id>10000||objects.has(id)){
  chamberNumber.value=selected;say('请输入未使用的培养槽编号（1 至 10000）。');return;
 }
 remember([selected],true);
 objects=new Map([...objects].map(([key,value])=>[key===selected?id:key,value]));
 selected=id;active().token=null;active().candidates=[];refresh();draw();
};
chamberNumber.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();chamberNumber.blur();}};
function add(rememberFirst=true){let id=objects.size?Math.max(...objects.keys())+1:1;
 if(state.chamber_mode&&id>10000){id=1;while(objects.has(id)&&id<=10000)id++;if(id>10000){say('培养槽编号已用完');return;}}
 if(rememberFirst)remember([],true);objects.set(id,{mask:empty(),points:[],box:null,token:null,candidate:0,candidates:[],confirmed:false,area:0,headPoint:null});selectObject(id);setMode(state.chamber_mode?'roi':'sam_positive');refresh();draw();if(state.chamber_mode){say(`请拖动框选培养槽 ${id}，覆盖虫体的活动范围。`);return}say(`已添加线虫 ${id}，请点击它的身体。`)}
function setBusy(value){busy=value;refresh();$('#session-status').textContent=value?'正在计算…':saved?'已保存':'本地标注'}
function refreshRegionChoice(){
 const cropped=!!roi||mode==='roi';
 const crop=$('[data-mode="roi"]'),full=$('#reset-roi');
 crop.classList.toggle('selected',cropped);crop.classList.toggle('active',cropped);
 full.classList.toggle('selected',!cropped);
 crop.setAttribute('aria-pressed',String(cropped));full.setAttribute('aria-pressed',String(!cropped));
 $('#roi-info').hidden=!roi;
 $('#roi-info').textContent=roi?`${roi[2]-roi[0]} × ${roi[3]-roi[1]} px · 从 (${roi[0]}, ${roi[1]}) 开始`:'';
}
let spaceHeld=false,handTool=false;
function restingCursor(){return (spaceHeld&&!drag)||mode==='view'?'grab':'crosshair';}
function setMode(value,hand=false){
 if(state?.chamber_mode&&step===2&&!roi&&!['roi','view'].includes(value))value='roi';
 handTool=hand;
 mode=value;pendingTap=null;poly=[];drag=null;boxDrag=null;
 $$('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
 $$('.scale-tools button,.tool[data-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.mode===mode)));
 const navigating=step===2&&mode==='view';
 $('#mode-label').textContent=navigating?'拖动画面':modes[mode][0];
 $('#mode-hint').textContent=navigating?'':step===3&&mode==='view'?'检查标注，准备开始分析':modes[mode][1];
 canvas.style.cursor=restingCursor();
 refreshRegionChoice();draw();
}
function canReturnToSetup(){
 return !!(state?.can_edit_mode && window.parent!==window && window.parent.StudioWorkspace?.editAnnotationSetup);
}
function showStep(value){
 if(busy||saved)return;
 if(state?.mask_correction&&value===1)value=2;
 if(value===3&&step!==3)$('#annotation-metrics-chamber').value=String(selected);
 step=value;
 WorkspaceLinks.setPanelOpen($('.sidebar'),true);
 $$('[data-panel]').forEach(e=>e.hidden=+e.dataset.panel!==step);
 $$('[data-step]').forEach(e=>{
  e.classList.toggle('active',+e.dataset.step===step);
  e.classList.toggle('done',+e.dataset.step<step);
 });
 const previous=$('#previous');
 previous.hidden=(step===1&&!canReturnToSetup())||(state?.mask_correction&&step===2);
 previous.textContent=step===1?'← 上一步：标注设置':'上一步';
 $('#next').hidden=step===3;
 $('#save').hidden=step!==3;
 $('#next').innerHTML=(step===1?'下一步：点选线虫':'下一步：确认并开始')+' <span>→</span>';
 setMode(step===2?(state.chamber_mode&&!roi?'roi':'sam_positive'):step===1&&!$('#pixel-mode').checked?'scale_auto':'view');
 ($('.sidebar>.floating-content')||$('.sidebar')).scrollTop=0;
 refresh();draw();
 if(step===3&&state.chamber_mode)void calculateFrameMetrics({missingOnly:true});
 if(matchMedia('(max-width:760px)').matches){
  if(step===3)$('.sidebar').scrollIntoView({block:'start'});
  else $('.workspace').scrollTop=0;
 }
}
function refresh(){
 document.body.classList.toggle('pixel-units',$('#pixel-mode').checked);
 $('#physical-mode').checked=!$('#pixel-mode').checked;
 if(!state)return;
 $('#tracking-contrast').disabled=busy||saved||!!state.mask_correction;
 if(!busy&&!saved)draftSaver?.mark();
 $('#chamber-number-field').hidden=!state.chamber_mode;chamberNumber.disabled=busy||saved;
 if(document.activeElement!==chamberNumber)chamberNumber.value=selected;
 const list=$('#worm-list');list.replaceChildren();
 for(const [id,o] of objects){
  const row=document.createElement('div');
  row.className='worm-row'+(id===selected?' selected':'');
  const select=document.createElement('button');
  select.className='worm-select';
  select.setAttribute('aria-pressed',String(id===selected));
  const detail=!state.chamber_mode&&o.area?`${o.area.toLocaleString()} px · ${o.confirmed?'已确认':'待确认'}`:o.confirmed?'已确认':o.area?'待确认':state.chamber_mode&&!o.roi?'待框选':'待点选虫体';
  select.innerHTML=`<i class="worm-color" style="background:${color(id)}"></i><span><span class="worm-name">${chamberLabel(id)}</span><span class="worm-detail">${detail}</span></span>${o.confirmed?'<span class="worm-check">✓</span>':''}`;
  select.onclick=()=>{
   if(busy||saved)return;
   selectObject(id);
   setMode(state.chamber_mode&&!roi?'roi':'sam_positive');
   refresh();draw();
  };
  const remove=document.createElement('button');
  remove.className='remove-worm';remove.textContent='×';
  remove.title='移除'+chamberLabel(id)+'（可撤销）';
  remove.setAttribute('aria-label',remove.title);
  remove.onclick=()=>{
   if(busy||saved)return;
   remember([id],true);objects.delete(id);
   if(!objects.size)add(false);
   else if(selected===id)selectObject(objects.keys().next().value);
   setMode(state.chamber_mode&&!roi?'roi':'sam_positive');refresh();draw();
  };
  row.append(select,remove);list.append(row);
 }
 const o=active();
 const frames=[...(o?.headPoints||[]).map(a=>a.frame),...(o?.headPoint?[state.frame]:[])].sort((a,b)=>a-b);
 $('#head-info').textContent=frames.length?'已标记头端 · '+frames.map(f=>'F'+f).join('、'):'头端未标记，可在当前帧或随机抽帧标记。';
 $('#sample-heads').hidden=!!state.mask_correction;
 const lastSample=o?.headPoints?.at(-1);
 $('#remove-sampled-head').hidden=!lastSample||!!state.mask_correction;
 if(lastSample)$('#remove-sampled-head').textContent='移除 F'+lastSample.frame+' 的标记';
 $('#review-heads').textContent=`${[...objects.values()].filter(w=>w.headPoint||w.headPoints?.length).length} / ${objects.size} 条已人工标记头部`;
 const confirmed=[...objects.values()].filter(o=>o.area>=20&&o.confirmed).length;
 $('#worm-count').textContent=objects.size;$('#view-title').textContent=step===2?`${state.mask_correction?'修正源帧 '+state.frame:'正在标注'} · 线虫 ${String(selected).padStart(2,'0')}`:`标注帧 ${state.frame}`;
 if(state.sequence_total)$('#view-title').textContent=`标定 ${state.sequence_index}/${state.sequence_total} · ${state.video_name}`;
 const r=roi||[0,0,W,H];refreshRegionChoice();
 $('#review-worms').textContent=`${confirmed} / ${objects.size} 条已确认`;$('#review-roi').textContent=`${r[2]-r[0]} × ${r[3]-r[1]} px`;$('#review-scale').textContent=$('#pixel-mode').checked?'仅像素单位（未标定）':+$('#scale').value>0?`${Number((+$('#scale').value).toPrecision(6))} μm/px`:'尚未标定';
 const check=readiness(),{algorithmIssue,ready}=check;
 $('#analysis-status').textContent=check.methodReady?'分析准备已完成，保存后自动处理。':algorithmIssue;
 $('#retry-analysis').hidden=analysisMethod()!=='body_sequence'||!!state.algorithm?.checking||state.algorithm?.available===true;
 $('#add').hidden=!state.chamber_mode;
 if(state.chamber_mode){
  $('#add').textContent='＋ 添加培养槽';
  $('#worm-count').textContent=`已确认 ${confirmed}/${objects.size} 槽`;
  $('#view-title').textContent=`${state.video_name} · 培养槽 ${selected}`;
  $('#review-worms').textContent=`${confirmed} / ${objects.size} 个培养槽已确认`;
  $('#review-roi').textContent=`${objects.size} 个独立槽区`;
 }
 const candidates=$('#candidates');candidates.replaceChildren();$('#candidates-panel').hidden=!o||o.candidates.length<2;
 if(o)for(const c of o.candidates){const b=document.createElement('button');b.textContent=`候选 ${c.index+1}`;b.classList.toggle('selected',o.candidate===c.index);b.onclick=async()=>{if(busy||saved)return;remember();setBusy(true);try{o.mask=await decode(c.mask);o.candidate=c.index;o.area=c.area_px;o.headPoint=null;o.confirmed=false;say(`已切换到候选 ${c.index+1}，请检查完整虫体。`)}finally{setBusy(false);draw()}};candidates.append(b)}
 $$('.sidebar .floating-content button,.sidebar input,.sidebar select,.workflow button,.footer-actions button').forEach(e=>e.disabled=busy||saved);
 $('#anchor-prev').disabled=busy||saved||!!state.mask_correction||state.frame===0;
 $('#anchor-next').disabled=busy||saved||!!state.mask_correction||state.frame===state.frame_count-1;
 $('#retry-analysis').disabled=busy||saved||!!state.algorithm?.checking;
 const needsConfirmation=step===2&&!o?.confirmed;
 $('#confirm-worm').hidden=step!==2;$('#confirm-worm').disabled=busy||saved||!o||o.area<20||!!o.confirmed;
 $('#confirm-worm').textContent=`${o?.confirmed?'✓ 已确认':'确认虫体'} · 线虫 ${String(selected).padStart(2,'0')}`;
 if(state.chamber_mode)$('#confirm-worm').textContent=(o?.confirmed?'已确认':'确认')+'槽 '+selected+' 的虫体';
 else if(state.sequence_total && !o?.confirmed){
   $('#confirm-worm').textContent=state.sequence_index<state.sequence_total?'确认虫体并标定下一个':'确认虫体并开始顺序处理';
 }
 $('#confirm-worm').classList.toggle('primary',needsConfirmation);$('#confirm-worm').classList.toggle('secondary',!needsConfirmation);
 $('#next').classList.toggle('primary',!needsConfirmation);$('#next').classList.toggle('secondary',needsConfirmation);
 if(state?.sequence_total)$('#save').textContent=state.sequence_index<state.sequence_total?'确认并标定下一个':'确认并开始顺序处理';
 else if(state?.batch_deferred)$('#save').textContent='保存标注并加入队列';
 $('#save').disabled=busy||saved||!ready;$('#undo').disabled=busy||saved||!history.length;
 $('#predict-sam').disabled=busy||saved||!(o?.points.some(p=>p.label===1)||o?.box);
 if(state.chamber_mode){
  const last=state.sequence_index===state.sequence_total;
  $('#save').textContent=last?(state.sequence_total===1?`分析 ${objects.size} 个培养槽`:'保存并开始批量分析'):'保存本视频，标注下一个';
  $('#add').disabled=busy||saved||!o?.confirmed;
  $('#reset-roi').disabled=true;
 }
 $('#next').disabled=busy||saved||(step===1&&!check.scaleReady)||(step===2&&!check.masksReady);
 if(state.mask_correction){
  $$('[data-step="1"],#add,.remove-worm,#load-anchor,#anchor-frame,#reset-roi,[data-mode="roi"],#pixel-mode,#physical-mode,#scale,#scale-length,.scale-tools button').forEach(el=>el.disabled=true);
 }
 refreshGuidance(check);
 refreshFrameMetrics();
}
let queued=false;
function draw(){if(!W||queued)return;queued=true;requestAnimationFrame(()=>{queued=false;render()})}
function render(){
 ctx.clearRect(0,0,W,H);const tmp=scratch.getContext('2d');
 if($('#show-mask').checked)for(const [id,o] of objects){tmp.clearRect(0,0,W,H);tmp.globalCompositeOperation='source-over';tmp.drawImage(o.mask,0,0);tmp.globalCompositeOperation='source-in';tmp.fillStyle=color(id);tmp.fillRect(0,0,W,H);tmp.globalCompositeOperation='source-over';ctx.globalAlpha=+$('#opacity').value/100*(step===2&&id!==selected ? .65 : 1);ctx.drawImage(scratch,0,0)}
 ctx.globalAlpha=1;const z=Math.max(.08,zoom);ctx.lineWidth=1.5/z;
 if(step!==1)for(const id of visibleFrameMetricsIds()){
  const measurement=currentFrameMetrics(id);
  if(!measurement?.centerline?.length)continue;
  ctx.beginPath();ctx.moveTo(...measurement.centerline[0]);
  for(const point of measurement.centerline.slice(1))ctx.lineTo(...point);
  ctx.strokeStyle='#fff';ctx.lineWidth=1.5/z;ctx.stroke();
  if(measurement.body_center){ctx.beginPath();ctx.arc(...measurement.body_center,3/z,0,Math.PI*2);ctx.fillStyle='#ffd06a';ctx.fill();}
 }
 if(state.chamber_mode){
  ctx.font=`bold ${13/z}px sans-serif`;
  ctx.textAlign='left';ctx.textBaseline='top';
  for(const [id,worm] of objects){
   const bounds=id===selected?roi:worm.roi;
   if(!bounds)continue;
   const [x,y,x1,y1]=bounds;
   ctx.strokeStyle=ctx.fillStyle=color(id);
   ctx.strokeRect(x,y,x1-x,y1-y);
   const label='槽 '+id+(worm.confirmed?' ✓':'');
   ctx.fillRect(x,y,ctx.measureText(label).width+8/z,19/z);
   ctx.fillStyle='#152b23';ctx.fillText(label,x+4/z,y+2/z);
  }
 }else if(roi){ctx.strokeStyle='#6cb7fa';ctx.strokeRect(roi[0],roi[1],roi[2]-roi[0],roi[3]-roi[1])}
 const o=active();if(step===2&&o){const box=boxDrag||o.box;if(box){ctx.strokeStyle='#ffe097';ctx.strokeRect(box[0],box[1],box[2]-box[0],box[3]-box[1])}for(const p of o.points){ctx.beginPath();ctx.arc(p.x,p.y,4/z,0,Math.PI*2);ctx.fillStyle=p.label?'#59e3ac':'#ff8b97';ctx.fill();ctx.strokeStyle='#fff';ctx.lineWidth=1.3/z;ctx.stroke();ctx.fillStyle='#243642';ctx.font=`${9/z}px sans-serif`;ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(p.label?'+':'−',p.x,p.y)}}
 for(const [id,worm] of objects){if(!worm.headPoint)continue;const [x,y]=worm.headPoint;ctx.beginPath();ctx.arc(x,y,7/z,0,Math.PI*2);ctx.strokeStyle='#fff';ctx.lineWidth=2/z;ctx.stroke();ctx.fillStyle=color(id);ctx.font=`bold ${14/z}px sans-serif`;ctx.textAlign='left';ctx.textBaseline='bottom';ctx.fillText(`H${id}`,x+10/z,y-8/z)}
 if(poly.length){ctx.beginPath();ctx.moveTo(...poly[0]);poly.slice(1).forEach(p=>ctx.lineTo(...p));ctx.strokeStyle=color(selected);ctx.stroke()}
 if(scalePoints.length && !$('#pixel-mode').checked){
  ctx.beginPath();ctx.moveTo(...scalePoints[0]);scalePoints.slice(1).forEach(p=>ctx.lineTo(...p));
  ctx.strokeStyle='#006653';ctx.lineWidth=1.5/z;ctx.stroke();
  for(const [x,y] of scalePoints){
   ctx.beginPath();ctx.arc(x,y,5/z,0,Math.PI*2);
   ctx.strokeStyle='white';ctx.lineWidth=3/z;ctx.stroke();
   ctx.strokeStyle='#006653';ctx.lineWidth=1.5/z;ctx.stroke();
   ctx.beginPath();ctx.arc(x,y,1.5/z,0,Math.PI*2);ctx.fillStyle='#006653';ctx.fill();
  }
 }
}
const stage=$('#stage');
let panX=0,panY=0;
function resetPan(){
 panX=panY=0;
 stage.style.transform='';
}
function fit(){
 if(!W)return;
 resetPan();
 const fitting=$('#zoom').value==='fit', style=getComputedStyle(viewport);
 const width=Math.max(1,viewport.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)-2);
 const height=Math.max(1,viewport.clientHeight-parseFloat(style.paddingTop)-parseFloat(style.paddingBottom)-2);
 zoom=fitting?Math.min(width/W,height/H):Math.max(.04,+$('#zoom').value);
 base.style.width=canvas.style.width=`${W*zoom}px`;base.style.height=canvas.style.height=`${H*zoom}px`;draw();
}
function point(e){return AnnotationCoordinates.point(e.clientX,e.clientY,canvas.getBoundingClientRect(),W,H)}
function inside(p){if(state?.chamber_mode&&!roi)return false;return !roi||(p[0]>=roi[0]&&p[0]<roi[2]&&p[1]>=roi[1]&&p[1]<roi[3])}
async function predict(){
 const o=active();if(busy||saved||!o)return;if(!o.points.some(p=>p.label===1)&&!o.box){say('先在虫体上点一个点，或框住整条虫。');return}
 setBusy(true);say(o.token?`正在修正线虫 ${selected} 的轮廓…`:`正在分割线虫 ${selected}，首次点选需要加载模型…`,'working');
 const token=state.token, signal=keyListeners.signal;
 try{
  const {job}=await annotationPost('/api/prediction',{
   worm_id:selected,roi,sam2_input_contrast:$('#tracking-contrast').value,points:o.points,box:o.box,prediction_token:o.token,previous_candidate:o.candidate
  });
  const url='/api/prediction?job='+encodeURIComponent(job);
  while(!signal.aborted){
   const response=await fetch(url,{headers:{'X-Annotation-Token':token},cache:'no-store',signal});
   const data=await response.json();
   if(!response.ok)throw Error(data.error||`HTTP ${response.status}`);
   if(response.status===202){
    await new Promise(resolve=>setTimeout(resolve,250));
    continue;
   }
   const mask=await decode(data.mask);
   if(signal.aborted||state.token!==token||o!==active()||saved)return;
   Object.assign(o,{mask,area:data.area_px,token:data.prediction_token,candidate:data.candidate,
    candidates:data.candidates,headPoint:null,confirmed:false});
   say(data.refined?'已在上一轮轮廓基础上修正。检查完整虫体后，确认这条虫。':'已生成虫体轮廓。可追加点、切换候选或手工修正。');
   break;
  }
 }
 catch(e){if(!signal.aborted)say(`分割失败：${e.message}。可以撤销此次点选后重试。`,'error')}
 finally{setBusy(false);draw()}
}
function stroke(a,b){const c=editMask(active().mask);c.save();if(roi){c.beginPath();c.rect(roi[0],roi[1],roi[2]-roi[0],roi[3]-roi[1]);c.clip()}c.globalCompositeOperation=mode==='erase'?'destination-out':'source-over';c.lineWidth=+$('#brush').value;c.lineCap='round';c.lineJoin='round';c.strokeStyle='white';c.fillStyle='white';c.beginPath();c.moveTo(...a);c.lineTo(...b);c.stroke();c.beginPath();c.arc(b[0],b[1],c.lineWidth/2,0,Math.PI*2);c.fill();c.restore();draw()}
canvas.onpointerdown=e=>{
 if(busy||saved||!state||mode==='view'||e.button!==0)return;const p=point(e),o=active();
 if(mode==='head'){if(!inside(p)||!o||o.area<20){say('请先生成当前线虫的掩膜，再点击头端。','error');return}remember();o.headPoint=p;refresh();draw();say(`线虫 ${selected} 的头部已标记；处理时将检查与中线端点是否匹配。`);return}
 if(isScaleTool(mode)){pickScale(p);return}
 if(mode==='polygon'){draftSaver?.mark();if(!inside(p)){say('请在分析区域内绘制。');return}poly.push(p);draw();return}
 if(mode==='roi'&&(state.chamber_mode?o.area:[...objects.values()].some(x=>x.area))){say('已有虫体掩膜。更改分析区域前请清空或移除已标注虫体。','error');return}
 if((mode==='paint'||mode==='erase')&&!inside(p))return;
 canvas.setPointerCapture(e.pointerId);drag=p;last=p;
 remember(mode==='roi'&&!state.chamber_mode?objects.keys():[selected]);
 if(mode==='paint'||mode==='erase')stroke(p,p);
};
canvas.onpointermove=e=>{if(!state)return;const p=point(e);$('#cursor-position').textContent=`X ${p[0]}  ·  Y ${p[1]}`;if(!drag||busy||saved)return;if(mode==='roi'){roi=[Math.min(drag[0],p[0]),Math.min(drag[1],p[1]),Math.max(drag[0],p[0])+1,Math.max(drag[1],p[1])+1];draw()}else if(mode==='sam_box'){boxDrag=[Math.min(drag[0],p[0]),Math.min(drag[1],p[1]),Math.max(drag[0],p[0])+1,Math.max(drag[1],p[1])+1];draw()}else{stroke(last,p);last=p}};
canvas.onpointerup=e=>{if(!drag)return;const p=point(e),rect=[Math.min(drag[0],p[0]),Math.min(drag[1],p[1]),Math.max(drag[0],p[0])+1,Math.max(drag[1],p[1])+1];drag=null;last=null;boxDrag=null;
 if(mode==='roi'){if(rect[2]-rect[0]<32||rect[3]-rect[1]<32){void undo();say('分析区域至少需要 32 × 32 像素。','error')}else{roi=rect;if(state.chamber_mode){active().roi=rect;if(step===2)setMode('sam_positive')}for(const o of state.chamber_mode?[active()]:objects.values()){o.points=[];o.box=null;o.token=null}say('分析区域已更新。');refresh()}}
 else if(mode==='sam_box'){if(inside([rect[0],rect[1]])&&inside([rect[2]-1,rect[3]-1])&&(rect[2]-rect[0])*(rect[3]-rect[1])>=16){active().box=rect;active().token=null;active().confirmed=false;void predict()}else say('请在分析区域内框住虫体。','error')}
 else changed(active());draw();
};
canvas.onpointercancel=()=>{if(drag&&(mode==='paint'||mode==='erase'))changed(active());drag=last=boxDrag=null;refresh();draw()};
$$('[data-mode]').forEach(button=>button.onclick=()=>{
 if(busy||saved)return;
 const tool=button.dataset.mode;
 if(isScaleTool(tool))startCalibration(tool);
 else setMode(isPromptTool(tool)&&tool===mode?'view':tool);
});
$$('[data-step]').forEach(b=>b.onclick=()=>{repairReturnStep=null;showStep(+b.dataset.step)});
$('#next').onclick=()=>{if(busy||saved||$('#next').disabled)return;const target=repairReturnStep||Math.min(3,step+1);repairReturnStep=null;showStep(target)};$('#previous').onclick=async()=>{
 if(busy||saved)return;
 repairReturnStep=null;
 if(step>1){showStep(step-1);return;}
 if(canReturnToSetup()){
  try{await window.parent.StudioWorkspace.editAnnotationSetup();}
  catch(error){say(error.message,'error');}
 }
};$('#add').onclick=()=>{if(!busy&&!saved)add()};$('#undo').onclick=undo;
$('#confirm-worm').onclick=async()=>{
 if(busy||saved||active().area<20)return;
 if(state.sequence_total && poly.length){say('请先闭合或取消多边形。','error');return;}
 if(!active().confirmed)remember();
 active().frameMetricsContext=null;active().confirmed=true;refresh();
 await calculateFrameMetrics();
 if(state.chamber_mode){
   say(`培养槽 ${selected} 已确认。可添加下一个槽，或点下一步提交。`);
 }else if(state.sequence_total){
   showStep(3);
   await saveAnnotation();
 }else{
   say(`线虫 ${selected} 已确认。可点击下一步，确认并开始分析。`);
   $('#annotation-metrics-panel').scrollIntoView({block:'nearest'});
 }
};
$('#predict-sam').onclick=()=>{if(busy||saved)return;remember();active().token=null;void predict()};
$('#clear-head').onclick=()=>{if(busy||saved)return;remember();active().headPoint=null;refresh();draw();say('已清除当前线虫的头部点。')};
$('#retry-analysis').onclick=()=>checkOptionalMethods(true);
$('#clear-sam').onclick=()=>{if(busy||saved)return;remember();Object.assign(active(),{points:[],box:null,token:null,candidates:[]});refresh();draw();say('提示点已清除，当前掩膜保留。')};
$('#clear').onclick=()=>{if(busy||saved)return;remember();Object.assign(active(),{mask:empty(),points:[],box:null,headPoint:null});changed(active());say('已清空当前虫，其他虫体保留。')};
$('#close-poly').onclick=()=>{if(busy||saved)return;if(poly.length<3){say('至少选三个多边形顶点。');return}remember();const c=editMask(active().mask);c.fillStyle='white';c.beginPath();c.moveTo(...poly[0]);poly.slice(1).forEach(p=>c.lineTo(...p));c.closePath();c.fill();poly=[];changed(active());say('已填充多边形，请确认掩膜。')};
$('#reset-roi').onclick=()=>{if(busy||saved)return;remember();roi=null;setMode('view');refresh();draw();say('已使用完整画面。')};
$('#zoom').onchange=fit;$('#show-mask').onchange=draw;$('#opacity').oninput=draw;$('#brush').oninput=()=>$('#brush-value').textContent=`${$('#brush').value} px`;
$('#scale-length').onfocus=()=>{if(step===1&&!busy&&!saved&&!isScaleTool(mode))setMode('scale_auto');};
$('#scale').oninput=()=>{calibration=null;calibrationPending=false;scalePoints=[];$('#calibration-info').textContent='当前使用手动填写的比例。如需测量，请重新选取标尺两端。';if(isScaleTool(mode))setMode('view');refresh();draw()};$('#scale-length').oninput=()=>{if(scalePoints.length||calibrationPending)updateCalibration()};

$('#save').onclick=saveAnnotation;
async function saveAnnotation(){if(busy||saved)return;const check=readiness();if(!check.ready){refresh();resolveBlocker(check.blockers[0]);return}if(poly.length){say('请先闭合或取消多边形。','error');return}setBusy(true);say(state.batch_deferred?'正在保存标注并加入队列…':'正在保存标注并准备分析…','working');try{await draftSaver?.flush();const method=analysisMethod();const response=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':state.token},body:JSON.stringify({[state.chamber_mode?'chambers':'worms']:[...objects].map(([id,o])=>({id,roi:o.roi,mask:encodedMask(o.mask),head_point:o.headPoint||null,head_points:o.headPoints||[]})),roi,sam2_input_contrast:$('#tracking-contrast').value,measurement_mode:$('#pixel-mode').checked?'pixels':'physical',scale_um_per_px:$('#pixel-mode').checked?null:+$('#scale').value,calibration,analysis_profile:'current',centerline_method:method,head_detection:CURRENT_HEAD_DETECTION,provenance:'User-clicked SAM2 masks, individually reviewed and optionally brush-edited in Worm Studio'})});const data=await response.json();if(!response.ok)throw Error(data.error);saved=true;draftSaver?.stop();if(data.next_annotation){allowLeave=true;window.location.replace('/');return}if(data.queued){if(window.parent!==window){window.parent.postMessage({type:'annotation-queued'},location.origin);say('标注已加入队列，请选择下一个视频继续标定。');}else{location.assign('/studio');}return}if(data.progress_url){window.location.assign('/progress');return}$('#success-summary').textContent=`${data.worm_count} 条线虫 · 自动分析`;$('#saved-path').textContent=data.path;$('#success').hidden=false;$('#save-state').style.background='#35ac87';say(`已保存 ${data.worm_count} 条虫，正在准备分析。`);$('#close-success').focus()}catch(e){say(`保存失败：${e.message}`,'error')}finally{setBusy(false)}};
$('#close-success').onclick=()=>$('#success').hidden=true;
document.addEventListener('keydown',e=>{if($('#head-sampler').open||e.target.matches('input,select,textarea')||busy||saved)return;if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();void undo()}else if(step===2&&['1','2','3'].includes(e.key)){e.preventDefault();setMode({'1':'sam_positive','2':'sam_negative','3':'erase'}[e.key])}else if(e.key==='Escape'){poly=[];setMode('view');draftSaver?.mark()}});
new ResizeObserver(fit).observe(viewport);
async function init(){try{const response=await fetch('/state');if(!response.ok)throw Error('无法读取标注会话');state=await response.json();W=state.width;H=state.height;canvas.width=scratch.width=W;canvas.height=scratch.height=H;$('#video-name').textContent=state.sequence_total ? `标定 ${state.sequence_index}/${state.sequence_total} · ${state.video_name}` : state.video_name;$('#frame-label').textContent=`第 ${state.frame} 帧 / 共 ${state.frame_count} 帧`;$('#dimensions').textContent=`${W} × ${H}`;$('#model-label').textContent='智能分割';$('#scale').value=state.scale_um_per_px||'';$('#pixel-mode').checked=state.measurement_mode==='pixels';$('#anchor-frame').value=state.frame;$('#anchor-frame').max=state.frame_count-1;$('#anchor-position').textContent=`第 ${state.frame} / ${state.frame_count-1} 帧`+(Number.isFinite(state.frame_seconds)?` · ${state.frame_seconds.toFixed(3)} s`:'');await loadImage(base,'/image.png');
 if(state.chamber_mode){
  document.body.classList.add('chamber-annotation');
  $('[data-panel="1"] .intro').textContent='先选择测量单位或完成标尺标定。所有培养槽共用这次标定。';
  $('[data-panel="1"] .section-title .tag').textContent='可在下一步框选';
  $('#slot-heading').textContent='按培养槽标注';
  $('#slot-intro').textContent='按槽号顺序框选每个培养槽，点选槽内的一只虫并确认。';
  $('#reset-roi').hidden=true;
  $('#worm-list').setAttribute('aria-label','培养槽列表');
 }
 const previous=state.previous;
 $('#tracking-contrast').value=previous?.sam2_input_contrast||state.sam2_input_contrast||'none';
 if(previous){$('#pixel-mode').checked=previous.measurement_mode==='pixels';roi=previous.roi;$('#scale').value=previous.scale_um_per_px;calibration=previous.calibration||null;if(calibration){scalePoints=structuredClone(calibration.points);scalePointMethod=calibration.method||'manual';$('#scale-length').value=calibration.length_um;$('#calibration-info').textContent=calibrationText(measureCalibration(scalePoints,calibration.length_um))}for(const w of previous.worms)objects.set(w.id,{mask:await decode(w.mask),points:[],box:null,token:null,candidate:0,candidates:[],confirmed:w.confirmed!==false,area:w.area_px,headPoint:w.head_point||null,headPoints:w.head_points||[]});selected=objects.keys().next().value;state.centerline_method=previous.centerline_method;state.head_detection=previous.head_detection}else add(false);
 $('#loading').hidden=true;showStep(state.mask_correction?2:1);fit();say(state.mask_correction?`已载入源帧 ${state.frame} 的最新已保存掩膜。修正并确认后重新处理，原结果保留。`:previous?'已载入上次保存的掩膜与方法，可以继续检查或修改。':'设置分析区域与尺寸，随后点选每条线虫。');
 if(state.mask_correction){document.body.classList.add("mask-correction");$(".manual-tools").open=true;setMode("paint");$("#save").innerHTML="保存修正并重新处理 <span>→</span>";}
 await initDrafts();
 if(active()?.confirmed&&!(state.chamber_mode&&step===3)){await calculateFrameMetrics({missingOnly:true});if(step!==1)$('#annotation-metrics-panel').scrollIntoView({block:'nearest'});}
 }catch(e){$('#loading').hidden=true;say(`加载失败：${e.message}`,'error')}}
async function checkOptionalMethods(retry=false){
 try{
  if(retry){state.algorithm={available:false,checking:true};refresh();}
  const response=await fetch('/api/algorithms'+(retry?'?retry=1':''),{cache:'no-store'});
  if(!response.ok)throw Error('分析组件状态暂不可用');
  const result=await response.json();state.algorithm=result.algorithm;refresh();
  if(result.algorithm.checking&&!saved)setTimeout(()=>checkOptionalMethods(),1000);
 }catch(e){state.algorithm={available:false,checking:false};refresh()}
}
init().then(()=>{if(state && analysisMethod()==='body_sequence')void checkOptionalMethods()});

function changeMeasurementMode(){
 if($('#pixel-mode').checked){if(isScaleTool(mode))setMode('view');}
 else if(step===1)setMode('scale_auto');
 refresh();draw();
}
$('#pixel-mode').onchange=changeMeasurementMode;
$('#physical-mode').onchange=changeMeasurementMode;

// Durable drafts keep pixel coordinates and never silently become confirmed measurements.
async function annotationPost(path, body) {
 const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Annotation-Token':state.token},body:JSON.stringify(body)});
 const d=await r.json();if(!r.ok)throw Error(d.error||'请求失败');return d;
}
function draftBody() {
 return {roi,selected,step,mode,poly,trackingContrast:$('#tracking-contrast').value,pixelMode:$('#pixel-mode').checked,scale:$('#scale').value,
  calibration,calibrationPending,scalePoints,scalePointMethod,scaleLength:$('#scale-length').value,
  method:analysisMethod(),autoHead:true,headInitMethod:CURRENT_HEAD_DETECTION,
  objects:[...objects].map(([id,o])=>[id,{roi:o.roi,mask:encodedMask(o.mask),points:o.points,box:o.box,confirmed:o.confirmed,area:o.area,headPoint:o.headPoint,headPoints:o.headPoints||[]}])};
}
async function restoreDraft(d) {
 const restored=await Promise.all(d.objects.map(async([id,o])=>[id,{...o,mask:await decode(o.mask),token:null,candidate:0,candidates:[]}]));
 objects=new Map(restored);selected=objects.has(d.selected)?d.selected:objects.keys().next().value;
 roi=d.roi;calibration=d.calibration;scalePointMethod=d.scalePointMethod||calibration?.method||'manual';scalePoints=d.scalePoints||[];calibrationPending=!!d.calibrationPending;
 if(d.trackingContrast&&!state.mask_correction)$('#tracking-contrast').value=d.trackingContrast;
 $('#pixel-mode').checked=d.pixelMode;$('#scale').value=d.scale;$('#scale-length').value=d.scaleLength||d.calibration?.length_um||'';
 // Old drafts restore geometry and units; new analyses always use the current profile.
 showStep([1,2,3].includes(d.step)?d.step:1);if(modes[d.mode])setMode(d.mode);poly=d.poly||[];
 $('#calibration-info').textContent=scalePoints.length||calibrationPending?calibrationText(measureCalibration(scalePoints,$('#scale-length').value)):'点击画面中的标尺线。';
 refresh();draw();say('已自动恢复此视频、此帧的草稿，请复核后开始处理。');
}
async function initDrafts() {
 const indicator=$('#draft-status');
 try {
  const response=await fetch('/api/draft');if(!response.ok)throw Error('无法读取草稿');
  const {body}=await response.json();if(body)await restoreDraft(body);
 } catch(e) {
  // Do not overwrite an unreadable existing draft with an empty initialization.
  indicator.hidden=false;indicator.dataset.state='error';
  indicator.textContent='草稿读取失败，请刷新重试';say(e.message,'error');return;
 }
 draftSaver=createDraftAutosave({capture:draftBody,write:body=>annotationPost('/api/draft',body),onState:({status,error})=>{
  if(status==='error'){
   indicator.dataset.state='error';
   indicator.textContent='草稿保存失败，请点击“保存草稿”重试。';
   indicator.title=error?.message||'';
   indicator.hidden=false;
  }else if(status==='saved'){
   indicator.hidden=true;
   indicator.textContent='';
   indicator.removeAttribute('title');
  }
 }});
}
async function saveDraft() {
 if(saved)return;
 if(busy)throw Error('正在处理标注，请等待完成后再离开。');
 if(!draftSaver)throw Error('草稿尚未就绪，请刷新后重试。');
 await draftSaver.flush();
}
window.AnnotationDraft={flush:saveDraft,setup:()=>canReturnToSetup()&&!saved?{sample:state.sample,token:state.token,chamberMode:!!state.chamber_mode}:null,dirty:()=>!saved&&!allowLeave&&(busy||!!drag||!!boxDrag||!draftSaver||draftSaver.dirty())};
window.addEventListener('beforeunload',event=>{
 if(window.AnnotationDraft.dirty()){event.preventDefault();event.returnValue='';}
});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&!busy)void saveDraft().catch(()=>{});});
$('#tracking-contrast').onchange=()=>{draftSaver?.mark();say('已更改跟踪画面。使用“重新预测”可按新设置生成轮廓。')};
$('#save-draft').onclick=()=>{draftSaver?.mark();saveDraft().then(()=>say('草稿已保存到本机。')).catch(e=>say(e.message,'error'));};
async function switchAnchor(frame){
 if(busy||saved)return;
 try{if(!Number.isInteger(frame)||frame<0||frame>=state.frame_count)throw Error('帧号超出范围');
  if(frame===state.frame)return;
  await saveDraft();if(busy)return;setBusy(true);await annotationPost('/api/annotation-frame',{frame});draftSaver.stop();allowLeave=true;location.reload();
 }catch(e){setBusy(false);say(e.message,'error')}
}
$('#load-anchor').onclick=()=>switchAnchor($('#anchor-frame').valueAsNumber);
$('#anchor-prev').onclick=()=>switchAnchor(state.frame-1);
$('#anchor-next').onclick=()=>switchAnchor(state.frame+1);
$('#pan-tool').onclick=()=>{if(!busy&&!saved)setMode('view',true)};
$('#locate-worm').onclick=()=>{
 if(!state||busy||saved)return;
 const o=active();if(!o?.area){say('请先为当前线虫建立掩膜，再定位。');return;}
 const data=o.mask.getContext('2d').getImageData(0,0,W,H).data;
 let left=W,right=0,top=H,bottom=0;
 for(let y=0;y<H;y++)for(let x=0;x<W;x++)if(data[(y*W+x)*4+3]>127){left=Math.min(left,x);right=Math.max(right,x);top=Math.min(top,y);bottom=Math.max(bottom,y);}
 const target=Math.min(4,Math.max(.04,Math.min(viewport.clientWidth/(right-left+100),viewport.clientHeight/(bottom-top+100))));
 const rect=viewport.getBoundingClientRect();zoomAt(target,rect.left+rect.width/2,rect.top+rect.height/2);
 const image=canvas.getBoundingClientRect();
 panBy(rect.left+viewport.clientWidth/2-image.left-(left+right)/2*zoom,rect.top+viewport.clientHeight/2-image.top-(top+bottom)/2*zoom,true);
 setMode('view');say(`已定位线虫 ${selected}；滚轮缩放，拖动画面平移。`);
};
function zoomAt(value,x,y) {
 const before=canvas.getBoundingClientRect(), px=(x-before.left)/zoom, py=(y-before.top)/zoom;
 resetPan();
 zoom=Math.max(.04,Math.min(6,value));let custom=$('#custom-zoom');
 if(!custom){custom=document.createElement('option');custom.id='custom-zoom';$('#zoom').append(custom)}
 custom.textContent=`${Math.round(zoom*100)}%`;custom.value=String(zoom);$('#zoom').value=String(zoom);
 base.style.width=canvas.style.width=`${W*zoom}px`;base.style.height=canvas.style.height=`${H*zoom}px`;
 const after=canvas.getBoundingClientRect();panBy(x-after.left-px*zoom,y-after.top-py*zoom,true);draw();
}
viewport.addEventListener('wheel',e=>{if(!state)return;e.preventDefault();zoomAt(zoom*Math.exp(-e.deltaY*.0015),e.clientX,e.clientY)},{passive:false});
function isPromptTool(tool){return tool==='sam_positive'||tool==='sam_negative';}
function addPrompt(p){
 if(!inside(p)){say('请在分析区域内点选。','error');return}
 const worm=active();
 remember();
 worm.points.push({x:p[0],y:p[1],label:mode==='sam_positive'?1:0});
 worm.confirmed=false;
 draw();
 void predict();
}
function panBy(dx,dy,moveFitted){
 // A fitted frame has no scroll range. Move the stage on fitted axes and
 // keep part of the image reachable. Oversized axes use normal scrolling.
 const frame=canvas.getBoundingClientRect(), bounds=viewport.getBoundingClientRect();
 if(moveFitted&&frame.width<=viewport.clientWidth){
  panX+=Math.max(bounds.left+32-frame.right,Math.min(dx,bounds.right-32-frame.left));
 }else viewport.scrollLeft-=dx;
 if(moveFitted&&frame.height<=viewport.clientHeight){
  panY+=Math.max(bounds.top+32-frame.bottom,Math.min(dy,bounds.bottom-32-frame.top));
 }else viewport.scrollTop-=dy;
 stage.style.transform=`translate(${panX}px, ${panY}px)`;
}
const navigationPointers=new Map();
const tapSlop=5;
let pinchDistance=0,pendingTap=null;
function holdSpace(held){
 spaceHeld=held;
 if(!navigationPointers.size)canvas.style.cursor=restingCursor();
}
function spaceDown(e){
 if(e.code!=='Space'||e.altKey||e.ctrlKey||e.metaKey||e.isComposing||!state||busy||saved)return;
 const target=e.target;
 if(target.isContentEditable||target.closest?.('textarea,input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="button"])'))return;
 if((e.currentTarget!==document||target.closest?.('button,summary,select,input,a[href]'))&&!canvas.matches(':hover'))return;
 e.preventDefault();
 if(!e.repeat)holdSpace(true);
}
function spaceUp(e){
 if(e.code==='Space'&&spaceHeld){e.preventDefault();holdSpace(false);}
}
// The outer workbench can still own keyboard focus on the first drag into the frame.
const keyListeners=new AbortController();
for(const doc of [document,window.frameElement?.ownerDocument].filter(Boolean)){
 doc.addEventListener('keydown',spaceDown,{signal:keyListeners.signal});
 doc.addEventListener('keyup',spaceUp,{signal:keyListeners.signal});
 doc.defaultView.addEventListener('blur',()=>holdSpace(false),{signal:keyListeners.signal});
}
window.addEventListener('pagehide',()=>{holdSpace(false);keyListeners.abort();},{once:true});
canvas.addEventListener('pointerdown',e=>{
 if(!state||busy||saved||drag)return;
 const primary=e.button===0, touch=e.pointerType==='touch';
 const spacePan=primary&&spaceHeld&&!touch;
 if(!(spacePan||touch||e.button===1||(primary&&(mode==='view'||isPromptTool(mode)))))return;
 e.preventDefault();e.stopImmediatePropagation();canvas.setPointerCapture(e.pointerId);
 const canTap=!spacePan&&primary&&(isPromptTool(mode)||(touch&&isScaleTool(mode)));
 pendingTap=canTap&&navigationPointers.size===0?{id:e.pointerId,x:e.clientX,y:e.clientY,point:point(e)}:null;
 navigationPointers.set(e.pointerId,{x:e.clientX,y:e.clientY,moveFitted:spacePan||handTool||step===2});pinchDistance=0;
 if(spacePan)canvas.style.cursor='grabbing';
},true);
canvas.addEventListener('pointermove',e=>{
 const old=navigationPointers.get(e.pointerId);if(!old)return;
 e.preventDefault();e.stopImmediatePropagation();
 if(pendingTap){
  if(Math.hypot(e.clientX-pendingTap.x,e.clientY-pendingTap.y)<tapSlop)return;
  pendingTap=null;
 }
 const dx=e.clientX-old.x,dy=e.clientY-old.y;
 old.x=e.clientX;old.y=e.clientY;
 if(navigationPointers.size===2){const [a,b]=[...navigationPointers.values()];const d=Math.hypot(a.x-b.x,a.y-b.y);
  if(pinchDistance)zoomAt(zoom*d/pinchDistance,(a.x+b.x)/2,(a.y+b.y)/2);pinchDistance=d;
 }else{
  canvas.style.cursor='grabbing';
  panBy(dx,dy,old.moveFitted);
 }
},true);
for(const name of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(name,e=>{
 if(!navigationPointers.delete(e.pointerId))return;
 e.stopImmediatePropagation();pinchDistance=0;
 const tap=pendingTap;
 pendingTap=null;
 if(!navigationPointers.size)canvas.style.cursor=restingCursor();
 if(name!=='pointerup'||tap?.id!==e.pointerId||busy||saved||Math.hypot(e.clientX-tap.x,e.clientY-tap.y)>=tapSlop)return;
 if(isPromptTool(mode))addPrompt(tap.point);
 else if(isScaleTool(mode))pickScale(tap.point);
},true);
