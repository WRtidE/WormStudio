/* Pixel-space viewport math, shared with the deterministic Node checks. */
(function(root,factory){if(typeof module==='object'&&module.exports)module.exports=factory();else root.ResultViewport=factory();})(typeof window==='undefined'?globalThis:window,()=>{
  const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
  function fit(width,height,vw,vh,box=null){
    const b=box||[0,0,width,height],s=Math.min(vw/(b[2]-b[0]),vh/(b[3]-b[1]));
    return {s,x:vw/2-(b[0]+b[2])/2*s,y:vh/2-(b[1]+b[3])/2*s};
  }
  function constrain(t,width,height,vw,vh){
    return {...t,x:width*t.s<=vw?(vw-width*t.s)/2:clamp(t.x,vw-width*t.s,0),
      y:height*t.s<=vh?(vh-height*t.s)/2:clamp(t.y,vh-height*t.s,0)};
  }
  function zoomAt(t,factor,x,y,min,max){
    const s=clamp(t.s*factor,min,max),f=s/t.s;
    return {s,x:x-(x-t.x)*f,y:y-(y-t.y)*f};
  }
  function focusBox(box,width,height){
    if(!Array.isArray(box)||box.length!==4||!box.every(Number.isFinite)||box[2]<=box[0]||box[3]<=box[1])return null;
    const side=Math.min(Math.max(width,height),Math.max(80,1.6*Math.max(box[2]-box[0],box[3]-box[1])));
    const w=Math.min(side,width),h=Math.min(side,height);
    const x=clamp((box[0]+box[2]-w)/2,0,width-w),y=clamp((box[1]+box[3]-h)/2,0,height-h);
    return [x,y,x+w,y+h];
  }
  function sourceBox(box,crop){
    if(!box)return null;
    return [box[0]+(crop?.[0]||0),box[1]+(crop?.[1]||0),box[2]+(crop?.[0]||0),box[3]+(crop?.[1]||0)];
  }
  return {fit,constrain,zoomAt,focusBox,sourceBox};
});
