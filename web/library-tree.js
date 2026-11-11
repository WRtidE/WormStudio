'use strict';
// The explorer only edits workspace references; source media stays untouched.
window.LibraryTree = {create({getState, mutate, select, importFolder, importFiles, dropFiles, icon, status, isBusy}) {
  const host=document.getElementById('file-list'), menu=document.getElementById('tree-menu');
  const dialog=document.getElementById('tree-dialog'), form=document.getElementById('tree-form');
  const input=document.getElementById('tree-name'), error=document.getElementById('tree-error');
  let destination=null, dragged=null, dialogAction=null, menuNode=null;
  const all=()=>{const s=getState();return [...s.folders.map(f=>({...f,isFolder:true})),...s.files];};
  const children=parent=>all().filter(n=>(n.parent||null)===parent).sort((a,b)=>(a.order??1e12)-(b.order??1e12)||a.name.localeCompare(b.name,'zh-CN'));
  const node=id=>all().find(n=>n.id===id);
  const path=n=>{const names=[n.name], seen=new Set([n.id]);while(n.parent){n=node(n.parent);if(!n||seen.has(n.id))break;seen.add(n.id);names.unshift(n.name);}return names.join(' / ');};
  const button=(label,fn)=>{const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=fn;return b;};
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
  function remove(n){
    const ids=new Set([n.id]);let old=-1;while(old!==ids.size){old=ids.size;for(const f of getState().folders)if(ids.has(f.parent))ids.add(f.id);}
    const count=getState().files.filter(f=>ids.has(f.id)||ids.has(f.parent)).length;
    prompt(n.isFolder?'删除文件夹':'移除文件',`将“${n.name}”${n.isFolder?`及其中 ${count} 个文件`:''}从工作区移除。磁盘文件、标注草稿和分析结果均保留。`,'',()=>mutate({op:'remove',id:n.id}),true);
  }
  function reorder(n,direction){const list=children(n.parent||null),i=list.findIndex(x=>x.id===n.id);if(i+direction<0||i+direction>=list.length)return;
    return act({op:'move',id:n.id,parent:n.parent||null,before:direction<0?list[i-1].id:list[i+2]?.id||null});}
  function context(n,x,y){
    closeMenu();menuNode=n;menu.replaceChildren();
    const add=(label,fn,disabled=false)=>{const b=button(label,()=>{closeMenu();fn();});b.disabled=disabled||isBusy();b.setAttribute('role','menuitem');menu.append(b);};
    if(n.isFolder){add('新建子文件夹',()=>newFolder(n.id));add('导入文件到此处',()=>importFiles(n.id));add('导入文件夹到此处',()=>importFolder(n.id));}
    add('重命名',()=>prompt('重命名','只修改工作区显示名称，原文件和分析记录保持不变。',n.name,name=>mutate({op:'rename',id:n.id,name})));
    const siblings=children(n.parent||null),i=siblings.findIndex(x=>x.id===n.id);
    add('上移  ·  Alt ↑',()=>reorder(n,-1),i===0);add('下移  ·  Alt ↓',()=>reorder(n,1),i===siblings.length-1);
    if(n.parent)add('移到上一级',()=>act({op:'move',id:n.id,parent:node(n.parent)?.parent||null}));
    add(n.isFolder?'删除文件夹…':'从工作区移除…',()=>remove(n));
    menu.hidden=false;menu.style.left=Math.max(4,Math.min(x,innerWidth-menu.offsetWidth-4))+'px';menu.style.top=Math.max(4,Math.min(y,innerHeight-menu.offsetHeight-4))+'px';
    menu.querySelector('button:not(:disabled)')?.focus();
  }
  function dropMode(event,n){const r=event.currentTarget.getBoundingClientRect(),ratio=(event.clientY-r.y)/r.height;return n.isFolder&&ratio>.25&&ratio<.75?'inside':ratio<.5?'before':'after';}
  function clearDrop(){host.querySelectorAll('[data-drop]').forEach(e=>delete e.dataset.drop);delete document.getElementById('workspace-home').dataset.drop;}
  function internalDrop(event,n,mode){
    const source=dragged||event.dataTransfer.getData('application/x-worm-node');if(!source||source===n?.id)return;
    const parent=mode==='inside'?n.id:n?.parent||null;
    const siblings=children(parent),i=siblings.findIndex(x=>x.id===n?.id);
    const before=mode==='before'?n.id:mode==='after'?siblings.slice(i+1).find(x=>x.id!==source)?.id||null:null;
    void act({op:'move',id:source,parent,before});
  }
  function render(){
    const s=getState(), focused=document.activeElement?.closest('[data-node]')?.dataset.node;
    if(destination&&!s.folders.some(f=>f.id===destination))destination=null;
    const query=s.query.trim().toLowerCase(),matches=f=>(s.filter==='all'||(s.filter==='video'?f.kind==='video':f.kind!=='video'))&&(!query||path(f).toLowerCase().includes(query));
    const matching=new Set(s.files.filter(matches).map(f=>f.id)), showFolders=new Set();
    const includeParents=n=>{const seen=new Set();while(n?.parent&&!seen.has(n.parent)){seen.add(n.parent);showFolders.add(n.parent);n=node(n.parent);}};
    for(const file of s.files)if(matching.has(file.id))includeParents(file);
    for(const folder of s.folders)if((!query&&s.filter==='all')||(query&&path(folder).toLowerCase().includes(query))){showFolders.add(folder.id);includeParents(folder);}
    host.replaceChildren();host.setAttribute('role','tree');host.setAttribute('aria-label','工作区文件树');
    function draw(parent,container,depth){
      for(const n of children(parent)){
        if(n.isFolder?!showFolders.has(n.id):!matching.has(n.id))continue;
        const entry=document.createElement('div');entry.className='tree-entry';entry.dataset.node=n.id;entry.style.setProperty('--depth',depth);
        const row=button('',()=>{destination=n.isFolder?n.id:n.parent||null;if(n.isFolder)void act({op:'collapse',id:n.id,collapsed:!n.collapsed});else select(n.id);});row.className='tree-item'+(!n.isFolder&&n.id===s.selectedId?' selected':'')+(n.isFolder&&n.id===destination?' folder-selected':'');
        row.setAttribute('role','treeitem');row.setAttribute('aria-level',depth+1);row.setAttribute('aria-label',n.name);row.setAttribute('aria-selected',String(!n.isFolder&&n.id===s.selectedId));row.title=path(n)+(n.isFolder?' · 拖动可移动或排序':' · 右键管理，拖动排序');
        const expanded=n.isFolder&&(!n.collapsed||!!query||s.filter!=='all');if(n.isFolder)row.setAttribute('aria-expanded',String(expanded));
        const arrow=document.createElement('span');arrow.className='tree-arrow';arrow.setAttribute('aria-hidden','true');
        if(n.isFolder)arrow.innerHTML='<svg viewBox="0 0 16 16" fill="none"><path d="m6 4 4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        const title=document.createElement('span');title.className='tree-name';title.textContent=n.name;
        row.append(arrow,icon(n.isFolder?'folder':n.kind==='video'?'video':n.kind==='image'?'image':'file'),title);
        if(!n.isFolder){const note=document.createElement('small');note.textContent=n.persisting?'保存中':n.persistError?'失败':n.available===false?'丢失':n.id===s.analysisId?'当前':'';row.append(note);}
        const more=button('⋯',event=>{const r=more.getBoundingClientRect();context(n,r.left,r.bottom);});more.className='tree-more';more.setAttribute('aria-label','管理 '+n.name);more.title='重命名、排序或移除';entry.append(row,more);container.append(entry);
        entry.oncontextmenu=event=>{event.preventDefault();context(n,event.clientX,event.clientY);};
        row.onkeydown=event=>{
          const key=event.key;if(key==='Delete'){event.preventDefault();remove(n);}else if(key==='F2'){event.preventDefault();prompt('重命名','只修改工作区显示名称。',n.name,name=>mutate({op:'rename',id:n.id,name}));}
          else if(event.altKey&&['ArrowUp','ArrowDown'].includes(key)){event.preventDefault();void reorder(n,key==='ArrowUp'?-1:1);}
          else if(key==='ArrowRight'&&n.isFolder){event.preventDefault();if(!expanded)void act({op:'collapse',id:n.id,collapsed:false});else host.querySelector(`[data-parent="${CSS.escape(n.id)}"] .tree-item`)?.focus();}
          else if(key==='ArrowLeft'){event.preventDefault();if(n.isFolder&&expanded)void act({op:'collapse',id:n.id,collapsed:true});else if(n.parent)host.querySelector(`[data-node="${CSS.escape(n.parent)}"] .tree-item`)?.focus();}
          else if(['ArrowUp','ArrowDown','Home','End'].includes(key)){event.preventDefault();const rows=[...host.querySelectorAll('.tree-item')],index=rows.indexOf(row);rows[key==='Home'?0:key==='End'?rows.length-1:Math.max(0,Math.min(rows.length-1,index+(key==='ArrowUp'?-1:1)))].focus();}
          else if(key==='F10'&&event.shiftKey){event.preventDefault();const r=row.getBoundingClientRect();context(n,r.left,r.bottom);}
        };
        row.draggable=!isBusy()&&!n.persisting;
        row.ondragstart=event=>{
          dragged=n.id;
          event.dataTransfer.setData('application/x-worm-node',n.id);
          if(n.kind==='video')event.dataTransfer.setData('application/x-worm-video',n.id);
          event.dataTransfer.effectAllowed=n.kind==='video'?'copyMove':'move';
          closeMenu();
        };
        row.ondragend=()=>{dragged=null;clearDrop();};
        entry.ondragover=event=>{if(!dragged&&![...event.dataTransfer.types].includes('Files'))return;event.preventDefault();event.stopPropagation();clearDrop();entry.dataset.drop=dragged?dropMode(event,n):'inside';event.dataTransfer.dropEffect=dragged?'move':'copy';};
        entry.ondragleave=()=>delete entry.dataset.drop;
        entry.ondrop=event=>{event.preventDefault();event.stopPropagation();const mode=dropMode(event,n);clearDrop();if([...event.dataTransfer.types].includes('Files'))dropFiles(event.dataTransfer,n.isFolder?n.id:n.parent||null);else internalDrop(event,n,mode);dragged=null;};
        if(expanded){const group=document.createElement('div');group.setAttribute('role','group');group.dataset.parent=n.id;container.append(group);draw(n.id,group,depth+1);}
      }
    }
    draw(null,host,0);
    document.getElementById('new-folder').title=destination?'在“'+path(node(destination))+'”中新建文件夹':'新建文件夹';
    if(focused)host.querySelector(`[data-node="${CSS.escape(focused)}"] .tree-item`)?.focus({preventScroll:true});
    return {count:matching.size,empty:!host.children.length};
  }
  for(const target of [host,document.getElementById('workspace-home')]){
    target.addEventListener('dragover',event=>{if(!dragged&&![...event.dataTransfer.types].includes('Files'))return;event.preventDefault();event.stopPropagation();target.dataset.drop='inside';});
    target.addEventListener('dragleave',()=>delete target.dataset.drop);
    target.addEventListener('drop',event=>{event.preventDefault();event.stopPropagation();delete target.dataset.drop;if([...event.dataTransfer.types].includes('Files'))dropFiles(event.dataTransfer,null);else internalDrop(event,null,null);dragged=null;});
  }
  document.getElementById('workspace-home').addEventListener('click',()=>{destination=null;render();});
  document.getElementById('new-folder').onclick=()=>newFolder();
  document.addEventListener('pointerdown',event=>{if(!menu.contains(event.target)&&!event.target.closest('.tree-more'))closeMenu();});
  menu.addEventListener('keydown',event=>{const buttons=[...menu.querySelectorAll('button:not(:disabled)')],i=buttons.indexOf(document.activeElement);if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();buttons[(i+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length]?.focus();}if(event.key==='Escape'){const id=menuNode?.id;closeMenu();host.querySelector(`[data-node="${CSS.escape(id||'')}"] .tree-item`)?.focus();}});
  form.onsubmit=async event=>{event.preventDefault();const submit=document.getElementById('tree-submit');submit.disabled=true;error.hidden=true;try{await dialogAction(input.value);dialog.close();}catch(e){error.hidden=false;error.textContent=e.message;}finally{submit.disabled=false;}};
  window.addEventListener('resize',closeMenu);host.addEventListener('scroll',closeMenu);
  return {render,get destination(){return destination;},setDestination(id){destination=id;},closeMenu};
}};
