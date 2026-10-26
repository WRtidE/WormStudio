"""Image-supported display poses, independent of scientific measurement gates."""
import numpy as np
from scipy import ndimage
from skimage.graph import route_through_array
from quality_schema import permissions, flag
from centerline_topology import geometry_quality

VERSION = 'display-centerlines-v2-mask-geometry-supported'


def dense_points(p):
    return np.concatenate([np.linspace(a,b,max(2,int(np.ceil(np.linalg.norm(b-a)))+1))
                           for a,b in zip(p[:-1],p[1:])])


def inside_fraction(p, mask):
    q=np.rint(dense_points(p)).astype(int)
    ok=(q[:,0]>=0)&(q[:,0]<mask.shape[1])&(q[:,1]>=0)&(q[:,1]<mask.shape[0])
    hit=np.zeros(len(q),bool);hit[ok]=mask[q[ok,1],q[ok,0]]
    return float(hit.mean())


def plausible_curve(p, reference_length=None):
    length=float(np.linalg.norm(np.diff(p,axis=0),axis=1).sum())
    if length<5:return False
    if reference_length is not None and not .65*reference_length<=length<=1.35*reference_length:return False
    return not geometry_quality(p)['local_hairpin']


def supported_curve(points, mask, reference_length=None):
    """Smooth only where the whole curve remains supported; route short edge leaks.

    No temporal extrapolation. Local routing cannot replace the global topology.
    The output may have more than 101 vertices to retain pixel-scale mask support.
    """
    p=np.asarray(points,float).copy()
    if p.ndim!=2 or p.shape[1]!=2 or len(p)<2 or not np.isfinite(p).all():return None
    if not plausible_curve(p,reference_length) or inside_fraction(p,mask)<.97:return None
    original_length=np.linalg.norm(np.diff(p,axis=0),axis=1).sum()
    for _ in range(8):
        q=p.copy();q[1:-1]=.6*p[1:-1]+.2*(p[:-2]+p[2:])
        if inside_fraction(q,mask)>=.999 and np.linalg.norm(np.diff(q,axis=0),axis=1).sum()>=.95*original_length:p=q
        else:break
    # Constrain occasional boundary leakage locally, keeping the global path order.
    _,nearest=ndimage.distance_transform_edt(~mask,return_indices=True)
    for i,v in enumerate(p):
        x,y=np.rint(v).astype(int)
        if not (0<=y<mask.shape[0] and 0<=x<mask.shape[1]):return None
        if not mask[y,x]:
            yy,xx=nearest[:,y,x]
            if np.linalg.norm(v-[xx,yy])>3:return None
            p[i]=[xx,yy]
    result=[p[0]]
    for a,b in zip(p[:-1],p[1:]):
        if inside_fraction(np.array([a,b]),mask)>=.999:result.append(b);continue
        lo=np.maximum(np.floor(np.minimum(a,b)).astype(int)-4,0)
        hi=np.minimum(np.ceil(np.maximum(a,b)).astype(int)+5,[mask.shape[1],mask.shape[0]])
        local=mask[lo[1]:hi[1],lo[0]:hi[0]]
        cost=np.where(local,1+1/(1+ndimage.distance_transform_edt(local)),np.inf)
        start=tuple(np.rint(a-lo).astype(int)[::-1]);end=tuple(np.rint(b-lo).astype(int)[::-1])
        try:route,total=route_through_array(cost,start,end,fully_connected=True)
        except ValueError:return None
        if not np.isfinite(total):return None
        result.extend(np.asarray(route)[1:,::-1]+lo)
    result=np.asarray(result,float)
    return result if inside_fraction(result,mask)>=.999 and plausible_curve(result,reference_length) else None


def build_display(rows, formal_lines, candidates, get_mask, fallback):
    """Return display vertices and add provenance only; never change metric gates."""
    output={};references={}
    for row in rows:
        key=(int(row['worm_id']),int(row['frame']))
        if permissions(row)['posture'] and key in formal_lines:
            references.setdefault(key[0],[]).append(float(np.linalg.norm(np.diff(formal_lines[key],axis=0),axis=1).sum()))
    references={w:float(np.median(v)) for w,v in references.items()}
    for row in rows:
        worm,frame=int(row['worm_id']),int(row['frame']);key=(worm,frame)
        row.update(display_source='unavailable',display_valid=False,display_inside_fraction=None,
                   display_reason='no_supported_curve',display_version=VERSION)
        if not flag(row,'source_valid'):
            row['display_reason']='source_invalid';continue
        mask=get_mask(worm,frame)
        if not mask.any():row['display_reason']='empty_mask';continue
        p=formal_lines.get(key)
        if permissions(row)['posture'] and p is not None:
            output[key]=p;row.update(display_source='formal',display_valid=True,
                display_inside_fraction=inside_fraction(p,mask),display_reason='measurement_pose');continue
        options=[]
        if p is not None:options.append(('geometry_review',p))
        if key in candidates:options.append(('diagnostic_candidate',candidates[key]))
        # Current-frame fallback is allowed even if identity/temporal mask gates reject it.
        # Such a curve remains a review visualization, without head confirmation.
        for source,proposal in options:
            q=supported_curve(proposal,mask,references.get(worm))
            if q is not None:break
        else:
            source='mask_refit';q=None
            try:
                proposal,_,_=fallback(mask)
                q=supported_curve(proposal,mask,references.get(worm))
            except (ValueError,IndexError):pass
        if q is None:continue
        output[key]=q
        row.update(display_valid=True,display_source=source,display_inside_fraction=inside_fraction(q,mask),
            display_reason='review_only_current_mask' if flag(row,'mask_valid') else 'mask_and_pose_review')
    return output
