'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
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
