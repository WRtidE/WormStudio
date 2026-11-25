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

  // Source pixels are shared by the video and its overlays. The transform maps them to stage pixels.
  function bindZoom({stage, controls, paint, onZoom}) {
    const slider = controls.querySelector('input'), level = controls.querySelector('output');
    const fitButton = controls.querySelector('button');
    let view = null;

    function apply(transform) {
      const {w, h, width, height} = view;
      view.transform = constrain(transform, w, h, width, height);
      paint(view);
    }

    function syncControls() {
      const percent = Math.round(100 * view.transform.s / view.fit);
      slider.value = percent;
      slider.setAttribute('aria-valuetext', percent + '%');
      level.textContent = percent + '%';
      fitButton.disabled = view.transform.s <= view.fit;
      stage.classList.toggle('is-zoomed', !fitButton.disabled);
    }

    function layout(w, h) {
      const {width, height} = stage.getBoundingClientRect();
      if (!width || !height) return;
      const fitted = fit(w, h, width, height);
      let transform = fitted;
      if (view && view.w === w && view.h === h) {
        if (view.width === width && view.height === height) return;
        const ratio = fitted.s / view.fit;
        transform = {s:fitted.s * (view.transform.s / view.fit),
          x:width/2 + (view.transform.x - view.width/2) * ratio,
          y:height/2 + (view.transform.y - view.height/2) * ratio};
      }
      view = {w, h, width, height, fit:fitted.s};
      apply(transform);
      syncControls();
    }

    function zoomBy(factor, x = view.width/2, y = view.height/2) {
      const next = zoomAt(view.transform, factor, x, y, view.fit, view.fit * Number(slider.max)/100);
      if (next.s === view.transform.s) return;
      apply(next);
      syncControls();
      onZoom?.();
    }

    function reset() {
      if (!view || controls.hidden) return;
      apply(fit(view.w, view.h, view.width, view.height));
      syncControls();
      onZoom?.();
    }
    slider.oninput = () => {
      if (view) zoomBy(view.fit * Number(slider.value) / (100 * view.transform.s));
    };
    fitButton.onclick = reset;
    stage.addEventListener('wheel', event => {
      if (!view || controls.hidden || !event.deltaY) return;
      event.preventDefault();
      const rect = stage.getBoundingClientRect();
      const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1);
      zoomBy(Math.exp(-pixels * .0015), event.clientX-rect.left, event.clientY-rect.top);
    }, {passive:false});

    return {
      get view() { return view; },
      get zoomed() { return view && view.transform.s > view.fit; },
      layout, fit:reset,
      pan(dx, dy) {
        const {s, x, y} = view.transform;
        apply({s, x:x+dx, y:y+dy});
      },
      clear() { view = null; stage.classList.remove('is-zoomed'); }
    };
  }
  return {fit,constrain,zoomAt,focusBox,sourceBox,bindZoom};
});
