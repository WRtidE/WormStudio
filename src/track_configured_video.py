"""Track configured objects from separated anchor frames, on CUDA in both directions."""
import argparse
from compute_device import selected_device, inference_context, device_label
from resource_paths import RESOURCE_ROOT, sam2_checkpoint
from pipeline_progress import report_progress
from live_preview import report_tracking
import csv
import gc
import json
from pathlib import Path
import cv2
import numpy as np
import torch
from scipy import ndimage as ndi
from scipy.interpolate import splprep,splev
from sam2.build_sam import build_sam2_video_predictor
from sam2.sam2_image_predictor import SAM2ImagePredictor
from analyze_phenotypes import ROOT,read_image,write_png
from worm_colors import worm_color
from sam2_models import model_spec, tracking_model_matches


def guided_mask(mask,prompt):
    if 'guide_path' not in prompt:return mask
    p=np.array(prompt['guide_path'],float);spline,_=splprep(p.T,s=0)
    curve=np.array(splev(np.linspace(0,1,500),spline)).T
    corridor=np.zeros(mask.shape,np.uint8)
    for xy,t in zip(curve,np.linspace(0,1,len(curve))):
        taper=min(1,.25+10*t,.25+10*(1-t))
        cv2.circle(corridor,tuple(np.rint(xy).astype(int)),max(2,round(prompt['guide_radius']*taper)),1,-1)
    return mask&(corridor>0)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True);parser.add_argument('--seeds-only',action='store_true')
    parser.add_argument('--group',help='Reprocess only one configured group, preserving the other tracks')
    args=parser.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'))
    spec=model_spec(config.get('sam2_model','large'))
    out=ROOT/'outputs'/config['sample']/'sam2-tracking';out.mkdir(parents=True,exist_ok=True)
    if args.group and not args.seeds_only:
        prior=json.loads((out/'summary.json').read_text(encoding='utf-8'))
        if not tracking_model_matches(prior,spec['size']):
            raise ValueError('跟踪模型已改变，请使用新样本运行全部分组，避免混用 Tiny 和 Large 结果')
    capture=cv2.VideoCapture(config['video']);n=int(capture.get(cv2.CAP_PROP_FRAME_COUNT));fps=capture.get(cv2.CAP_PROP_FPS)
    cx,cy,ex,ey=config['crop_xyxy'];rows=[];ids=[w['id'] for g in config['groups'] for w in g['worms']]
    if args.group and not args.seeds_only:
        changed={w['id'] for g in config['groups'] if g['name']==args.group for w in g['worms']}
        with (out/'frame_metrics.csv').open() as handle:
            for row in csv.DictReader(handle):
                for k in ('frame','worm_id','area_px','ambiguous_overlap_px'):row[k]=int(row[k])
                if row['worm_id'] not in changed:rows.append(row)
    device=selected_device()
    checkpoint=sam2_checkpoint(spec['checkpoint'])
    print(f'视频跟踪模型：{spec["label"]}；权重：{checkpoint.name}；设备：{device_label(device)}',flush=True)
    model=build_sam2_video_predictor(spec['config'],str(checkpoint),device=device,
        hydra_overrides_extra=['++model.non_overlap_masks=false','++model.non_overlap_masks_for_mem_enc=false'])
    amp_dtype=(torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16) if device=='cuda' else torch.float32
    colors={w:worm_color(w) for w in ids}
    for group_index,group in enumerate(config['groups']):
        report_progress(detail=f'准备分组 {group_index+1}/{len(config["groups"])} 的跟踪数据',force=True)
        if args.group and group['name']!=args.group:continue
        x,y,x1,y1=group['crop_xyxy'];anchor=group['anchor_frame'];initial={};extra_masks={}
        capture.set(cv2.CAP_PROP_POS_FRAMES,anchor);ok,frame=capture.read()
        if not ok:raise RuntimeError('Missing anchor frame')
        im=frame[y:y1,x:x1];image_predictor=SAM2ImagePredictor(model)
        with torch.inference_mode(),inference_context(device):
            if any('mask_path' not in worm for worm in group['worms']):
                image_predictor.set_image(cv2.cvtColor(im,cv2.COLOR_BGR2RGB))
            for worm in group['worms']:
                w=worm['id']
                if 'mask_path' in worm:
                    initial[w]=read_image(Path(worm['mask_path']),True)>0
                    if initial[w].shape!=im.shape[:2]:raise ValueError('Manual mask dimensions mismatch')
                    write_png(out/f'initial-worm-{w}.png',initial[w].astype(np.uint8)*255)
                    print('manual seed',w,'area',int(initial[w].sum()),flush=True)
                    continue
                masks,scores,_=image_predictor.predict(point_coords=np.asarray(worm['points'],np.float32),
                    point_labels=np.asarray(worm['labels'],np.int32),box=np.asarray(worm['box'],np.float32),multimask_output=True)
                chosen=worm.get('mask_index',int(np.argmax(scores)))
                initial[w]=guided_mask(masks[chosen].astype(bool),worm)
                for k,mask in enumerate(masks):
                    mask=mask.astype(bool)
                    preview=im.copy();preview[mask]=(.6*preview[mask]+.4*np.array(colors[w])).astype(np.uint8)
                    cv2.putText(preview,f'Worm {w} option {k} score={scores[k]:.3f}',(10,28),0,.7,(0,0,255),2)
                    write_png(out/f'seed-w{w}-option-{k}.png',preview)
                write_png(out/f'initial-worm-{w}.png',initial[w].astype(np.uint8)*255)
                print('seed',w,'chosen',chosen,'scores',scores.tolist(),'area',int(initial[w].sum()),flush=True)
            for extra in group.get('additional_anchors',[]):
                capture.set(cv2.CAP_PROP_POS_FRAMES,extra['frame']);ok,frame=capture.read()
                if not ok:raise RuntimeError('Missing extra anchor')
                im=frame[y:y1,x:x1]
                if any('mask_path' not in worm for worm in extra['worms']):
                    image_predictor.set_image(cv2.cvtColor(im,cv2.COLOR_BGR2RGB))
                extra_masks[extra['frame']]={}
                for worm in extra['worms']:
                    if 'mask_path' in worm:
                        mask=read_image(Path(worm['mask_path']),True)>0
                        if mask.shape!=im.shape[:2]:raise ValueError('Manual anchor mask dimensions mismatch')
                        extra_masks[extra['frame']][worm['id']]=mask
                        continue
                    w=worm['id'];masks,scores,_=image_predictor.predict(point_coords=np.asarray(worm['points'],np.float32),
                        point_labels=np.asarray(worm['labels'],np.int32),box=np.asarray(worm['box'],np.float32),multimask_output=True)
                    k=worm.get('mask_index',int(np.argmax(scores)));mask=guided_mask(masks[k].astype(bool),worm);extra_masks[extra['frame']][w]=mask
                    preview=im.copy();preview[mask]=(.6*preview[mask]+.4*np.array(colors[w])).astype(np.uint8)
                    write_png(out/f'extra-seed-w{w}-f{extra["frame"]}.png',preview)
                    print('extra seed',w,extra['frame'],'chosen',k,'area',int(mask.sum()),flush=True)
        del image_predictor;torch.cuda.empty_cache()
        if args.seeds_only:continue
        frames=out/f'frames-{group["name"]}-{n}';frames.mkdir(exist_ok=True)
        capture.set(cv2.CAP_PROP_POS_FRAMES,0)
        for f in range(n):
            report_progress(f,n,'正在提取视频帧',fraction=(group_index+.15*f/n)/len(config['groups']))
            ok,frame=capture.read()
            if not ok:raise RuntimeError('Truncated video')
            path=frames/f'{f:06d}.jpg'
            path.write_bytes(cv2.imencode('.jpg',frame[y:y1,x:x1],[cv2.IMWRITE_JPEG_QUALITY,94])[1].tobytes())
        report_progress(detail='视频帧准备完成，正在初始化 SAM2',fraction=(group_index+.15)/len(config['groups']),force=True)
        predictions={}
        with torch.inference_mode(),inference_context(device):
            state=model.init_state(str(frames),offload_video_to_cpu=True,offload_state_to_cpu=True)
            for w,mask in initial.items():model.add_new_mask(state,frame_idx=anchor,obj_id=w,mask=mask)
            for f,objects in extra_masks.items():
                for w,mask in objects.items():model.add_new_mask(state,frame_idx=f,obj_id=w,mask=mask)
            start=group.get('propagate_from',anchor)
            for reverse in ([False,True] if start else [False]):
                for f,object_ids,logits in model.propagate_in_video(state,start_frame_idx=start,reverse=reverse):
                    current={int(w):mask for w,mask in zip(object_ids,logits[:,0].cpu().numpy()>0)}
                    # Keep separate visible body pieces; discard only tiny speckles.
                    for w,mask in current.items():
                        labels,_=ndi.label(mask,np.ones((3,3)));sizes=np.bincount(labels.ravel());keep=sizes>=20;keep[0]=False
                        current[w]=keep[labels]
                    predictions[f]={}
                    for w,mask in current.items():
                        full=np.zeros((ey-cy,ex-cx),bool);full[y-cy:y1-cy,x-cx:x1-cx]=mask
                        dest=out/'masks'/f'worm-{w}';dest.mkdir(parents=True,exist_ok=True)
                        write_png(dest/f'{f:06d}.png',full.astype(np.uint8)*255)
                        yy,xx=np.nonzero(mask);other=np.any([m for v,m in current.items() if v!=w],axis=0)
                        predictions[f][w]=dict(frame=f,worm_id=w,area_px=int(mask.sum()),
                            centroid_x_px=float(xx.mean()+x-cx) if len(xx) else None,
                            centroid_y_px=float(yy.mean()+y-cy) if len(yy) else None,
                            ambiguous_overlap_px=int((mask&other).sum()))
                    detail=f'分组 {group_index+1}/{len(config["groups"])} · '+('反向跟踪' if reverse else '正向跟踪')
                    report_tracking(frames/f'{f:06d}.jpg',current,f,n,detail)
                    report_progress(len(predictions),n,detail,fraction=(group_index+.15+.8*len(predictions)/n)/len(config['groups']))
            del state
        for f in sorted(predictions):rows.extend(predictions[f].values())
        gc.collect();torch.cuda.empty_cache()
        print('completed group',group['name'],flush=True)
    capture.release()
    if args.seeds_only:return
    rows.sort(key=lambda row:(row['frame'],row['worm_id']))
    from tracking_quality import mark_quality
    mark_quality(rows,ids,config.get('review_intervals',[]))
    for name in ('frame_metrics.csv','tracking-quality.csv'):
        with (out/name).open('w',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    summary=dict(video=config['video'],frames=n,fps=fps,crop_xyxy=config['crop_xyxy'],worm_ids=ids,
        scale_um_per_px=config['scale_um_per_px'],scale_source=config['scale_source'],model=spec['label'],
        measurement_mode=config.get('measurement_mode','physical'),
        sam2_model=spec['size'],model_config=spec['config'],checkpoint=spec['checkpoint'],
        inference_precision=str(amp_dtype).removeprefix('torch.'),
        mask_overlap_allowed=True,initialization=config['groups'],review_intervals=config.get('review_intervals',[]),device=device_label(device),
        cuda_peak_memory_mb=torch.cuda.max_memory_allocated()/2**20 if device=='cuda' else 0)
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
