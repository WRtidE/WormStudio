"""Opt-in feedback command. Existing run/retrack/replay and v4 sources are unchanged."""
from pathlib import Path
from dataclasses import asdict
import json,shutil,time
import numpy as np
from .feedback import FeedbackPolicy,VERSION,plan_feedback,judge_proposal

def controlled_pass(frames,masks,enabled,policy,track_fn,segment_fn,reseed_fn):
    original=np.asarray(masks,bool).copy()
    result=dict(version=VERSION,enabled=bool(enabled),policy=asdict(policy),iterations=0,status='disabled',
                accepted=False,accuracy_claim='Internal consistency only; no ground-truth accuracy established.')
    artifacts={}
    if not enabled:return original,result,artifacts
    tracks=track_fn(frames,original.copy(),policy);artifacts['tracks']=tracks
    if tracks is None:
        result.update(status='no_queries',reason='insufficient_foreground_for_region_queries');return original,result,artifacts
    plan=plan_feedback(original,tracks,policy);result['plan']=plan
    if plan['status']!='proposed':result.update(status='no_trigger');return original,result,artifacts
    candidate=np.asarray(segment_fn(frames,original.copy(),plan),bool);artifacts['candidate']=candidate.copy()
    decision=judge_proposal(original,candidate,tracks,plan,policy);result.update(iterations=1,decision=decision)
    if not decision['accepted']:
        result.update(status='rejected');return original,result,artifacts
    # Only one round: the new bank is exported for downstream use, never fed back again here.
    new_bank=reseed_fn(frames,candidate.copy(),policy,plan['frame'])
    if new_bank is None:
        result.update(status='rejected',reason='accepted_mask_cannot_initialize_new_query_bank');return original,result,artifacts
    artifacts['new_bank']=new_bank;result.update(status='accepted',accepted=True,new_query_identity='new bank at correction frame, no old point identity inherited')
    return candidate.copy(),result,artifacts

def execute_feedback(config_path,baseline,output,enabled=False,sam2_checkpoint=None,tapnext_checkpoint=None,
                     policy=None,device='cuda',retrack_v4=True):
    from . import pipeline
    from .quality import write_json
    policy=policy or FeedbackPolicy();baseline=Path(baseline).resolve();output=Path(output).resolve()
    if output==baseline or output.is_relative_to(baseline) or baseline.is_relative_to(output):raise ValueError('Baseline and output must be separate sibling trees')
    config=pipeline.load_config(config_path);config['cache']=baseline
    masks,base,bq,meta=pipeline.replay_inputs(config)
    if not 3<=len(masks)<=512:raise ValueError('Feedback accepts events of3..512 frames')
    if enabled:
        from .inference import require_checkpoint
        require_checkpoint(sam2_checkpoint);require_checkpoint(tapnext_checkpoint)
        if device!='cuda':raise ValueError('Fresh feedback inference requires CUDA')
    if output.exists() and any(output.iterdir()):raise ValueError('Output directory is not empty')
    output.mkdir(parents=True,exist_ok=True);start=time.perf_counter()
    code_hashes={name:pipeline.sha256(Path(__file__).parent/name) for name in ('feedback.py','feedback_runner.py','feedback_inference.py')}
    np.save(output/'masks-before.npy',masks)
    if enabled:
        from .feedback_inference import track_region,propose_masks
        frames=pipeline.read_frames(config,meta['tracking_roi'])
        if len(frames)!=len(masks):raise ValueError('Frame count mismatch')
        track_fn=lambda f,m,p:track_region(f,m,tapnext_checkpoint,p,device)
        segment_fn=lambda f,m,p:propose_masks(f,m,p,sam2_checkpoint,device)
        reseed_fn=lambda f,m,p,a:track_region(f,m,tapnext_checkpoint,p,device,anchor=a)
    else:
        frames=None
        def forbidden(*args):raise AssertionError('Disabled feedback invoked a model')
        track_fn=segment_fn=reseed_fn=forbidden
    final,result,artifacts=controlled_pass(frames,masks,enabled,policy,track_fn,segment_fn,reseed_fn)
    np.save(output/'masks-final.npy',final)
    if 'candidate' in artifacts:np.save(output/'masks-proposed.npy',artifacts['candidate'])
    for name in ('tracks','new_bank'):
        if artifacts.get(name) is not None:np.savez_compressed(output/(name+'.npz'),**artifacts[name])
    result.update(source_video_sha256=meta['source_video_sha256'],code_sha256=code_hashes,baseline=str(baseline),config=str(Path(config_path).resolve()),
                  tracking_roi=meta['tracking_roi'],crop_xyxy=meta['crop_xyxy'],coordinate_system='xy pixels local to tracking_roi within crop',
                  changed_pixels=int((masks!=final).sum()),changed_frames=int(np.any(masks!=final,axis=(1,2)).sum()),
                  model_sha256={key:pipeline.sha256(path) for key,path in [('sam2',sam2_checkpoint),('tapnext',tapnext_checkpoint)]} if enabled else {})
    write_json(output/'feedback.json',result)
    if result['accepted'] and retrack_v4:
        x,y,xx,yy=config['crop_xyxy'];full=np.zeros((len(final),yy-y,xx-x),bool);lx,ly,rx,by=meta['tracking_roi'];full[:,ly:by,lx:rx]=final
        masks2,base2,bq2,meta2=pipeline.fresh_inputs(config,full,meta['fps']);prepared=output/'feedback-input';prepared.mkdir()
        np.savez_compressed(prepared/'inputs.npz',masks=masks2,baseline=base2);write_json(prepared/'metadata.json',meta2);write_json(prepared/'baseline-quality.json',bq2)
        pipeline.execute(config_path,output/'final-v4',mode='retrack',tapnext_checkpoint=tapnext_checkpoint,device=device,render=False,solver='v4',cache_override=prepared)
        result['v4_output']='final-v4'
    elif not result['accepted']:
        dest=output/'final-v4';dest.mkdir()
        for name in ('inputs.npz','metadata.json','baseline-quality.json','centerlines-candidate.npy','centerlines-auto-supported.npy','quality.json','frame-metrics.json','frame-metrics.csv','summary.json'):
            source=baseline/name
            if source.is_file():shutil.copy2(source,dest/name)
        result.update(v4_output='final-v4',v4_action='baseline copied byte-for-byte; no new v4 inference')
    else:result['v4_output']=None
    result.update(wall_seconds=time.perf_counter()-start,complete=True)
    write_json(output/'feedback.json',result);print(json.dumps(result,ensure_ascii=False),flush=True)
    return result
