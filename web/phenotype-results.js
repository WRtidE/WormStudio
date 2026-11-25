'use strict';
const SummaryMetrics=(()=>{
 const canonical=key=>String(key).replace(/_px/g,'_um');
 const labels={median_length_um:'中位体长',mean_width_um:'平均体宽',median_width_um:'中位体宽',median_area_um2:'中位面积',median_volume_estimate_um3:'中位体积估算',mean_speed_um_s:'平均运动速度',median_body_extension:'中位身体伸展度',median_length_width_ratio:'中位长宽比',median_midbody_abs_curvature_per_um:'中位中段曲率'};
 const coreKeys=['median_length_um','mean_width_um','mean_speed_um_s','median_body_extension'];
 const numeric=value=>value!==null&&value!==undefined&&value!==''&&typeof value!=='boolean'&&Number.isFinite(Number(value));
 const number=(value,digits=3)=>numeric(value)?Number(value).toLocaleString('zh-CN',{maximumFractionDigits:digits}):'—';
 const label=metric=>labels[canonical(metric.key)]||metric.label;
 const find=(worm,key)=>worm.metrics.find(metric=>canonical(metric.key)===key);
 const core=worm=>coreKeys.map(key=>find(worm,key)||{key,label:labels[key],value:null,unit:'',status:'missing',note:'此结果未记录该指标。'});
 function display(metric){
  const key=canonical(metric.key);
  const physicalCurvature=key.includes('curvature') && ['μm⁻¹','µm⁻¹'].includes(metric.unit);
  const unit=physicalCurvature?'mm⁻¹':metric.unit||'';
  if(!numeric(metric.value))return {value:'—',unit};
  const value=Number(metric.value)*(physicalCurvature?1000:1);
  const digits=key.endsWith('_count')?0:/length_um$|width_um$|area_um2$|volume_estimate_um3$/.test(key)?1:2;
  const rounded=number(value,digits);
  return {value:value!==0 && Number(rounded)===0?value.toExponential(1):rounded,unit};
 }
 function detail(metric,worm){
  const coverage=[];
  if(numeric(metric.valid_frames))coverage.push(`有效 ${number(metric.valid_frames,0)} / ${number(worm.frames,0)} 帧`);
  if(numeric(metric.valid_duration_s))coverage.push(`有效观察 ${number(metric.valid_duration_s)} 秒`);
  return {name:label(metric),...display(metric),note:metric.note||(numeric(metric.value)?'':'缺少可靠数据。'),definition:metric.definition||'',coverage:coverage.join(' · ')};
 }
 function columns(data){const seen=new Set(),out=[];for(const worm of data.worms)for(const m of worm.metrics){const key=canonical(m.key);if(!seen.has(key)){seen.add(key);out.push(m);}}return out;}
 function csv(data){
  const metrics=columns(data),quote=value=>'"'+String(value??'').replace(/"/g,'""')+'"';
  const indexed=data.worms.map(worm=>new Map(worm.metrics.map(m=>[canonical(m.key),m])));
  const coverage=[];
  for(const m of metrics){
   for(const [field,title] of [['valid_frames','有效帧数'],['valid_duration_s','有效时长 (s)']]){
    if(indexed.some(values=>numeric(values.get(canonical(m.key))?.[field])))coverage.push({key:canonical(m.key),field,label:label(m)+' · '+title});
   }
  }
  const rows=[['视频','源文件','任务','培养槽','线虫','总帧数','有效姿态帧',...metrics.map(m=>label(m)+(m.unit?` (${m.unit})`:'')),...coverage.map(c=>c.label)]];
  for(const [index,worm] of data.worms.entries()){
   const cell=(key,field)=>{const value=indexed[index].get(key)?.[field];return numeric(value)?Number(value):'';};
   rows.push([data.video_name,data.video_path,data.sample,data.chamber_id,worm.worm_id,worm.frames,worm.valid_frames,
    ...metrics.map(m=>cell(canonical(m.key),'value')),...coverage.map(c=>cell(c.key,c.field))]);
  }
  return rows.map(row=>row.map(quote).join(',')).join('\r\n');
 }
 function groupCsv(items,reports){
  const metrics=new Map();
  const key=m=>JSON.stringify([canonical(m.key),m.unit||'']);
  for(const item of items)for(const worm of reports[item.sample]?.worms||[])for(const m of worm.metrics)metrics.set(key(m),m);
  const rows=[['视频','源文件','培养槽','任务','状态','说明','线虫','总帧数','有效姿态帧',...Array.from(metrics.values(),m=>label(m)+(m.unit?' ('+m.unit+')':''))]];
  const states={complete:'已完成',running:'处理中',pending:'待处理',failed:'失败',cancelled:'已中断',removed:'已移出'};
  for(const item of items){
   const report=reports[item.sample];
   for(const worm of report?.worms?.length?report.worms:[null]){
    const values=new Map(worm?.metrics.map(m=>[key(m),m.value])||[]);
    rows.push([item.video_name,item.video_path,item.chamber_id,item.sample,states[item.status]||item.status,item.error||report?.error,worm?.worm_id,worm?.frames,worm?.valid_frames,...Array.from(metrics.keys(),k=>values.get(k))]);
   }
  }
  return rows.map(row=>row.map(value=>'"'+String(value??'').replace(/"/g,'""')+'"').join(',')).join('\r\n');
 }
 function download(csv,name){
  const url=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'})),link=document.createElement('a');
  link.href=url;link.download=name+'.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 }
 function series(timeline,worm,key){
  if(!timeline?.frames||!numeric(timeline.fps)||timeline.fps<=0)return [];
  return timeline.frames.map((rows,index)=>{const value=rows.find(row=>row.worm_id===worm)?.[key];return {time:index/timeline.fps,value:numeric(value)?Number(value):null};});
 }
 function runs(series){const out=[];let run=[];for(const point of series){if(point.value===null){if(run.length)out.push(run);run=[];}else run.push(point);}if(run.length)out.push(run);return out;}
 return {display,coreKeys,canonical,label,number,numeric,find,core,detail,columns,csv,groupCsv,download,series,runs};
})();
if(typeof module!=='undefined'&&module.exports)module.exports=SummaryMetrics;

const renderPhenotypes=(()=>{
 if(typeof document==='undefined')return ()=>{};
 const el=id=>document.getElementById(id);
 const add=(parent,tag,className,text)=>{const node=document.createElement(tag);node.className=className;node.textContent=text;parent.append(node);return node;};
 const {label,number,numeric,canonical}=SummaryMetrics;
 let current=null,selected=null,signature='',sample='',timeline=null;
 function showDetail(metric,worm){
  const d=SummaryMetrics.detail(metric,worm);
  el('metric-detail-title').textContent=d.name;el('metric-detail-value').textContent=d.value+(d.unit?' '+d.unit:'');
  el('metric-detail-note').textContent=d.note;el('metric-detail-definition').textContent=d.definition;el('metric-detail-coverage').textContent=d.coverage;
  if(!el('metric-detail-dialog').open)el('metric-detail-dialog').showModal();
 }
 function metricButton(parent,metric,worm,valueOnly=false){
  const detail=SummaryMetrics.detail(metric,worm);
  const button=add(parent,'button',valueOnly?'summary-missing':'summary-metric-name',valueOnly?'—':label(metric));
  button.type='button';button.title=[detail.definition,detail.note,detail.coverage].filter(Boolean).join('\n');
  button.setAttribute('aria-label',`${label(metric)}：${detail.value}${detail.unit}，查看详情`);button.onclick=()=>showDetail(metric,worm);return button;
 }
 function metricValue(parent,metric,worm){
  const shown=SummaryMetrics.display(metric);
  if(!numeric(metric.value))metricButton(parent,metric,worm,true);else add(parent,'strong','summary-value',shown.value);
  if(shown.unit)add(parent,'span','summary-unit',shown.unit);
 }
 function select(id,sync=true){
  if(!current?.worms.some(w=>w.worm_id===Number(id)))return;
  selected=Number(id);draw();const picker=el('result-worm');
  if(sync&&picker&&!picker.disabled&&Number(picker.value)!==selected){picker.value=selected;picker.dispatchEvent(new Event('change',{bubbles:true}));}
 }
 function comparison(){
  const panel=el('summary-comparison');panel.hidden=current.worms.length<2;
  const table=el('summary-comparison-table');table.replaceChildren();if(panel.hidden)return;
  const keys=['median_length_um','mean_width_um','mean_speed_um_s','median_body_extension','median_area_um2','path_length_um'];
  const metrics=keys.map(key=>current.worms.map(w=>SummaryMetrics.find(w,key)).find(Boolean)).filter(Boolean);
  const header=add(add(table,'thead','',''),'tr','','');add(header,'th','','线虫');for(const metric of metrics)add(header,'th','',label(metric)+(metric.unit?` (${metric.unit})`:''));
  const body=add(table,'tbody','','');
  for(const worm of current.worms){
   const row=add(body,'tr',worm.worm_id===selected?'is-selected':'','');const cell=add(row,'th','',''),button=add(cell,'button','summary-metric-name','线虫 '+number(worm.worm_id,0));button.type='button';button.onclick=()=>select(worm.worm_id);
   for(const metric of metrics){const item=SummaryMetrics.find(worm,canonical(metric.key));add(row,'td','',item?SummaryMetrics.display(item).value:'—');}
  }
 }
 function draw(){
  const worm=current.worms.find(w=>w.worm_id===selected)||current.worms[0];selected=worm.worm_id;
  const picker=el('phenotype-worms');picker.replaceChildren();picker.hidden=current.worms.length<2;
  if(!picker.hidden){const caption=add(picker,'label','','当前线虫 '),selectNode=add(caption,'select','','');selectNode.setAttribute('aria-label','切换线虫');for(const item of current.worms){const option=add(selectNode,'option','','线虫 '+number(item.worm_id,0));option.value=item.worm_id;}selectNode.value=selected;selectNode.onchange=()=>select(selectNode.value);}
  const duration=numeric(current.fps)&&current.fps>0&&numeric(worm.frames)?` · ${number(worm.frames/current.fps,2)} 秒`:'';
  el('phenotype-context').textContent=`${current.video_name||'整段视频'}${duration}${current.chamber_id!=null?' · 培养槽 '+current.chamber_id:''} · 线虫 ${number(worm.worm_id,0)}`;
  if(numeric(worm.valid_frames)&&numeric(worm.frames))el('phenotype-context').textContent+=' · 有效姿态 '+number(worm.valid_frames,0)+' / '+number(worm.frames,0)+' 帧';
  if(current.measurement_mode==='pixels')el('phenotype-context').textContent+=' · 像素单位';
  else if(numeric(current.scale_um_per_px))el('phenotype-context').textContent+=' · '+number(current.scale_um_per_px,5)+' μm/px'+(current.calibration_source?'（'+current.calibration_source+'）':'');
  el('phenotype-context').title=`共 ${number(worm.frames,0)} 帧；有效姿态 ${number(worm.valid_frames,0)} 帧；有效掩膜 ${number(worm.mask_valid_frames,0)} 帧`;
  el('summary-core').replaceChildren();for(const metric of SummaryMetrics.core(worm)){const card=add(el('summary-core'),'article','summary-core-card','');metricButton(card,metric,worm);const value=add(card,'div','summary-core-value','');metricValue(value,metric,worm);}
  const common=['median_area_um2','median_length_width_ratio','path_length_um','moving_duration_s','stationary_duration_s'];
  for(const group of ['growth','motion']){
   el(group+'-metrics').replaceChildren();
   el(group+'-extra').replaceChildren();
   el(group+'-more').hidden=true;
  }
  const ordered=common.flatMap(key=>worm.metrics.filter(m=>canonical(m.key)===key));
  ordered.push(...worm.metrics.filter(m=>!common.includes(canonical(m.key))&&!SummaryMetrics.coreKeys.includes(canonical(m.key))));
  for(const metric of ordered){
   const group=metric.group==='growth'?'growth':'motion';
   const extra=!common.includes(canonical(metric.key));
   if(extra)el(group+'-more').hidden=false;
   const row=add(el(group+(extra?'-extra':'-metrics')),'tr','','');const name=add(row,'th','','');name.setAttribute('scope','row');metricButton(name,metric,worm);const value=add(row,'td','','');metricValue(value,metric,worm);}
  comparison();drawTrend();
 }
 function drawTrend(){
  if(!current||!el('summary-trends').open)return;
  const pixel=current.measurement_mode==='pixels',kind=el('summary-trend-metric').value||'length';
  const spec={length:['length_'+(pixel?'px':'um'),'体长',pixel?'px':'μm'],speed:['speed_'+(pixel?'px':'um')+'_s','运动速度',pixel?'px/s':'μm/s'],width:['mean_width_'+(pixel?'px':'um'),'平均体宽',pixel?'px':'μm']}[kind];
  const points=SummaryMetrics.series(timeline,selected,spec[0]),valid=points.filter(p=>p.value!==null),chart=el('summary-trend-chart');chart.replaceChildren();
  if(!valid.length){el('summary-trend-note').textContent=timeline?'该指标暂无可靠的逐帧数据。':'正在读取逐帧指标…';return;}
  let min=Infinity,max=-Infinity;for(const point of valid){min=Math.min(min,point.value);max=Math.max(max,point.value);}const nonnegative=min>=0,padding=Math.max((max-min)*.12,Math.abs(max)*.03,1e-6);min-=padding;max+=padding;if(kind==='speed'&&nonnegative)min=0;
  const duration=Math.max(points.at(-1)?.time||0,1/timeline.fps),svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 800 250');svg.setAttribute('role','img');svg.setAttribute('aria-label',spec[1]+'随时间变化');chart.append(svg);
  const shape=(tag,attrs,text)=>{const node=document.createElementNS(svg.namespaceURI,tag);for(const [key,value]of Object.entries(attrs))node.setAttribute(key,value);if(text!==undefined)node.textContent=text;svg.append(node);return node;};
  const x=t=>62+712*t/duration,y=v=>208-174*(v-min)/(max-min);
  for(let i=0;i<4;i++){const value=min+(max-min)*i/3,yy=y(value);shape('line',{x1:62,x2:774,y1:yy,y2:yy,stroke:'#e5ebe7'});shape('text',{x:52,y:yy+4,'text-anchor':'end',fill:'#75847c','font-size':11},number(value,2));}
  for(let i=0;i<5;i++)shape('text',{x:x(duration*i/4),y:230,'text-anchor':'middle',fill:'#75847c','font-size':11},number(duration*i/4,2));
  shape('text',{x:62,y:18,fill:'#52695c','font-size':12},spec[1]+' ('+spec[2]+')');shape('text',{x:774,y:246,'text-anchor':'end',fill:'#75847c','font-size':11},'时间 (s)');
  const segments=SummaryMetrics.runs(points);
  if(points.length<=1500){
   for(const run of segments){
    if(run.length===1)shape('circle',{cx:x(run[0].time),cy:y(run[0].value),r:2,fill:'#39755b'});
    else shape('polyline',{points:run.map(p=>x(p.time)+','+y(p.value)).join(' '),fill:'none',stroke:'#39755b','stroke-width':2});
   }
  }else{
   const path=[];
   for(const run of segments){
    if(run.length===1){path.push('M'+x(run[0].time)+','+y(run[0].value)+'h0.1');continue;}
    // Keep extrema and endpoints of each screen column, in temporal order.
    for(let i=0;i<run.length;){
     const first=i,column=Math.floor(x(run[i].time));let low=i,high=i;
     while(++i<run.length && Math.floor(x(run[i].time))===column){
      if(run[i].value<run[low].value)low=i;
      if(run[i].value>run[high].value)high=i;
     }
     for(const j of [...new Set([first,low,high,i-1])].sort((a,b)=>a-b)){
      path.push((j===0?'M':'L')+x(run[j].time).toFixed(2)+','+y(run[j].value).toFixed(2));
     }
    }
   }
   shape('path',{d:path.join(' '),fill:'none',stroke:'#39755b','stroke-width':2,'stroke-linecap':'round'});
  }
  const note=`显示线虫 ${selected} 的${spec[1]}；缺失帧保持断开。`;el('summary-trend-note').textContent=note;
  svg.onpointermove=event=>{const box=svg.getBoundingClientRect(),fraction=Math.max(0,Math.min(1,((event.clientX-box.left)/box.width*800-62)/712)),point=points[Math.round(fraction*(points.length-1))];el('summary-trend-note').textContent=`${number(point.time,3)} s · ${spec[1]} ${SummaryMetrics.display({key:spec[0],value:point.value}).value} ${spec[2]}`;};svg.onpointerleave=()=>{el('summary-trend-note').textContent=note;};
 }
 const render=(data,run)=>{
  el('summary-export-all').hidden=!(data?.worms?.length>1);
  const present=!!data?.worms?.length;el('phenotype-panel').hidden=!present;el('summary-empty').hidden=present;el('summary-export').disabled=!present;
  el('summary-trends').hidden=!present;
  if(!present){current=null;timeline=null;signature='';return;}const key=JSON.stringify([run,data]);if(key===signature)return;if(sample!==run)timeline=null;
  sample=run;signature=key;current=data;if(!data.worms.some(w=>w.worm_id===selected))selected=data.worms[0].worm_id;draw();
 };
 render.selected=()=>selected;render.select=(id,sync=false)=>{if(Number(id)!==selected)select(id,sync);};render.setTimeline=data=>{timeline=data;drawTrend();};
 el('metric-detail-close').onclick=()=>el('metric-detail-dialog').close();el('summary-trends').addEventListener('toggle',drawTrend);el('summary-trend-metric').onchange=drawTrend;
 el('summary-export').onclick=()=>{if(current)SummaryMetrics.download(SummaryMetrics.csv({...current,worms:current.worms.filter(w=>w.worm_id===selected)}),(sample||'WormStudio')+'_线虫'+selected+'_指标');};
 el('summary-export-all').onclick=()=>{if(current)SummaryMetrics.download(SummaryMetrics.csv(current),(sample||'WormStudio')+'_全部线虫指标');};
 return render;
})();
