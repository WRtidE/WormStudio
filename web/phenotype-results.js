'use strict';
const renderPhenotypes=(()=>{
 let current=null,selected=null,signature='';
 const el=id=>document.getElementById(id);
 const number=(value,digits=2)=>value===null||value===undefined?'—':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:digits});
 const add=(parent,tag,className,text)=>{const node=document.createElement(tag);node.className=className;node.textContent=text;parent.append(node);return node};
 function draw(){
  const worm=current.worms.find(w=>w.worm_id===selected)||current.worms[0];selected=worm.worm_id;
  el('phenotype-worms').replaceChildren();for(const item of current.worms){const button=add(el('phenotype-worms'),'button','worm-tab',`线虫 ${number(item.worm_id,0)}`);button.type='button';button.setAttribute('aria-pressed',String(item.worm_id===selected));button.onclick=()=>{selected=item.worm_id;draw()}}
  el('phenotype-context').textContent=`线虫 ${number(worm.worm_id,0)} · 共 ${number(worm.frames,0)} 帧 · 有效姿态 ${number(worm.valid_frames,0)} 帧 · 有效掩膜 ${number(worm.mask_valid_frames,0)} 帧${current.scale_um_per_px===null?'':` · ${number(current.scale_um_per_px,5)} μm/px`}`;
  el('growth-metrics').replaceChildren();el('motion-metrics').replaceChildren();
  for(const metric of worm.metrics){const card=add(el(metric.group==='growth'?'growth-metrics':'motion-metrics'),'article',`metric-card ${metric.status}`,'');add(card,'span','metric-label',metric.label);const value=add(card,'div','metric-value','');add(value,'strong','',number(metric.value));add(value,'span','metric-unit',metric.unit);if(metric.status==='missing')add(card,'span','metric-status','暂不可用');if(metric.status==='candidate')add(card,'span','metric-status','头端候选结果');if(metric.note)add(card,'p','metric-note',metric.note);add(card,'p','metric-definition',metric.definition)}
  el('phenotype-context').textContent+=' · '+(worm.identity_note||'');
  el('head-result').textContent=worm.head.label;el('head-note').textContent=worm.head.note;
  el('activity-result').textContent=worm.activity.label;el('activity-note').textContent=worm.activity.provisional?'暂定分类，仅供复核；PPT 中的 A/B/C 阈值仍待确定。':'保留当前不可分类状态，不强行归入 A/B/C。';
  el('movement-metrics').replaceChildren();for(const movement of worm.movement){const card=add(el('movement-metrics'),'article','metric-card'+(movement.count===null?' missing':''),'');add(card,'span','metric-label',movement.label);const value=add(card,'div','metric-value','');add(value,'strong','',number(movement.count,0));add(value,'span','metric-unit','帧');if(movement.ratio!==null)add(card,'span','metric-status',`占总帧数 ${number(movement.ratio*100,1)}%`);add(card,'p','metric-note',movement.note)}
  el('movement-states').replaceChildren();if(!worm.state_counts.length)add(el('movement-states'),'p','help','没有可展示的运动状态明细。');for(const state of worm.state_counts){const item=add(el('movement-states'),'div','movement-state','');add(item,'span','',state.label);add(item,'strong','',`${number(state.count,0)} 帧 · ${number(state.ratio===null?null:state.ratio*100,1)}%`)}
 }
 return (data,sample)=>{const present=!!data?.worms?.length;el('phenotype-panel').hidden=!present;const jump=document.querySelector('.metrics-jump');if(jump)jump.hidden=!present;if(!present){current=null;signature='';return}const key=JSON.stringify([sample,data]);if(signature===key)return;signature=key;current=data;selected=data.worms[0].worm_id;draw()};
})();
