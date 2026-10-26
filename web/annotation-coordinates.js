'use strict';
const AnnotationCoordinates=(()=>{
 function point(x,y,rect,width,height){
  return [Math.max(0,Math.min(width-1,Math.round((x-rect.left)*width/rect.width))),
   Math.max(0,Math.min(height-1,Math.round((y-rect.top)*height/rect.height)))];
 }
 function sourcePoint(cropPoint,roi){return [cropPoint[0]+roi[0],cropPoint[1]+roi[1]];}
 return {point,sourcePoint};
})();
if(typeof module!=='undefined')module.exports=AnnotationCoordinates;
