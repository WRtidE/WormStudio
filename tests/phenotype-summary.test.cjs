'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const metrics=require('../web/phenotype-results.js');
const metric=(key,value,extra={})=>({key,value,label:'原名称',unit:'μm',status:value===null?'missing':'measured',group:'growth',...extra});
const worm=(id,values)=>({worm_id:id,frames:120,valid_frames:100,mask_valid_frames:110,metrics:values});
test('exactly four aggregate metrics lead the page, preserving zero and missing values',()=>{
 const result=metrics.core(worm(1,[metric('median_length_um',505),metric('mean_width_um',30),metric('mean_speed_um_s',0)]));
 assert.deepEqual(result.map(metrics.label),['中位体长','平均体宽','平均运动速度','中位身体伸展度']);
 assert.equal(result[2].value,0);assert.equal(result[3].value,null);
});
test('pixel units remain pixels and explanations retain observation coverage',()=>{
 const item=metric('median_length_px',505,{unit:'px',valid_frames:100,valid_duration_s:8.5,definition:'有效帧中位数'});
 const detail=metrics.detail(item,worm(1,[item]));
 assert.equal(detail.name,'中位体长');assert.equal(detail.unit,'px');
 assert.match(detail.coverage,/100 \/ 120 帧/);assert.match(detail.coverage,/8.5 秒/);
});
test('missing head metrics have a reason and never become zero',()=>{
 const detail=metrics.detail(metric('directional_bends_per_min',null,{note:'需确认头端后计算',unit:'次/min'}),worm(1,[]));
 assert.equal(detail.value,'—');assert.equal(detail.note,'需确认头端后计算');
});
test('comparison export has one row per worm, correct units and empty missing values',()=>{
 const data={video_name:'A.mp4',video_path:'raw/A.mp4',sample:'A-v2',worms:[worm(1,[metric('median_length_px',505,{unit:'px',valid_frames:5}),metric('mean_speed_px_s',0,{unit:'px/s',valid_duration_s:0})]),
  worm(7,[metric('median_length_px',null,{unit:'px'}),metric('mean_speed_px_s',20,{unit:'px/s'})])]};
 assert.equal(metrics.csv(data),'"视频","源文件","任务","培养槽","线虫","总帧数","有效姿态帧","中位体长 (px)","平均运动速度 (px/s)","中位体长 · 有效帧数","平均运动速度 · 有效时长 (s)"\r\n"A.mp4","raw/A.mp4","A-v2","","1","120","100","505","0","5","0"\r\n"A.mp4","raw/A.mp4","A-v2","","7","120","100","","20","",""');
});
test('time series selects the requested worm and leaves missing measurements disconnected',()=>{
 const timeline={fps:2,frames:[[{worm_id:1,speed_px_s:10},{worm_id:7,speed_px_s:0}],[{worm_id:7,speed_px_s:null}],[{worm_id:7,speed_px_s:20}]]};
 const series=metrics.series(timeline,7,'speed_px_s');
 assert.deepEqual(series,[{time:0,value:0},{time:.5,value:null},{time:1,value:20}]);
 assert.deepEqual(metrics.runs(series).map(run=>run.length),[1,1]);
});
test('default rendering keeps definitions in details and shows grouped compact rows',()=>{
 const nodes=new Map();
 function node(tag){return {tag,children:[],textContent:'',hidden:false,open:false,disabled:true,append(...items){this.children.push(...items)},replaceChildren(){this.children=[]},setAttribute(){},addEventListener(){},showModal(){this.open=true},close(){this.open=false}};}
 const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node('div'));return nodes.get(id)},createElement:node};
 const data={fps:12,measurement_mode:'physical',worms:[worm(1,[metric('median_length_um',505,{valid_frames:100,definition:'有效帧中位数'}),metric('mean_speed_um_s',0,{unit:'μm/s',group:'motion'})])]};
 const context=vm.createContext({document,data});vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/phenotype-results.js'),'utf8'),context);vm.runInContext('renderPhenotypes(data,"sample")',context);
 assert.equal(nodes.get('summary-core').children.length,4);
 assert.match(nodes.get('phenotype-context').textContent,/10 秒/);
 assert.match(nodes.get('phenotype-context').textContent,/有效姿态 100 \/ 120 帧/);
 assert.equal(nodes.get('growth-metrics').children.length,0);
 const button=nodes.get('summary-core').children[0].children[0];assert.equal(button.textContent,'中位体长');assert.match(button.title,/100 \/ 120/);
 button.onclick();assert.equal(nodes.get('metric-detail-dialog').open,true);assert.equal(nodes.get('metric-detail-definition').textContent,'有效帧中位数');
 assert.equal(nodes.get('summary-trends').open,false);
 assert.equal(nodes.get('summary-trends').hidden,false);
 vm.runInContext('renderPhenotypes(null,"sample")',context);
 assert.equal(nodes.get('summary-trends').hidden,true);
});
test('result HTML is metric-first with optional video/review and no provisional A/B/C category',()=>{
 const html=fs.readFileSync(path.join(__dirname,'../web/studio.html'),'utf8');
 assert.ok(html.indexOf('id="summary-core"')<html.indexOf('id="studio-result-video"'));
 assert.match(html,/<section id="result-summary-panel"/);
 assert.ok(html.indexOf('id="result-video-details"')<html.indexOf('id="summary-trends"'));
 assert.ok(html.indexOf('id="summary-trends"')<html.indexOf('id="result-analysis-details"'));
 assert.match(html,/<details id="result-video-details"/);assert.match(html,/<details id="result-analysis-details"/);
 assert.doesNotMatch(html,/id="activity-result"|运动分类 A \/ B \/ C/);
});

test('chamber exports identify the video and physical chamber next to each measurement',()=>{
 const rows=metrics.csv({video_name:'sample.mp4',chamber_id:3,worms:[worm(1,[metric('median_length_um',123.45678)])]}).split('\r\n');
 assert.match(rows[0],/^"视频","源文件","任务","培养槽","线虫"/);
 assert.match(rows[1],/^"sample.mp4","","","3","1","120","100","123.45678"$/);
});
