'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const studio=fs.readFileSync(path.join(__dirname,'../web/studio.js'),'utf8');
function studioPart(start,end){
 const a=studio.indexOf(start),b=studio.indexOf(end,a+1);
 assert.ok(a>=0&&b>a,start);return studio.slice(a,b);
}

test('tree edits keep pending uploads and uploads registered during a tree request',async()=>{
 const pending={id:'local',parent:'sub',signature:'large',persisting:true};
 const saved={id:'saved',path:'saved.mp4',name:'old'};
 let reply;
 const c=vm.createContext({
  files:[saved,pending],folders:[{id:'top'},{id:'sub',parent:'top'}],selectedId:'local',stagedIds:[],
  organizing:false,folderImport:false,progress:{token:'session'},selected:()=>pending,
  renderActions(){},renderFiles(){},renderDetails(){},status(){},jsonRequest:()=>new Promise(resolve=>{reply=resolve})
 });
 vm.runInContext(studioPart('async function treeMutation(','async function importFiles('),c);
 await assert.rejects(c.treeMutation({op:'remove',id:'top'}),/尚未保存/);
 await assert.rejects(c.treeMutation({op:'move',id:'saved',before:'local'}),/尚未保存/);
 const rename=c.treeMutation({op:'rename',id:'saved',name:'new'});
 reply({files:[{...saved,name:'new'},{id:'registered',parent:'sub',signature:'large'}],folders:c.folders});
 await rename;
 assert.equal(c.files.length,2);assert.equal(c.files[0],saved);assert.equal(saved.name,'new');assert.equal(c.files[1],pending);
 const collapse=c.treeMutation({op:'collapse',id:'sub'});
 Object.assign(pending,{id:'registered',path:'large.mp4',persisting:false});c.selectedId=pending.id;
 reply({files:[saved],folders:c.folders});await collapse;
 assert.equal(c.files.length,2);assert.equal(c.files[1],pending);assert.equal(c.selectedId,'registered');
});

