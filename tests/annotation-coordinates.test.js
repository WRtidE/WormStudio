const test=require('node:test'),assert=require('node:assert/strict');
const {point}=require('../web/annotation-coordinates.js');
test('native coordinates survive zoom, viewport scroll, CSS offset and device-independent client coordinates',()=>{
 for(const scale of [.125,.5,1,2])for(const left of [30,-600])for(const top of [50,-500]){
  const rect={left,top,width:3200*scale,height:2200*scale};
  assert.deepEqual(point(left+945*scale,top+1267*scale,rect,3200,2200),[945,1267]);
  assert.deepEqual(point(left-100,top-100,rect,3200,2200),[0,0]);
  assert.deepEqual(point(left+rect.width+1,top+rect.height+1,rect,3200,2200),[3199,2199]);
 }
});
