'use strict';
window.ResultPlayer=(()=>{
  const el=id=>document.getElementById(id),video=el('studio-result-video'),canvas=el('result-canvas');
  const ctx=canvas.getContext('2d'),math=window.ResultViewport;
  let transform=null,focus=false,box=null,visible=false,pointers=new Map(),vw=0,vh=0;
  let frameInfo=null,original=false,crop=null,frameCount=0,fps=12,callback=null;
  const dimensions=()=>[video.videoWidth,video.videoHeight];
  function bounds(){
    const b=frameInfo?.mask_bbox_crop_xyxy;
    if(!b)return null;
    // Both served videos are full-source frames. Geometry uses crop coordinates.
    return math.focusBox(math.sourceBox(b,crop),...dimensions());
  }
  function apply(){
    if(!transform)return;
    transform=math.constrain(transform,...dimensions(),vw,vh);
    const baseline=math.fit(...dimensions(),vw,vh).s;
    el('result-zoom-level').textContent=Math.round(transform.s/baseline*100)+'%';
    paint();
  }
  function fit(){
    if(!video.videoWidth||!vw||!vh)return;
    box=focus?bounds():null;transform=math.fit(...dimensions(),vw,vh,box);
    el('result-focus-note').textContent=focus?(box?'跟随当前虫体 · 仅调整观察窗口':'本帧无可靠虫体轮廓，显示完整画面'):'滚轮或双指缩放 · 拖动平移 · 双击复位';
    el('result-focus').setAttribute('aria-pressed',String(focus));el('result-full').setAttribute('aria-pressed',String(!focus));apply();
  }
  function paint(){
    if(!visible||!transform||video.readyState<2)return;
    const ratio=window.devicePixelRatio||1;
    ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,vw,vh);
    ctx.save();ctx.translate(transform.x,transform.y);ctx.scale(transform.s,transform.s);
    ctx.drawImage(video,0,0);ctx.restore();
  }
  function resize(){
    const rect=canvas.getBoundingClientRect();vw=rect.width;vh=rect.height;
    const ratio=window.devicePixelRatio||1;
    canvas.width=Math.round(vw*ratio);canvas.height=Math.round(vh*ratio);fit();
  }
  function zoom(factor,x=vw/2,y=vh/2){
    if(!transform)return;
    const base=math.fit(...dimensions(),vw,vh).s;
    transform=math.zoomAt(transform,factor,x,y,base,base*32);apply();
  }
  canvas.addEventListener('wheel',e=>{e.preventDefault();const r=canvas.getBoundingClientRect();zoom(Math.exp(-e.deltaY*.0015),e.clientX-r.left,e.clientY-r.top);},{passive:false});
  canvas.addEventListener('dblclick',fit);
  canvas.addEventListener('pointerdown',e=>{
    if(e.button!==0)return;canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove',e=>{
    if(!pointers.has(e.pointerId)||!transform)return;
    const before=[...pointers.values()],previous=pointers.get(e.pointerId);
    pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});const after=[...pointers.values()];
    if(before.length===2){
      const distance=a=>Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y);
      const r=canvas.getBoundingClientRect(),cx=(before[0].x+before[1].x)/2-r.left,cy=(before[0].y+before[1].y)/2-r.top;
      if(distance(before)>0)zoom(distance(after)/distance(before),cx,cy);
      transform.x+=(after[0].x+after[1].x-before[0].x-before[1].x)/2;
      transform.y+=(after[0].y+after[1].y-before[0].y-before[1].y)/2;
    }else{transform.x+=e.clientX-previous.x;transform.y+=e.clientY-previous.y;}
    apply();
  });
  function end(e){pointers.delete(e.pointerId);if(!pointers.size)canvas.classList.remove('dragging');}
  for(const name of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(name,end);
  canvas.addEventListener('keydown',e=>{
    if(['+','=','-','0','ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))e.preventDefault();
    if(e.key==='+'||e.key==='=')zoom(1.25);else if(e.key==='-')zoom(.8);else if(e.key==='0')fit();
    else if(transform){const delta={ArrowLeft:[30,0],ArrowRight:[-30,0],ArrowUp:[0,30],ArrowDown:[0,-30]}[e.key];if(delta){transform.x+=delta[0];transform.y+=delta[1];apply();}}
  });
  el('result-zoom-in').onclick=()=>zoom(1.25);el('result-zoom-out').onclick=()=>zoom(.8);
  el('result-fit').onclick=fit;
  el('result-full').onclick=()=>{focus=false;fit();};el('result-focus').onclick=()=>{focus=true;fit();};
  el('result-play').onclick=async()=>{if(video.paused){if(video.ended)video.currentTime=0;try{await video.play();}catch{el('result-focus-note').textContent='视频未能播放，请重新读取结果或下载视频。';}}else video.pause();};
  const playLabel=()=>{el('result-play').textContent=video.paused?'播放':'暂停';el('result-play').setAttribute('aria-pressed',String(!video.paused));};
  for(const name of ['play','pause','ended'])video.addEventListener(name,playLabel);
  for(const name of ['loadeddata','seeked'])video.addEventListener(name,()=>{if(!transform)fit();paint();});
  video.addEventListener('loadedmetadata',fit);
  new ResizeObserver(resize).observe(canvas);
  if('requestVideoFrameCallback' in video){
    const tick=(_,m)=>{if(visible&&!video.paused){callback?.(Math.min(frameCount-1,Math.floor(m.mediaTime*fps+1e-4)));paint();}video.requestVideoFrameCallback(tick);};video.requestVideoFrameCallback(tick);
  }else video.addEventListener('timeupdate',paint);
  return {
    configure(info,onFrame){crop=info.crop_xyxy;frameCount=info.frame_count;fps=info.fps;callback=onFrame;},
    frame(row){
      const previous=box;frameInfo=row;
      if(focus){
        const next=bounds();
        if(JSON.stringify(next)!==JSON.stringify(previous)){
          if(transform&&previous&&next){
            const oldFit=math.fit(...dimensions(),vw,vh,previous),newFit=math.fit(...dimensions(),vw,vh,next);
            const relative=transform.s/oldFit.s,dx=transform.x-(vw/2-(previous[0]+previous[2])/2*transform.s),dy=transform.y-(vh/2-(previous[1]+previous[3])/2*transform.s);
            const s=newFit.s*relative;transform={s,x:vw/2-(next[0]+next[2])/2*s+dx,y:vh/2-(next[1]+next[3])/2*s+dy};box=next;apply();
          }else fit();
        }
      }
      paint();
    },
    source(value){original=value;transform=null;},
    reset(){video.pause();focus=false;box=null;frameInfo=null;transform=null;original=false;pointers.clear();fit();},
    setVisible(value){visible=value;if(value)resize();else video.pause();}
  };
})();
