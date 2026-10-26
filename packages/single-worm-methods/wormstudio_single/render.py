"""Portable H.264 overlays and an offline per-frame result viewer."""
import html
import json
import subprocess

import cv2
import imageio_ffmpeg
import numpy as np

LABELS={'auto_supported':'Automatic checks passed','review':'Pose needs review',
        'temporal_recovered':'Temporal recovery: REVIEW','mask_review':'Mask needs review',
        'unavailable':'Centerline unavailable','no_anchor':'No reliable anchor'}


def letterbox(frame,width,height):
    scale=min(width/frame.shape[1],height/frame.shape[0])
    resized=cv2.resize(frame,None,fx=scale,fy=scale)
    result=np.full((height,width,3),22,np.uint8)
    y=(height-resized.shape[0])//2;x=(width-resized.shape[1])//2
    result[y:y+resized.shape[0],x:x+resized.shape[1]]=resized
    return result


def export_video(config,masks,curves,rows,meta,out):
    width,height=1152,832
    command=[imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-y',
             '-f','rawvideo','-pix_fmt','bgr24','-s',f'{width}x{height}','-r',str(meta['fps']),
             '-i','pipe:0','-an','-c:v','libx264','-preset','fast','-crf','19',
             '-pix_fmt','yuv420p','-movflags','+faststart',str(out/'overlay.mp4')]
    cap=cv2.VideoCapture(str(config['video']))
    # File-backed stderr avoids a full pipe preventing ffmpeg from finishing.
    with (out/'ffmpeg.log').open('wb') as log:
        writer=subprocess.Popen(command,stdin=subprocess.PIPE,stderr=log)
        try:
            cx,cy,ex,ey=meta['crop_xyxy'];x0,y0,x1,y1=meta['tracking_roi']
            for i,row in enumerate(rows):
                ok,raw=cap.read()
                if not ok:raise RuntimeError(f'Video ended before frame {i}.')
                frame=raw[cy:ey,cx:ex].copy()
                mask=np.zeros(frame.shape[:2],np.uint8);mask[y0:y1,x0:x1]=masks[i]
                contours,_=cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
                cv2.drawContours(frame,contours,-1,(65,210,75),2)
                p=curves[i]+[x0,y0]
                if np.isfinite(p).all():
                    points=np.rint(p).astype(np.int32)
                    if row['status']=='auto_supported':
                        cv2.polylines(frame,[points],False,(245,245,245),2,cv2.LINE_AA)
                    else:
                        for j in range(0,100,6):cv2.polylines(frame,[points[j:min(j+4,101)]],False,(0,180,255),2,cv2.LINE_AA)
                    for k,key in [(0,'endpoint_a_observed'),(-1,'endpoint_b_observed')]:
                        cv2.circle(frame,tuple(points[k]),5,(255,180,20) if k==0 else (40,70,255),-1 if row[key] else 2)
                canvas=np.full((height,width,3),22,np.uint8)
                canvas[52:820,:768]=letterbox(frame,768,768)
                yy,xx=np.nonzero(mask)
                if len(xx):
                    zoom=frame[max(0,yy.min()-25):min(frame.shape[0],yy.max()+26),max(0,xx.min()-25):min(frame.shape[1],xx.max()+26)]
                    canvas[58:358,790:1130]=letterbox(zoom,340,300)
                def fmt(v):return f'{v:.1f}' if v is not None and np.isfinite(v) else '--'
                lines=[LABELS[row['status']],f'Frame {i+1}/{len(rows)}   {i/meta["fps"]:.2f} s',
                       f'Length: {fmt(row["length_px"])} px',f'Width: {fmt(row["mean_width_px"])} px',
                       f'Area: {fmt(row["area_px2"])} px^2',f'Speed: {fmt(row["centroid_speed_px_s"])} px/s',
                       f'Candidate L: {fmt(row["candidate_length_px"])} px','Green: contour / White: passed',
                       'Orange: review / Hollow: inferred','A/B endpoints: head unconfirmed']
                label='v4 evidence' if 'v4' in meta['version'] else 'v3'
                support=meta.get('tapnext_support',{})
                if support.get('per_query'):label+=f' + {support["mode"]} support {support["per_query"]}'
                cv2.putText(canvas,config['video'].name+' | Single worm '+label,(16,32),0,.8,(240,240,240),1,cv2.LINE_AA)
                for j,line in enumerate(lines):cv2.putText(canvas,line,(790,395+j*38),0,.48,(220,220,220),1,cv2.LINE_AA)
                writer.stdin.write(canvas.tobytes())
                if i==0:cv2.imencode('.jpg',canvas)[1].tofile(str(out/'preview.jpg'))
        finally:
            cap.release();writer.stdin.close()
            code=writer.wait()
        if code:raise RuntimeError('FFmpeg failed; see ffmpeg.log.')


def write_index(out,rows,summary,has_video):
    from .quality import clean_json
    data=json.dumps(clean_json(rows),ensure_ascii=False).replace('</','<\\/')
    title=html.escape(summary['video'])
    media='<video controls muted src="overlay.mp4" poster="preview.jpg"></video>' if has_video else '<p>本次未导出视频。</p>'
    body='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>单虫 v3 结果</title><style>body{max-width:1152px;margin:30px auto;padding:20px;background:#f3f5f8;color:#253044;font:17px/1.7 system-ui}video{width:100%}button,input{font:inherit;margin:6px;padding:6px}pre{white-space:pre-wrap;background:white;padding:18px}</style>
<h1>单虫 v3：TITLE</h1><p>绿色轮廓，白色自动质检通过，橙色待复核，空心端点为推断。A/B 尚未确认头尾。通过比例不是准确率。</p>MEDIA
<div><button id="prev">上一帧</button><input id="frame" type="number" min="0" value="0"><button id="next">下一帧</button><button id="review">下一待复核帧</button></div>
<pre id="metrics"></pre><p><a href="frame-metrics.csv">逐帧 CSV</a> · <a href="frame-metrics.json">逐帧 JSON</a> · <a href="summary.json">处理时间</a> · <a href="centerlines-candidate.npy">候选坐标</a></p>
<script>const rows=DATA;const fps=FPS;let v=document.querySelector('video'),f=document.querySelector('#frame');
function show(i){i=Math.max(0,Math.min(rows.length-1,i));f.value=i;document.querySelector('#metrics').textContent=JSON.stringify(rows[i],null,2);return i}
function seek(i){i=show(i);if(v){v.pause();v.currentTime=(i+.1)/fps}}
document.querySelector('#prev').onclick=()=>seek(+f.value-1);document.querySelector('#next').onclick=()=>seek(+f.value+1);f.onchange=()=>seek(+f.value);
document.querySelector('#review').onclick=()=>{let i=rows.findIndex((r,k)=>k>+f.value&&!r.pose_metrics_valid);if(i<0)i=rows.findIndex(r=>!r.pose_metrics_valid);if(i>=0)seek(i)};
if(v)v.ontimeupdate=()=>show(Math.floor(v.currentTime*fps+.01));show(0);</script></html>'''
    body=body.replace('TITLE',title).replace('MEDIA',media).replace('DATA',data).replace('FPS',str(summary['fps']))
    if 'v4' in summary['version']:body=body.replace('单虫 v3','单虫 v4 · TAPNext 信息融合')
    (out/'index.html').write_text(body,encoding='utf-8')
