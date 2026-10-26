"""Real pretrained model calls for single-pass feedback, separate from existing v4 execution."""
import gc,tempfile
from pathlib import Path
import numpy as np,cv2
from .feedback import sample_queries,distances

def track_region(frames,masks,checkpoint,policy,device='cuda',anchor=0):
    from .inference import load_tracker,track
    import torch
    queries=sample_queries(masks[anchor],policy.query_count)
    if not len(queries):return None
    model=load_tracker(checkpoint,device)
    try:
        if anchor!=0:
            # New query IDs are established at the corrected frame, not inherited from old tracks.
            f,fv,fe=track(model,frames[anchor:],queries,with_evidence=True)
            b,bv,be=track(model,frames[:anchor+1][::-1],queries,with_evidence=True)
            return dict(queries=queries,anchor=np.array(anchor),points=np.concatenate([b[::-1][:-1],f]),visible=np.concatenate([bv[::-1][:-1],fv]),
                        query_ids=np.arange(len(queries)),identity_scope=np.array('new_local_query_bank_no_old_point_identity_claim'))
        f,fv,fe=track(model,frames,queries,with_evidence=True)
        eligible=np.isfinite(f[-1]).all(-1)&fv[-1]&(distances(masks[-1],f[-1])<=.5)
        ids=np.flatnonzero(eligible);b=np.full_like(f,np.nan);bv=np.zeros_like(fv);be={k:np.full_like(v,np.nan) for k,v in fe.items()}
        if len(ids):
            rb,rv,re=track(model,frames[::-1],f[-1,ids],with_evidence=True);b[:,ids]=rb[::-1];bv[:,ids]=rv[::-1]
            for k,v in re.items():be[k][:,ids]=v[::-1]
        out=dict(queries=queries,forward=f,backward=b,forward_visible=fv,backward_visible=bv)
        out.update({'forward_'+k:v for k,v in fe.items()});out.update({'backward_'+k:v for k,v in be.items()})
        return out
    finally:
        del model;gc.collect();torch.cuda.empty_cache()

def propose_masks(frames,masks,plan,checkpoint,device='cuda'):
    """Fresh isolated SAM2 state; only the bounded interval is copied back into the proposal."""
    import torch,os,sys
    if os.environ.get('WORMSTUDIO_SAM2_ROOT'):sys.path.insert(0,os.environ['WORMSTUDIO_SAM2_ROOT'])
    from sam2.build_sam import build_sam2_video_predictor
    from .inference import require_checkpoint
    model=build_sam2_video_predictor('configs/sam2/sam2_hiera_l.yaml',require_checkpoint(checkpoint),device=device,
        hydra_overrides_extra=['++model.non_overlap_masks=false','++model.non_overlap_masks_for_mem_enc=false'])
    proposal=masks.copy();i=plan['frame'];lo=plan['start'];hi=plan['stop']
    dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    try:
        with tempfile.TemporaryDirectory(prefix='worm-feedback-') as temp:
            for k,frame in enumerate(frames):
                (Path(temp)/f'{k:06d}.jpg').write_bytes(cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,94])[1].tobytes())
            with torch.inference_mode(),torch.autocast(device,dtype=dtype):
                state=model.init_state(temp,offload_video_to_cpu=True,offload_state_to_cpu=True)
                # Endpoint seeds carry object identity, but contain no evaluation skeleton/mask labels.
                model.add_new_mask(state,frame_idx=0,obj_id=1,mask=masks[0])
                model.add_new_mask(state,frame_idx=len(frames)-1,obj_id=1,mask=masks[-1])
                model.add_new_points_or_box(state,frame_idx=i,obj_id=1,points=np.asarray(plan['points'],np.float32),labels=np.asarray(plan['labels'],np.int32))
                for reverse,distance in [(False,hi-1-i),(True,i-lo)]:
                    for frame_idx,obj_ids,logits in model.propagate_in_video(state,start_frame_idx=i,max_frame_num_to_track=distance,reverse=reverse):
                        if list(obj_ids)!=[1]:raise ValueError('Single-instance feedback expected')
                        if lo<=frame_idx<hi:proposal[frame_idx]=logits[0,0].cpu().numpy()>0
                del state
        return proposal
    finally:
        del model;gc.collect();torch.cuda.empty_cache()
