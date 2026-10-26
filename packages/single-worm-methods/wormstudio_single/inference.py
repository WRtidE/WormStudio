"""Optional neural inference; CPU replay does not import torch or SAM2."""
from pathlib import Path
import gc
import os
import sys
import tempfile

import cv2
import numpy as np
from scipy import ndimage as ndi


def require_checkpoint(path):
    if path is None or not Path(path).is_file():
        raise ValueError(f'Missing model checkpoint: {path}. See docs/INSTALL.md.')
    return str(Path(path).resolve())


def load_tracker(checkpoint,device):
    path=require_checkpoint(checkpoint)
    vendor=Path(__file__).resolve().parent/'_vendor/tapnet'
    sys.path.insert(0,str(vendor))
    import torch
    from tapnet.tapnextpp.votsp2026.model import TAPNextPP
    torch.manual_seed(0)
    if device=='cuda' and not torch.cuda.is_available():
        raise RuntimeError('CUDA is unavailable. Cached replay is available on CPU.')
    model=TAPNextPP.from_checkpoint(path,device=device)
    if device=='cuda':torch.cuda.synchronize()
    return model


def track(model,frames,points,with_evidence=False,support=None,diagnostics=None):
    from .evidence import decode_heads, FIELDS
    result=[];visible=[];state=None;heads={};records={key:[] for key in FIELDS}
    primary_count=len(points)
    support=np.empty((0,2),np.float32) if support is None else np.asarray(support,dtype=np.float32)
    if support.ndim!=2 or support.shape[1]!=2 or not np.isfinite(support).all():
        raise ValueError('Support queries must be finite xy points.')
    queries=np.concatenate([points,support]) if len(support) else points
    auxiliary=[];auxiliary_visible=[]
    # Observe upstream outputs without editing the vendored model or changing
    # its coordinate decoder / recurrence. The hook is removed on failure too.
    def capture(module,inputs,output):
        heads['coordinate']=output[1][0,0,:primary_count].detach().cpu().float().numpy()
        heads['visibility']=output[2][0,0,:primary_count,0].detach().cpu().float().numpy()
    hook=model._model.register_forward_hook(capture) if with_evidence else None
    try:
        for frame in frames:
            p,v,state=model.track_frame(frame,queries if state is None else None,state,autocast=True)
            result.append(p[:primary_count]);visible.append(v[:primary_count])
            if diagnostics is not None:
                auxiliary.append(p[primary_count:]);auxiliary_visible.append(v[primary_count:])
            if with_evidence:
                decoded=decode_heads(heads['coordinate'],heads['visibility'],frame.shape[1],frame.shape[0],model.MODEL_SIZE)
                for key in FIELDS:records[key].append(decoded[key])
    finally:
        if hook is not None:hook.remove()
    if diagnostics is not None:
        diagnostics.update(support_initial_xy=support,support_tracks=np.asarray(auxiliary),
                           support_visible=np.asarray(auxiliary_visible))
    if with_evidence:return np.asarray(result),np.asarray(visible),{key:np.asarray(value) for key,value in records.items()}
    return np.asarray(result),np.asarray(visible)


def segment(config,checkpoint,device):
    path=require_checkpoint(checkpoint)
    if device!='cuda':
        raise ValueError('Fresh SAM2 inference currently requires CUDA; use replay for CPU.')
    if os.environ.get('WORMSTUDIO_SAM2_ROOT'):
        sys.path.insert(0,os.environ['WORMSTUDIO_SAM2_ROOT'])
    import torch
    from sam2.build_sam import build_sam2_video_predictor
    if not torch.cuda.is_available():raise RuntimeError('CUDA is unavailable.')
    seed_path=config.get('seed_mask')
    if seed_path is None or not seed_path.is_file():
        raise ValueError('Fresh inference requires a seed_mask PNG in the config.')
    seed=cv2.imdecode(np.frombuffer(seed_path.read_bytes(),np.uint8),0)
    x0,y0,x1,y1=config['crop_xyxy']
    if seed is None or seed.shape!=(y1-y0,x1-x0) or not seed.any():
        raise ValueError('seed_mask must be a nonempty binary image matching crop dimensions.')
    model=build_sam2_video_predictor('configs/sam2/sam2_hiera_l.yaml',path,device=device,
        hydra_overrides_extra=['++model.non_overlap_masks=false','++model.non_overlap_masks_for_mem_enc=false'])
    dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    try:
        with tempfile.TemporaryDirectory(prefix='wormstudio-frames-') as temp:
            cap=cv2.VideoCapture(str(config['video']));n=0
            try:
                while True:
                    ok,frame=cap.read()
                    if not ok:break
                    ok,encoded=cv2.imencode('.jpg',frame[y0:y1,x0:x1],[cv2.IMWRITE_JPEG_QUALITY,94])
                    if not ok:raise RuntimeError('Could not encode model input frame.')
                    (Path(temp)/f'{n:06d}.jpg').write_bytes(encoded.tobytes());n+=1
            finally:cap.release()
            predictions={};anchor=config.get('anchor_frame',0)
            with torch.inference_mode(),torch.autocast('cuda',dtype=dtype):
                state=model.init_state(temp,offload_video_to_cpu=True,offload_state_to_cpu=True)
                model.add_new_mask(state,frame_idx=anchor,obj_id=1,mask=seed>0)
                for reverse in ([False,True] if anchor else [False]):
                    for i,ids,logits in model.propagate_in_video(state,start_frame_idx=anchor,reverse=reverse):
                        if list(ids)!=[1]:raise RuntimeError('Expected exactly one worm.')
                        mask=logits[0,0].cpu().numpy()>0
                        labels,_=ndi.label(mask,np.ones((3,3)));sizes=np.bincount(labels.ravel())
                        keep=sizes>=20;keep[0]=False;predictions[i]=keep[labels]
                del state
            if sorted(predictions)!=list(range(n)):raise RuntimeError('SAM2 did not return every frame.')
            return np.array([predictions[i] for i in range(n)])
    finally:
        del model
        gc.collect();torch.cuda.empty_cache()
