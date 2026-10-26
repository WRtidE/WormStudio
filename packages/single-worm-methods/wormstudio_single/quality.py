"""Unchanged common geometric measures for the v3 solver."""
from pathlib import Path
import hashlib, json
import cv2
import numpy as np
from scipy import ndimage as ndi
from .skeleton import extract_niu
from .geometry import geometry_quality
INDICES = np.arange(0, 101, 5)
_SKELETON_CACHE = {}

def skeleton_geometry(mask):
    """Reuse mask-only geometry without retaining the large graph objects."""
    key=(mask.shape,hashlib.blake2b(mask.tobytes(),digest_size=16).digest())
    if key not in _SKELETON_CACHE:
        sk=extract_niu(mask)
        _SKELETON_CACHE[key]=(sk['raw_xy'].copy(),sk['radius'])
    return _SKELETON_CACHE[key]

def clean_json(x):
    if isinstance(x,dict):return {k:clean_json(v) for k,v in x.items()}
    if isinstance(x,(tuple,list)):return [clean_json(v) for v in x]
    if isinstance(x,np.ndarray):return clean_json(x.tolist())
    if isinstance(x,(np.floating,float)):return float(x) if np.isfinite(x) else None
    if isinstance(x,np.integer):return int(x)
    if isinstance(x,np.bool_):return bool(x)
    return x

def write_json(path,x):Path(path).write_text(json.dumps(clean_json(x),ensure_ascii=False,indent=2),encoding='utf-8')

def align(p,reference):
    return p[::-1].copy() if np.mean((p[::-1]-reference)**2)<np.mean((p-reference)**2) else p.copy()

def full_line(p):
    # Interpolation along the ordered body indices, not nearest pixel-path sorting.
    return np.column_stack([np.interp(np.arange(101),INDICES,p[:,j]) for j in range(2)])

def curve_measures(p,mask,reference):
    if not np.isfinite(p).all():return dict(finite=False,geometric_pass=False)
    xi=np.clip(np.rint(p[:,0]).astype(int),0,mask.shape[1]-1)
    yi=np.clip(np.rint(p[:,1]).astype(int),0,mask.shape[0]-1)
    in_bounds=((p[:,0]>=0)&(p[:,0]<mask.shape[1])&(p[:,1]>=0)&(p[:,1]<mask.shape[0]))
    inside=float((mask[yi,xi]&in_bounds).mean())
    length=float(np.linalg.norm(np.diff(p,axis=0),axis=1).sum())
    shape=geometry_quality(p)
    canvas=np.zeros(mask.shape,np.uint8)
    cv2.polylines(canvas,[np.rint(p).astype(np.int32)],False,1,1)
    dist=ndi.distance_transform_edt(~canvas.astype(bool))
    raw,radius=skeleton_geometry(mask)
    if len(raw):coverage=float(np.mean(dist[raw[:,1].astype(int),raw[:,0].astype(int)]<=max(2,radius)))
    else:coverage=0.
    length_error=abs(length/reference-1)
    return dict(finite=True,inside_fraction=inside,length_px=length,length_relative_deviation=length_error,
        skeleton_coverage=coverage,**shape,
        geometric_pass=bool(inside>=.97 and length_error<=.15 and not shape['local_hairpin'] and coverage>=.9))
