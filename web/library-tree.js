'use strict';
// The explorer only edits workspace references; source media stays untouched.
window.LibraryTree = {create({getState, mutate, select, stageVideos, importFolder, importFiles, dropFiles, icon, status, isBusy, selectionChanged}) {
  const host=document.getElementById('file-list'), menu=document.getElementById('tree-menu');
  const dialog=document.getElementById('tree-dialog'), form=document.getElementById('tree-form');
  const input=document.getElementById('tree-name'), error=document.getElementById('tree-error');
  let destination=null, dragged=null, dialogAction=null, menuNode=null;
  let renderPending=false, lastRow=null;
  let picked=new Set(), anchor=null, activeId=null, visibleFiles=[], ctrlPicked=null;
  let visible={count:0,empty:true};
  let searchQuery='', searchCollapsed=new Map();
  const all=()=>{const s=getState();return [...s.folders.map(f=>({...f,isFolder:true})),...s.files];};
  const children=parent=>all().filter(n=>(n.parent||null)===parent).sort((a,b)=>(a.order??1e12)-(b.order??1e12)||a.name.localeCompare(b.name,'zh-CN'));
  const node=id=>all().find(n=>n.id===id);
  const button=(label,fn)=>{const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=fn;return b;};
  function selectOnly(id){
    picked=new Set(id?[id]:[]);anchor=activeId=id;
  }
  function replaceId(previous,id){
    if(picked.delete(previous))picked.add(id);
    if(anchor===previous)anchor=id;
    if(activeId===previous)activeId=id;
  }
  function pick(id,event){
    const toggle=event.ctrlKey||event.metaKey;
    if(!toggle&&!event.shiftKey){select(id);return;}
    if(event.ctrlKey){
      // macOS can deliver a contextmenu and a click for one Control-click.
      if(ctrlPicked===id)return;
      ctrlPicked=id;
    }
    if(event.shiftKey){
      if(!visibleFiles.includes(anchor))anchor=visibleFiles.includes(activeId)?activeId:id;
      const start=visibleFiles.indexOf(anchor),end=visibleFiles.indexOf(id);
      if(!toggle)picked.clear();
      for(const file of visibleFiles.slice(Math.min(start,end),Math.max(start,end)+1))picked.add(file);
    }else{
      anchor=id;
      if(picked.has(id))picked.delete(id);else picked.add(id);
    }
    if(picked.has(activeId)&&visibleFiles.includes(activeId)){render();return;}
    activeId=picked.has(id)?id:visibleFiles.find(file=>picked.has(file))||null;
    select(activeId,{keepSelection:true});
  }
  function selectionFor(n){
    return !n.isFolder&&picked.has(n.id)?visibleFiles.filter(id=>picked.has(id)):[n.id];
  }
  function closeMenu(){menu.hidden=true;menuNode=null;}
  async function act(body,focusId=body.id){
    if(isBusy()){status('文件正在保存，请稍后再整理。',true);return false;}
    try{await mutate(body);if(focusId)host.querySelector(`[data-node="${CSS.escape(focusId)}"] .tree-item`)?.focus({preventScroll:true});return true;}
    catch(e){status(e.message,true);return false;}
  }
  function prompt(title,description,value,action,remove=false){
    if(isBusy()){status('请等待当前文件操作完成。',true);return;}
    closeMenu();document.getElementById('tree-dialog-title').textContent=title;
    document.getElementById('tree-dialog-description').textContent=description;
    document.getElementById('tree-name-field').hidden=remove;input.required=!remove;input.value=value||'';
    const submit=document.getElementById('tree-submit');submit.textContent=remove?'从工作区移除':'保存';submit.classList.toggle('danger',remove);
    error.hidden=true;dialogAction=action;dialog.showModal();if(!remove){input.focus();input.select();}
  }
  function newFolder(parent=destination){prompt('新建文件夹','在工作区中整理素材，不会修改磁盘上的原始目录。','新建文件夹',name=>mutate({op:'mkdir',name,parent}));}
  function remove(n,files=selectionFor(n)){
    if(files.length>1){
      prompt('移除文件',`将所选 ${files.length} 个文件从工作区移除。磁盘文件、标注草稿和分析结果均保留。`,'',()=>mutate({op:'remove',ids:files}),true);
      return;
    }
    const ids=new Set([n.id]);let old=-1;while(old!==ids.size){old=ids.size;for(const f of getState().folders)if(ids.has(f.parent))ids.add(f.id);}
    const count=getState().files.filter(f=>ids.has(f.id)||ids.has(f.parent)).length;
    prompt(n.isFolder?'删除文件夹':'移除文件',`将“${n.name}”${n.isFolder?`及其中 ${count} 个文件`:''}从工作区移除。磁盘文件、标注草稿和分析结果均保留。`,'',()=>mutate({op:'remove',id:n.id}),true);
  }
  function reorder(n,direction){const list=children(n.parent||null),i=list.findIndex(x=>x.id===n.id);if(i+direction<0||i+direction>=list.length)return;
    return act({op:'move',id:n.id,parent:n.parent||null,before:direction<0?list[i-1].id:list[i+2]?.id||null});}
  function context(n,x,y){
    closeMenu();menuNode=n;menu.replaceChildren();
    const add=(label,fn,disabled=false)=>{const b=button(label,()=>{closeMenu();fn();});b.disabled=disabled||isBusy();b.setAttribute('role','menuitem');menu.append(b);};
    const files=selectionFor(n);
    if(files.length>1){
      if(n.parent&&files.every(id=>node(id)?.parent===n.parent))add('移到上一级',()=>act({op:'move',ids:files,parent:node(n.parent)?.parent||null},n.id));
      add(`移除所选的 ${files.length} 个文件…`,()=>remove(n,files));
    }else{
      if(n.isFolder){add('新建子文件夹',()=>newFolder(n.id));add('导入文件到此处',()=>importFiles(n.id));add('导入文件夹到此处',()=>importFolder(n.id));}
      add('重命名',()=>prompt('重命名','只修改工作区显示名称，原文件和分析记录保持不变。',n.name,name=>mutate({op:'rename',id:n.id,name})));
      const siblings=children(n.parent||null),i=siblings.findIndex(x=>x.id===n.id);
      add('上移  ·  Alt ↑',()=>reorder(n,-1),i===0);add('下移  ·  Alt ↓',()=>reorder(n,1),i===siblings.length-1);
      if(n.parent)add('移到上一级',()=>act({op:'move',id:n.id,parent:node(n.parent)?.parent||null}));
      add(n.isFolder?'删除文件夹…':'从工作区移除…',()=>remove(n));
    }
    menu.hidden=false;menu.style.left=Math.max(4,Math.min(x,innerWidth-menu.offsetWidth-4))+'px';menu.style.top=Math.max(4,Math.min(y,innerHeight-menu.offsetHeight-4))+'px';
    menu.querySelector('button:not(:disabled)')?.focus();
  }
  function dropMode(entry,y){
    const r=entry.getBoundingClientRect(),ratio=(y-r.top)/r.height;
    if(entry.firstElementChild.hasAttribute('aria-expanded')&&ratio>.25&&ratio<.75)return 'inside';
    return ratio<.5?'before':'after';
  }
  const home=document.getElementById('workspace-home');
  let marker=null;
  function mark(target,mode){
    if(marker!==target&&marker)delete marker.dataset.drop;
    marker=target;
    if(marker)marker.dataset.drop=mode;
  }
  function blankDrop(y){
    // Immediately below the last visible row is still its insertion point.
    // Farther into the empty space the original root drop target applies.
    if(lastRow){
      const gap=y-lastRow.getBoundingClientRect().bottom;
      if(gap>=0&&gap<=16)return lastRow;
    }
    return null;
  }
  function internalDrop(sources,n,mode){
    if(sources.includes(n?.id))return;
    const parent=(mode==='inside'?n?.id:n?.parent)||null;
    const siblings=children(parent),i=siblings.findIndex(x=>x.id===n?.id);
    const before=mode==='before'?n.id:mode==='after'?siblings.slice(i+1).find(x=>!sources.includes(x.id))?.id||null:null;
    const items=sources.length>1?{ids:sources}:{id:sources[0]};
    void act({op:'move',...items,parent,before},sources[0]);
  }
  function render(){
    if(dragged){
      renderPending=true;
      return visible;
    }
    renderPending=false;
    const s=getState(), focused=document.activeElement?.closest('[data-node]')?.dataset.node;
    // Build a fresh index for this render; state can be mutated between renders.
    const byId=new Map(), byParent=new Map();
    for(const n of [...s.folders.map(f=>({...f,isFolder:true})),...s.files]){
      byId.set(n.id,n);
      const parent=n.parent||null;
      if(!byParent.has(parent))byParent.set(parent,[]);
      byParent.get(parent).push(n);
    }
    for(const siblings of byParent.values())siblings.sort((a,b)=>(a.order??1e12)-(b.order??1e12)||a.name.localeCompare(b.name,'zh-CN'));
    const node=id=>byId.get(id),children=parent=>byParent.get(parent)||[];
    const path=n=>{const names=[n.name],seen=new Set([n.id]);while(n.parent){n=node(n.parent);if(!n||seen.has(n.id))break;seen.add(n.id);names.unshift(n.name);}return names.join(' / ');};
    if(s.selectedId!==activeId)selectOnly(s.selectedId);
    if(destination&&!s.folders.some(f=>f.id===destination))destination=null;
    const query=s.query.trim().toLowerCase(),matches=f=>(s.filter==='all'||(s.filter==='video'?f.kind==='video':f.kind!=='video'))&&(!query||path(f).toLowerCase().includes(query));
    if(query!==searchQuery){searchQuery=query;searchCollapsed.clear();}
    function expandFolder(folder,expanded){
      destination=folder.id;
      // Search initially reveals matches, then respects the user's disclosure
      // choices without overwriting the normal saved folder layout.
      if(query){searchCollapsed.set(folder.id,!expanded);render();}
      else void act({op:'collapse',id:folder.id,collapsed:!expanded});
    }
    const matching=new Set(s.files.filter(matches).map(f=>f.id)), showFolders=new Set();
    const includeParents=n=>{const seen=new Set();while(n?.parent&&!seen.has(n.parent)){seen.add(n.parent);showFolders.add(n.parent);n=node(n.parent);}};
    for(const file of s.files)if(matching.has(file.id))includeParents(file);
    for(const folder of s.folders)if((!query&&s.filter==='all')||(query&&path(folder).toLowerCase().includes(query))){showFolders.add(folder.id);includeParents(folder);}
    lastRow=null;visibleFiles=[];
    host.replaceChildren();host.setAttribute('role','tree');host.setAttribute('aria-label','工作区文件树');host.setAttribute('aria-multiselectable','true');
    function draw(parent,container,depth){
      for(const n of children(parent)){
        if(n.isFolder?!showFolders.has(n.id):!matching.has(n.id))continue;
        const entry=document.createElement('div');entry.className='tree-entry';entry.dataset.node=n.id;entry.style.setProperty('--depth',depth);
        const expanded=n.isFolder&&(query?!searchCollapsed.get(n.id):!n.collapsed);
        const row=button('',event=>{destination=n.isFolder?n.id:n.parent||null;if(n.isFolder)expandFolder(n,!expanded);else pick(n.id,event);});
        const selected=!n.isFolder&&picked.has(n.id);
        row.className='tree-item'+(selected?' selected':'')+(n.isFolder&&n.id===destination?' folder-selected':'');
        row.setAttribute('role','treeitem');row.setAttribute('aria-level',depth+1);row.setAttribute('aria-label',n.name);
        if(!n.isFolder){row.setAttribute('aria-selected',String(selected));visibleFiles.push(n.id);}
        row.title=path(n)+(n.isFolder?' · 拖动可移动或排序':' · 右键管理，拖动排序');
        if(n.isFolder)row.setAttribute('aria-expanded',String(expanded));
        const arrow=document.createElement('span');arrow.className='tree-arrow';arrow.setAttribute('aria-hidden','true');
        if(n.isFolder)arrow.innerHTML='<svg viewBox="0 0 16 16" fill="none"><path d="m6 4 4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        const title=document.createElement('span');title.className='tree-name';title.textContent=n.name;
        row.append(arrow,icon(n.isFolder?'folder':n.kind==='video'?'video':n.kind==='image'?'image':'file'),title);
        if(!n.isFolder){const note=document.createElement('small');note.textContent=n.persisting?'保存中':n.persistError?'失败':n.available===false?'丢失':n.id===s.analysisId?'当前':'';row.append(note);}
        const more=button('⋯',event=>{const r=more.getBoundingClientRect();context(n,r.left,r.bottom);});more.className='tree-more';more.setAttribute('aria-label','管理 '+n.name);more.title='重命名、排序或移除';entry.append(row,more);container.append(entry);lastRow=entry;
        entry.oncontextmenu=event=>{event.preventDefault();if(event.ctrlKey&&!n.isFolder)pick(n.id,event);else context(n,event.clientX,event.clientY);};
        row.onkeydown=event=>{
          const key=event.key;if(key==='Delete'){event.preventDefault();remove(n);}else if(key==='F2'){event.preventDefault();prompt('重命名','只修改工作区显示名称。',n.name,name=>mutate({op:'rename',id:n.id,name}));}
          else if(event.altKey&&['ArrowUp','ArrowDown'].includes(key)){event.preventDefault();void reorder(n,key==='ArrowUp'?-1:1);}
          else if(key==='ArrowRight'&&n.isFolder){event.preventDefault();if(!expanded)expandFolder(n,true);else host.querySelector(`[data-parent="${CSS.escape(n.id)}"] .tree-item`)?.focus();}
          else if(key==='ArrowLeft'){event.preventDefault();if(n.isFolder&&expanded)expandFolder(n,false);else if(n.parent)host.querySelector(`[data-node="${CSS.escape(n.parent)}"] .tree-item`)?.focus();}
          else if(['ArrowUp','ArrowDown','Home','End'].includes(key)){event.preventDefault();const rows=[...host.querySelectorAll('.tree-item')],index=rows.indexOf(row);rows[key==='Home'?0:key==='End'?rows.length-1:Math.max(0,Math.min(rows.length-1,index+(key==='ArrowUp'?-1:1)))].focus();}
          else if(key==='F10'&&event.shiftKey){event.preventDefault();const r=row.getBoundingClientRect();context(n,r.left,r.bottom);}
        };
        if(expanded){const group=document.createElement('div');group.setAttribute('role','group');group.dataset.parent=n.id;container.append(group);draw(n.id,group,depth+1);}
      }
    }
    draw(null,host,0);
    document.getElementById('new-folder').title=destination?'在“'+path(node(destination))+'”中新建文件夹':'新建文件夹';
    if(focused)host.querySelector(`[data-node="${CSS.escape(focused)}"] .tree-item`)?.focus({preventScroll:true});
    const shown=new Set(visibleFiles);
    for(const id of picked)if(id!==activeId&&!shown.has(id))picked.delete(id);
    const count=visibleFiles.filter(id=>picked.has(id)).length;
    document.getElementById('file-total').textContent=count>1?`已选 ${count} 个`:s.files.length;
    visible={count:matching.size,empty:!host.children.length};
    selectionChanged?.(visibleFiles.filter(id=>picked.has(id)));
    return visible;
  }
  const preview=document.querySelector('.preview-main');
  let press=null, frame=0, blockClick=false;
  function trackTarget(){
    const {x,y}=press;
    const hit=document.elementFromPoint(x,y);
    const queue=preview.contains(hit)&&press.video;
    preview.classList.toggle('queue-hover',queue);
    if(host.contains(hit)){
      const entry=hit.closest('.tree-entry')||blankDrop(y);
      if(!entry){mark(host,'inside');return;}
      if(dragged.includes(entry.dataset.node)){mark(null);return;}
      mark(entry,dropMode(entry,y));
    }else mark(home.contains(hit)?home:null,'inside');
  }
  function scrollWhileDragging(){
    if(!dragged)return;
    const r=host.getBoundingClientRect(), {x,y}=press;
    if(x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom){
      const edge=26;
      const step=Math.max(0,edge-(r.bottom-y))-Math.max(0,edge-(y-r.top));
      if(step){host.scrollTop+=Math.sign(step)*Math.ceil(Math.abs(step)/3);trackTarget();}
    }
    frame=requestAnimationFrame(scrollWhileDragging);
  }
  host.addEventListener('pointerdown',event=>{
    const row=event.target.closest('.tree-item');
    if(!row||!event.isPrimary||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||isBusy())return;
    const source=node(row.parentElement.dataset.node);
    if(!source||source.persisting)return;
    const sources=selectionFor(source);
    press={id:event.pointerId,row,sources,video:sources.every(id=>node(id)?.kind==='video'),x:event.clientX,y:event.clientY};
  });
  document.addEventListener('pointermove',event=>{
    if(press?.id!==event.pointerId)return;
    if(!dragged){
      if(Math.hypot(event.clientX-press.x,event.clientY-press.y)<5)return;
      if(isBusy()||!press.row.isConnected){press=null;return;}
      dragged=press.sources;
      press.row.setPointerCapture(event.pointerId);
      press.row.parentElement.classList.add('tree-dragging');
      closeMenu();
      frame=requestAnimationFrame(scrollWhileDragging);
    }
    press.x=event.clientX;press.y=event.clientY;
    trackTarget();
  });
  function endPress(commit,event){
    if(!press)return;
    if(dragged){
      if(commit){
        press.x=event.clientX;press.y=event.clientY;trackTarget();
        if(marker)internalDrop(dragged,marker.dataset.node?node(marker.dataset.node):null,marker.dataset.drop);
        else if(preview.classList.contains('queue-hover'))stageVideos(dragged);
      }
      blockClick=true;
      press.row.parentElement?.classList.remove('tree-dragging');
      if(press.row.hasPointerCapture(press.id))press.row.releasePointerCapture(press.id);
      cancelAnimationFrame(frame);
      preview.classList.remove('queue-hover');
      mark(null);dragged=null;
    }
    press=null;
    if(renderPending)render();
  }
  document.addEventListener('pointerup',e=>{if(press?.id===e.pointerId)endPress(true,e);});
  document.addEventListener('pointercancel',e=>{if(press?.id===e.pointerId)endPress(false);});
  window.addEventListener('blur',()=>endPress(false));
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&dragged){e.preventDefault();endPress(false);}});
  document.addEventListener('pointerdown',()=>{blockClick=false;ctrlPicked=null;},true);
  document.addEventListener('click',e=>{
    if(blockClick&&e.detail){blockClick=false;e.preventDefault();e.stopImmediatePropagation();}
  },true);
  for(const area of [host,home]){
    area.addEventListener('dragover',event=>{
      if(!Array.from(event.dataTransfer.types).includes('Files'))return;
      event.preventDefault();event.stopPropagation();
      mark(event.target.closest('.tree-entry')||area,'inside');
      event.dataTransfer.dropEffect='copy';
    });
    area.addEventListener('dragleave',event=>{if(!area.contains(event.relatedTarget))mark(null);});
    area.addEventListener('drop',event=>{
      if(!Array.from(event.dataTransfer.types).includes('Files'))return;
      event.preventDefault();event.stopPropagation();mark(null);
      const target=node(event.target.closest('.tree-entry')?.dataset.node);
      dropFiles(event.dataTransfer,target?.isFolder?target.id:target?.parent||null);
    });
  }
  document.getElementById('workspace-home').addEventListener('click',()=>{destination=null;render();});
  document.getElementById('new-folder').onclick=()=>newFolder();
  document.addEventListener('pointerdown',event=>{if(!menu.contains(event.target)&&!event.target.closest('.tree-more'))closeMenu();});
  menu.addEventListener('keydown',event=>{const buttons=[...menu.querySelectorAll('button:not(:disabled)')],i=buttons.indexOf(document.activeElement);if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();buttons[(i+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length]?.focus();}if(event.key==='Escape'){const id=menuNode?.id;closeMenu();host.querySelector(`[data-node="${CSS.escape(id||'')}"] .tree-item`)?.focus();}});
  form.onsubmit=async event=>{event.preventDefault();const submit=document.getElementById('tree-submit');submit.disabled=true;error.hidden=true;try{await dialogAction(input.value);dialog.close();}catch(e){error.hidden=false;error.textContent=e.message;}finally{submit.disabled=false;}};
  window.addEventListener('resize',closeMenu);host.addEventListener('scroll',closeMenu);
  return {render,selectOnly,replaceId,selectedIds:()=>visibleFiles.filter(id=>picked.has(id)),get destination(){return destination;},setDestination(id){destination=id;},closeMenu};
}};