test('a saved video starts and progress refreshes while the next upload is pending',async()=>{
 const nodes=new Map(),requests=[],uploads=[],ready={status:'ready',batch:{running:false},token:'token'};
 const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',firstChild:{},close(){}});return nodes.get(id)};
 const c=vm.createContext({$, $$:()=>[],files:[],folders:[],terminal:['ready','complete','failed','cancelled'],
  busy:false,localFile:false,connected:true,progress:ready,view:'preview',mode:'phenotype',initialized:true,
  resultRevision:0,pollNumber:0,appliedPoll:0,organizing:false,folderImport:false,pendingUploads:0,importQueue:Promise.resolve(),
  currentSample:'',setupSession:null,historyGroup:'',pickedSample:'',analysisId:null,selectedId:null,analysisItem:null,
  explorer:{destination:null,replaceId(){}},window:{},frame:{dataset:{}},channel:{flush:async()=>{}},
  AnalysisPresentation:require('../web/analysis-presentation.js'),ResultNavigation:require('../web/result-navigation.js'),
  videoExtensions:/\.mp4$/,imageExtensions:/\.png$/,URL:{createObjectURL:()=>null},crypto:require('node:crypto'),
  renderDetails(){},renderFiles(){},renderLinkedRuns(){},renderBatch(){},renderResultNavigation(){},status(){},
  editingSetup:()=>false,preview(){},persistSelection(){},batchSelection:()=>[],selected:()=>c.files.find(f=>f.id===c.selectedId),
  selectFile(id){c.selectedId=id},showView(next){c.view=next},
  upload(item){return new Promise((resolve,reject)=>uploads.push({item,resolve,reject}))},
  async jsonRequest(route,options){
   requests.push(route);
   if(route==='/api/new-video'){
    const {video_path}=JSON.parse(options.body);
    assert.equal(video_path,'saved/one.mp4');return {file:c.files[0]};
   }
   assert.equal(route,'/api/progress');return c.serverState;
  }
 });
 const run=script=>vm.runInContext(script,c);
 run(studioPart('const canStart =','function sizeLabel')+studioPart('function renderActions()','async function batchAction')+
  studioPart('async function importFiles(','async function droppedFiles(')+studioPart('function queueImport(','function upload(')+
  studioPart('async function prepareToLeave()','function renderLinkedRuns()')+
  studioPart('async function start(item','frame.addEventListener(\'load\''));
 c.serverState=ready;c.incoming=['one.mp4','two.mp4'].map(name=>({name,size:10,lastModified:1}));
 const batch=run('importFiles(incoming)');
 const tick=()=>new Promise(r=>setImmediate(r));
 await tick();assert.equal(c.pendingUploads,2);
 uploads[0].resolve({id:'one',path:'saved/one.mp4'});
 await tick();assert.equal(c.pendingUploads,1);
 assert.equal($('#start-analysis').disabled,false);
 c.selectedId=c.files[1].id;run('renderActions()');assert.equal($('#start-analysis').disabled,true);
 c.selectedId='one';c.serverState={...ready,status:'running',percent:47};
 await run('poll()');assert.equal(c.progress.percent,47);
 assert.equal($('#cancel-run').hidden,false);
 c.serverState=ready;await run('poll()');
 assert.equal($('#cancel-run').hidden,true);
 const file=c.files[0];await run('start(selected(), selected().path)');
 assert.equal(c.view,'annotation');
 uploads[1].reject(Error('disk full'));await batch;
 await tick();assert.equal(c.files[1].persistError,'disk full');assert.equal(c.files[0],file);
 c.selectedId=c.files[1].id;$('#retry-import').onclick();await tick();
 assert.equal(uploads[2].item,c.files[1]);
 uploads[2].resolve({id:'two',path:'saved/two.mp4'});await c.importQueue;
 assert.equal(c.files.length,2);assert.equal(c.files[1].persistError,false);
 assert.equal(requests.filter(r=>r==='/api/new-video').length,1);
 assert.deepEqual(uploads.map(u=>u.item.name),['one.mp4','two.mp4','two.mp4']);
});
function fixture(filter='video'){
 class Element{
  constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.attrs={};this.style={setProperty(){}};this.classList={toggle(){},add(){},remove(){},contains(){return false}};this.isConnected=true;}
  append(...items){for(const item of items){item.parentElement=this;this.children.push(item);}}
  replaceChildren(...items){this.children=[];this.append(...items);}
  setAttribute(k,v){this.attrs[k]=String(v);}getAttribute(k){return this.attrs[k];}hasAttribute(k){return k in this.attrs;}
  addEventListener(){} focus(){} contains(n){return this===n||this.children.some(c=>c.contains(n));}
  querySelector(){return null;}
 }
 const ids=new Map();const get=id=>{if(!ids.has(id))ids.set(id,new Element());return ids.get(id)};
 const document={getElementById:get,createElement:tag=>new Element(tag),querySelector:()=>new Element(),addEventListener(){},activeElement:null};
 const context=vm.createContext({window:{addEventListener(){}},document,CSS:{escape:s=>s},requestAnimationFrame(){},cancelAnimationFrame(){}});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/library-tree.js'),'utf8'),context);
 const state={filter,query:'',selectedId:null,files:[{id:'a',parent:'sub',kind:'video',name:'worm.mp4'},{id:'b',parent:'sub',kind:'file',name:'notes.csv'}],folders:[{id:'root',name:'数据',parent:null,collapsed:false},{id:'sub',name:'子目录',parent:'root',collapsed:false}]};
 const mutations=[];let tree;
 tree=context.window.LibraryTree.create({getState:()=>state,mutate:async body=>{mutations.push(body);state.folders.find(f=>f.id===body.id).collapsed=body.collapsed;tree.render();},select(){},stageVideos(){},importFolder(){},importFiles(){},dropFiles(){},icon:()=>new Element('svg'),status(){},isBusy:()=>false});
 function rows(){const out=new Map();const visit=e=>{if(e.dataset.node)out.set(e.dataset.node,e.children[0]);for(const c of e.children)visit(c)};visit(get('file-list'));return out;}
 tree.render();return{tree,state,mutations,rows};
}
for(const filter of ['all','video','other'])test(`folder click collapses and reopens under ${filter} filter`,async()=>{
 const f=fixture(filter);assert.equal(f.rows().get('root').getAttribute('aria-expanded'),'true');
 f.rows().get('root').onclick({});await Promise.resolve();assert.equal(f.rows().get('root').getAttribute('aria-expanded'),'false');assert.equal(f.rows().has('sub'),false);
 f.rows().get('root').onclick({});await Promise.resolve();assert.equal(f.rows().has('sub'),true);
 assert.equal(f.rows().has('a'),filter!=='other');assert.equal(f.rows().has('b'),filter!=='video');
});
test('changing file type filter preserves the saved collapsed folders',()=>{
 const f=fixture('all');f.state.folders[0].collapsed=true;
 for(const filter of ['video','other','all']){f.state.filter=filter;f.tree.render();assert.equal(f.rows().get('root').getAttribute('aria-expanded'),'false');assert.equal(f.rows().has('sub'),false);}
});
test('search reveals matches and still allows disclosure without altering saved layout',()=>{
 const f=fixture();f.state.folders[0].collapsed=true;f.state.query='worm';f.tree.render();assert.equal(f.rows().has('a'),true);
 f.rows().get('root').onclick({});assert.equal(f.rows().has('a'),false);assert.equal(f.mutations.length,0);
 f.rows().get('root').onclick({});assert.equal(f.rows().has('a'),true);
 f.state.query='';f.tree.render();assert.equal(f.rows().has('sub'),false);assert.equal(f.state.folders[0].collapsed,true);
});
test('keyboard folder navigation works in a filtered tree and during search',async()=>{
 const f=fixture();const key=key=>({key,preventDefault(){}});
 f.rows().get('root').onkeydown(key('ArrowLeft'));await Promise.resolve();assert.equal(f.rows().has('sub'),false);
 f.rows().get('root').onkeydown(key('ArrowRight'));await Promise.resolve();assert.equal(f.rows().has('sub'),true);
 f.state.query='worm';f.tree.render();f.rows().get('root').onkeydown(key('ArrowLeft'));assert.equal(f.rows().has('a'),false);
 f.rows().get('root').onkeydown(key('ArrowRight'));assert.equal(f.rows().has('a'),true);
});

test('tree rebuilds paths and order after in-place rename, move, and removal',()=>{
 const f=fixture('all');
 f.state.folders[1].name='已复核';f.tree.render();
 assert.equal(f.rows().get('a').title,'数据 / 已复核 / worm.mp4 · 右键管理，拖动排序');
 f.state.files[0].parent=null;f.state.files[0].order=0;f.tree.render();
 assert.equal(f.rows().get('a').title,'worm.mp4 · 右键管理，拖动排序');
 f.state.query='已复核';f.tree.render();
 assert.equal(f.rows().has('a'),false);assert.equal(f.rows().has('b'),true);
 f.state.files.splice(1,1);f.state.query='';f.tree.render();
 assert.equal(f.rows().has('b'),false);assert.equal(f.rows().has('a'),true);
});
