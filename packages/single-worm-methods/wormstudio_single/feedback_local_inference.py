"""SAM2 correction with the current predicted mask as its prior, never labels."""
import gc,tempfile,os,sys
from pathlib import Path
import numpy as np,cv2

def propose_local_masks(frames,masks,plan,checkpoint,device='cuda'):
    import torch
    if os.environ.get('WORMSTUDIO_SAM2_ROOT'):sys.path.insert(0,os.environ['WORMSTUDIO_SAM2_ROOT'])
    from sam2.build_sam import build_sam2_video_predictor
    from .inference import require_checkpoint
    model=build_sam2_video_predictor('configs/sam2/sam2_hiera_l.yaml',require_checkpoint(checkpoint),device=device,
        hydra_overrides_extra=['++model.non_overlap_masks=false','++model.non_overlap_masks_for_mem_enc=false'])
    proposal=masks.copy();i=plan['frame'];lo,hi=plan['start'],plan['stop']
    dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    try:
        with tempfile.TemporaryDirectory(prefix='worm-local-feedback-') as temp:
            for k,frame in enumerate(frames):
                ok,data=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,94])
                if not ok:raise ValueError('Frame encoding failed')
                (Path(temp)/f'{k:06d}.jpg').write_bytes(data.tobytes())
            with torch.inference_mode(),torch.autocast(device,dtype=dtype):
                state=model.init_state(temp,offload_video_to_cpu=True,offload_state_to_cpu=True)
                model.add_new_mask(state,frame_idx=0,obj_id=1,mask=masks[0])
                model.add_new_mask(state,frame_idx=len(frames)-1,obj_id=1,mask=masks[-1])
                # Official add_new_points_or_box reads existing temporary pred_masks
                # as prev_sam_mask_logits, then replaces the mask input with clicks.
                model.add_new_mask(state,frame_idx=i,obj_id=1,mask=masks[i])
                model.add_new_points_or_box(state,frame_idx=i,obj_id=1,points=np.asarray(plan['points'],np.float32),labels=np.asarray(plan['labels'],np.int32))
                for reverse,distance in [(False,hi-1-i),(True,i-lo)]:
                    for frame_idx,obj_ids,logits in model.propagate_in_video(state,start_frame_idx=i,max_frame_num_to_track=distance,reverse=reverse):
                        if list(obj_ids)!=[1]:raise ValueError('Single instance expected')
                        if lo<=frame_idx<hi:proposal[frame_idx]=logits[0,0].cpu().numpy()>0
                del state
        return proposal
    finally:del model;gc.collect();torch.cuda.empty_cache()
