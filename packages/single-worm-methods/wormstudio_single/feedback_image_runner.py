"""Image-corroborated feedback; at most one accepted update per video."""
from pathlib import Path
from dataclasses import asdict
import json,time,gc,shutil,inspect,hashlib
import numpy as np
from .feedback import FeedbackPolicy
from .feedback_local import LocalPolicy,windows,track_window,select_attempts
from .feedback_image import VERSION,ImagePolicy,image_plan,image_judge

def execute_image(config_path,baseline,output,sam2_checkpoint,tapnext_checkpoint,local=None,track_cache=None,image_policy=None):
    from . import pipeline
    from .quality import write_json
    from .inference import load_tracker,require_checkpoint
    from .feedback_inference import propose_masks,track_region
    from .feedback_local_inference import propose_local_masks
    import torch
    local=local or LocalPolicy();image_policy=image_policy or ImagePolicy();policy=FeedbackPolicy();baseline=Path(baseline).resolve();output=Path(output).resolve()
    if baseline==output or output.is_relative_to(baseline) or baseline.is_relative_to(output):raise ValueError('Separate output tree required')
    if output.exists() and any(output.iterdir()):raise ValueError('New output directory required')
    require_checkpoint(sam2_checkpoint);require_checkpoint(tapnext_checkpoint)
    c=pipeline.load_config(config_path);c['cache']=baseline;masks,base,bq,meta=pipeline.replay_inputs(c)
    if not 3<=len(masks)<=512:raise ValueError('Events3..512 frames required')
    source_hashes={p.name:pipeline.sha256(p) for p in baseline.iterdir() if p.is_file()}
    model_hashes={'sam2':pipeline.sha256(sam2_checkpoint),'tapnext':pipeline.sha256(tapnext_checkpoint)}
    manifest=None
    if track_cache:
        manifest=json.loads((Path(track_cache)/'cache-manifest.json').read_text())
        function_hash=hashlib.sha256(inspect.getsource(track_window).encode()).hexdigest()
        if manifest['source_video_sha256']!=meta['source_video_sha256'] or manifest['masks_sha256']!=source_hashes['inputs.npz'] or manifest['tap_sha256']!=model_hashes['tapnext'] or manifest['track_function_sha256']!=function_hash:raise ValueError('Local track cache provenance mismatch')
    frames=pipeline.read_frames(c,meta['tracking_roi']);output.mkdir(parents=True);start=time.perf_counter();records=[];model=None
    try:
        for s,e in windows(len(masks),local):
            fn=f'tracks-{s:04d}-{e:04d}.npz';cache=Path(track_cache)/fn if track_cache else None
            if cache and cache.exists():
                if manifest['files'].get(fn)!=pipeline.sha256(cache):raise ValueError('Local track cache file mismatch')
                tr=dict(np.load(cache,allow_pickle=False));shutil.copy2(cache,output/fn)
            else:
                if model is None:model=load_tracker(tapnext_checkpoint,'cuda')
                tr=track_window(model,frames[s:e],masks[s:e],policy)
                if tr is None:records.append(dict(start=s,stop=e,plan={'status':'no_queries'}));continue
                np.savez_compressed(output/fn,**tr)
            plan=image_plan(frames[s:e],masks[s:e],tr,policy,local,image_policy);records.append(dict(start=s,stop=e,plan=plan))
    finally:
        del model;gc.collect();torch.cuda.empty_cache()
    attempts=select_attempts(records,local);write_json(output/'plans.json',dict(windows=records,selected=attempts))
    final=masks.copy();results=[];accepted=False
    for number,r in enumerate(attempts):
        s,e=r['start'],r['stop'];tr=dict(np.load(output/f'tracks-{s:04d}-{e:04d}.npz'));plan=r['plan']
        candidate=propose_local_masks(frames[s:e],masks[s:e],plan,sam2_checkpoint)
        np.save(output/f'proposal-{number}.npy',candidate);decision=image_judge(frames[s:e],masks[s:e],candidate,tr,plan,policy,local,image_policy)
        record=dict(start=s,stop=e,proposal=f'proposal-{number}.npy',decision=decision);results.append(record)
        print('LOCAL_ATTEMPT',s,e,json.dumps(decision),flush=True)
        if decision['accepted']:
            bank=track_region(frames[s:e],candidate,tapnext_checkpoint,policy,anchor=plan['frame'])
            if bank is None or not np.isfinite(bank['points']).all():
                record['reseed_failure']='no finite new query bank';continue
            np.savez_compressed(output/'new_bank.npz',**bank);final[s:e]=candidate;accepted=True;break
    np.save(output/'masks-before.npy',masks);np.save(output/'masks-final.npy',final)
    if accepted:
        x,y,xx,yy=c['crop_xyxy'];full=np.zeros((len(final),yy-y,xx-x),bool);lx,ly,rx,by=meta['tracking_roi'];full[:,ly:by,lx:rx]=final
        m,b,q,mt=pipeline.fresh_inputs(c,full,meta['fps']);prepared=output/'feedback-input';prepared.mkdir()
        np.savez_compressed(prepared/'inputs.npz',masks=m,baseline=b);write_json(prepared/'metadata.json',mt);write_json(prepared/'baseline-quality.json',q)
        pipeline.execute(config_path,output/'final-v4',mode='retrack',tapnext_checkpoint=tapnext_checkpoint,render=False,solver='v4',cache_override=prepared)
    else:
        dest=output/'final-v4';dest.mkdir()
        for name in ('inputs.npz','metadata.json','baseline-quality.json','centerlines-candidate.npy','centerlines-auto-supported.npy','quality.json','frame-metrics.json','frame-metrics.csv','summary.json'):
            if (baseline/name).exists():shutil.copy2(baseline/name,dest/name)
    after={p.name:pipeline.sha256(p) for p in baseline.iterdir() if p.is_file()}
    if source_hashes!=after:raise RuntimeError('Source cache changed')
    result=dict(version=VERSION,complete=True,status='accepted' if accepted else 'rejected' if attempts else 'no_trigger',
        local_policy=asdict(local),policy=asdict(policy),mask_prior=True,image_policy=asdict(image_policy),source_video_sha256=meta['source_video_sha256'],source_cache_hashes=source_hashes,
        code_sha256={n:pipeline.sha256(Path(__file__).parent/n) for n in ('feedback_image.py','feedback_image_runner.py','feedback_local.py','feedback_local_inference.py','feedback.py','feedback_inference.py')},
        model_sha256=model_hashes,
        windows=len(records),proposed_windows=sum(r['plan']['status']=='proposed' for r in records),attempts=results,
        changed_frames=int(np.any(final!=masks,axis=(1,2)).sum()),tracking_roi=meta['tracking_roi'],crop_xyxy=meta['crop_xyxy'],
        baseline=str(baseline),wall_seconds=time.perf_counter()-start,accuracy_claim='Development pilot, no mask truth; internal acceptance is not accuracy')
    write_json(output/'feedback.json',result);return result
