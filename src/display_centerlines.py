"""Image-supported display poses, independent of scientific measurement gates."""
import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage.graph import route_through_array
from skimage.morphology import skeletonize
from skeleton_geometry import sampled, visible_mask_tips
from head_temporal import MAX_INTERVAL_S
from quality_schema import permissions, flag
from centerline_topology import geometry_quality

VERSION = 'display-centerlines-v5-visible-tips'


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


def mask_guided_review(mask, left, right, fraction):
    """Fit a neighbouring posture to this frame's medial pixels for display."""
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    origin = np.maximum([xs.min()-3, ys.min()-3], 0)
    x, y = origin
    local = mask[y:ys.max()+4, x:xs.max()+4].astype(bool)
    skeleton = skeletonize(local)
    medial = np.argwhere(skeleton)[:, ::-1]
    if len(medial) < 2:
        return None
    a, b = sampled(left), sampled(right)
    if a is None or b is None:
        return None
    a, b = a-origin, b-origin
    if np.mean((a-b[::-1])**2) < np.mean((a-b)**2):
        b = b[::-1]
    length = np.mean([np.linalg.norm(np.diff(p, axis=0), axis=1).sum() for p in (a,b)])
    guide = (1-fraction)*a + fraction*b
    tree = cKDTree(medial)
    if np.mean(tree.query(guide)[0]) > .10*length:
        return None
    degree = ndimage.convolve(skeleton.astype(int),np.ones((3,3),int),mode='constant')-skeleton
    tips = np.argwhere(skeleton & (degree == 1))[:, ::-1]
    ramp = np.maximum(1 - np.arange(len(guide))/20, 0)[:, None]
    best, best_cost = None, np.inf
    # Around self-contact, interpolation can cut across a loop. Try both
    # neighbouring loop shapes as well and rank by current skeleton coverage.
    for t in (fraction, fraction*.4, 1-(1-fraction)*.4):
        p = (1-t)*a + t*b
        if len(tips):
            for end, weight in ((0,ramp),(-1,ramp[::-1])):
                distances = np.linalg.norm(tips-guide[end],axis=1)
                if distances.min() <= .055*length:
                    p += weight*(tips[distances.argmin()] - p[end])
        for step in range(14):
            correction = medial[tree.query(p)[1]] - p
            p += .7*ndimage.gaussian_filter1d(correction,max(.6,3-.21*step),axis=0,mode='nearest')
        line = supported_curve(p,local,length)
        if line is None:
            continue
        dense = dense_points(line)
        coverage = cKDTree(dense).query(medial)[0]
        line_length = np.linalg.norm(np.diff(line,axis=0),axis=1).sum()
        cost = (2*np.mean(coverage) + .12*abs(line_length-length)
                + .10*np.linalg.norm(p-guide,axis=1).mean())
        if cost < best_cost:
            best_cost, best = cost, line + origin
    return best


def fill_display_gaps(rows, output, get_mask):
    """Use two bracketing display poses for gaps of one or two frames."""
    by_key = {(int(r['worm_id']),int(r['frame'])): r for r in rows}
    anchors = set(output)
    for (worm,frame), row in by_key.items():
        if (worm,frame) in anchors or row.get('display_reason') != 'no_supported_curve':
            continue
        left = next((f for f in (frame-1,frame-2) if (worm,f) in anchors),None)
        right = next((f for f in (frame+1,frame+2) if (worm,f) in anchors),None)
        if left is None or right is None or right-left > 3:
            continue
        span = [by_key.get((worm,f)) for f in range(left,right+1)]
        if not all(r and flag(r,'source_valid') and flag(r,'mask_valid') and flag(r,'identity_valid')
                   and r.get('identity_segment') == row.get('identity_segment') for r in span):
            continue
        mask = get_mask(worm,frame)
        line = mask_guided_review(mask,output[worm,left],output[worm,right],(frame-left)/(right-left))
        if line is not None:
            output[worm,frame] = line
            row.update(display_source='temporal_mask_review',display_valid=True,
                       display_inside_fraction=inside_fraction(line,mask),
                       display_reason='current_mask_and_bracketing_poses',display_version=VERSION)


def track_visible_tips(rows, display, get_mask, fps):
    """Save independent display landmarks. A missing body path never becomes a measurement."""
    anchors = {}
    for i, row in enumerate(rows):
        if not all(flag(row, k) for k in ('source_valid', 'mask_valid', 'identity_valid')):
            continue
        p = display.get((int(row['worm_id']), i))
        if (p is None or row.get('display_source') != 'formal'
                or not flag(row, 'body_sequence_head_known') or flag(row, 'head_anchor_conflict')):
            continue
        tips = p[[0, -1]]
        hidden = [row.get(k+'_display_estimated_end') not in (None, '') for k in ('head', 'tail')]
        if any(hidden):
            row['display_tip_source'] = 'body_sequence_visible'
            for side, xy, missed in zip(('head', 'tail'), tips, hidden):
                if not missed:
                    row['display_'+side+'_tip_x_px'], row['display_'+side+'_tip_y_px'] = xy
        anchors[i] = (tips, np.linalg.norm(np.diff(p, axis=0), axis=1).sum(), hidden)

    for left, right in zip(anchors, list(anchors)[1:]):
        gap = right - left
        if gap < 2 or gap > 3 or gap / fps > MAX_INTERVAL_S:
            continue
        span = rows[left:right+1]
        if (any(r.get('identity_segment') != span[0].get('identity_segment')
                or not all(flag(r, k) for k in ('source_valid', 'mask_valid', 'identity_valid'))
                or flag(r, 'head_anchor_conflict') for r in span)
                or any(r.get('head_reason') != 'no_supported_body_path' for r in span[1:-1])):
            continue
        start, end = anchors[left][0], anchors[right][0]
        length = min(anchors[left][1], anchors[right][1])
        for i in range(left+1, right):
            row = rows[i]
            mask = get_mask(int(row['worm_id']), i)
            tips = visible_mask_tips(mask, length)
            row['display_tip_source'] = 'bracketed_mask_tips'
            row['display_tip_reference_frames'] = f'{left},{right}'
            if not len(tips):
                continue
            predicted = start + (end - start) * ((i-left) / gap)
            distances = np.linalg.norm(predicted[:, None] - tips, axis=2)
            for j, side in enumerate(('head', 'tail')):
                if anchors[left][2][j] or anchors[right][2][j]:
                    continue
                k = distances[j].argmin()
                distance = distances[j, k]
                if (np.linalg.norm(end[j]-start[j]) > .30 * length * gap or distance > .05 * length
                        or distances[1-j, k] <= distance + .05 * length
                        or np.count_nonzero(distances[j] <= distance + .03 * length) > 1):
                    continue
                row['display_'+side+'_tip_x_px'], row['display_'+side+'_tip_y_px'] = tips[k]


def build_display(rows, formal_lines, candidates, get_mask, fallback, allow_fallback=True):
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
            if allow_fallback:
                try:
                    proposal,_,_=fallback(mask)
                    q=supported_curve(proposal,mask,references.get(worm))
                except (ValueError,IndexError):pass
        if q is None:continue
        output[key]=q
        row.update(display_valid=True,display_source=source,display_inside_fraction=inside_fraction(q,mask),
            display_reason='review_only_current_mask' if flag(row,'mask_valid') else 'mask_and_pose_review')
    fill_display_gaps(rows,output,get_mask)
    return output
