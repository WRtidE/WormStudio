"""Uncalibrated TAPNext evidence, in window-local x/y pixel coordinates.

The coordinate head has two *marginal* distributions, not a joint 2-D
posterior. Retain both modes rather than the head's truncated soft argmax.
Neither visibility nor these distributions are an accuracy probability.
"""
import numpy as np
from scipy import ndimage as ndi
from scipy.special import expit

from .quality import INDICES

SCHEMA = 1
FIELDS = ('visibility_logits', 'coordinate_probabilities', 'position_std_px', 'entropy')


def decode_heads(coordinate_logits, visibility_logits, width, height, model_size=256):
    """Decode NumPy head outputs [Q,512], [Q] without changing tracker output.

    Upstream calls its axes x/y internally, but the wrapper returns the head
    in y/x order. Reverse the two marginals to match displayed x/y points.
    Temperature 0.5 matches upstream; omit its +/-20-bin truncation so that
    competing peaks remain available for downstream candidate scoring.
    """
    logits = np.asarray(coordinate_logits, np.float64)
    if logits.ndim != 2 or logits.shape[1] != 2*model_size or not np.isfinite(logits).all():
        raise ValueError('Expected finite coordinate logits [Q, 2*model_size].')
    logits = logits.reshape(-1, 2, model_size)[:, ::-1] * .5
    logits -= logits.max(-1, keepdims=True)
    probs = np.exp(logits); probs /= probs.sum(-1, keepdims=True)
    bins = np.arange(model_size) + .5
    mean = (probs*bins).sum(-1)
    variance = (probs*(bins-mean[..., None])**2).sum(-1)
    scale = np.array([width, height])/model_size
    entropy = -(probs*np.log(np.maximum(probs, 1e-30))).sum(-1)/np.log(model_size)
    return dict(visibility_logits=np.asarray(visibility_logits, np.float32).reshape(-1),
                coordinate_probabilities=probs.astype(np.float32),
                position_std_px=(np.sqrt(variance)*scale).astype(np.float32),
                entropy=entropy.astype(np.float32))


def has_evidence(z):
    present=[f'{direction}_{field}' in z for direction in ('forward','backward') for field in FIELDS]
    if any(present) and not all(present):
        raise ValueError('Incomplete TAPNext evidence cache; run retrack again.')
    return all(present)


def prepare(d, z):
    """Weight observations using visibility, concentration, mask and agreement.

    Normalization remains per queried point, not per surviving weight: a
    handful of uncertain observations must not acquire full voting power.
    """
    if not has_evidence(z):
        raise ValueError('v4 requires rich TAPNext evidence; run retrack first.')
    t = len(d['masks']); q = len(INDICES); scale = max(5., .03*d['reference_length'])
    result = dict(scale=scale, frames=[], backward_reversed=False)
    directions = []
    for direction in ('forward', 'backward'):
        points = np.asarray(z[direction], float).copy()
        logits = np.asarray(z[f'{direction}_visibility_logits'], float)
        probs = np.array(z[f'{direction}_coordinate_probabilities'], dtype=float, copy=True)
        std = np.asarray(z[f'{direction}_position_std_px'], float)
        entropy = np.asarray(z[f'{direction}_entropy'], float)
        if (points.shape != (t,q,2) or logits.shape != (t,q) or probs.shape != (t,q,2,256)
                or std.shape != (t,q,2) or entropy.shape != (t,q,2)):
            raise ValueError('TAPNext evidence has incompatible frame/point/axis dimensions.')
        available = np.isfinite(points).all(-1) & np.isfinite(logits) & np.isfinite(std).all(-1) & np.isfinite(entropy).all(-1)
        if not np.isfinite(probs).all() or (probs < 0).any():
            raise ValueError('Coordinate probabilities must be finite and nonnegative.')
        mass = probs.sum(-1)
        if np.any(available[...,None] & (abs(mass-1) > .01)):
            raise ValueError('Available coordinate marginals must sum to one.')
        probs /= np.maximum(mass[...,None], 1e-30)
        distribution_weight = np.nan_to_num(expit(logits)*(1-entropy.mean(-1)).clip(0,1),nan=0.)*available
        confidence = np.nan_to_num(distribution_weight * scale**2/(scale**2+(std**2).sum(-1)), nan=0.)
        confidence *= available
        for i, mask in enumerate(d['masks']):
            distance = ndi.distance_transform_edt(~mask)
            safe = np.nan_to_num(points[i], nan=-1e6)
            outside = ndi.map_coordinates(distance, [safe[:,1],safe[:,0]], order=1, mode='constant', cval=1e6)
            confidence[i] *= np.exp(-.5*(outside/max(2.,.01*d['reference_length']))**2)
            if not d['mask_valid'][i]: confidence[i] = 0; distribution_weight[i] = 0
        directions.append(dict(points=points, probabilities=probs, weight=confidence,
                               distribution_weight=distribution_weight))
    f,b = directions
    # The independently extracted right anchor may have the opposite A/B
    # order. Make one window-wide decision; never flip individual frames.
    def orientation_cost(reverse):
        bp = b['points'][:,::-1] if reverse else b['points']
        bw = b['weight'][:,::-1] if reverse else b['weight']
        w = np.minimum(f['weight'], bw)
        distance = np.nan_to_num(np.linalg.norm(f['points']-bp, axis=-1), nan=1e6)
        return float((w*np.minimum(distance/scale,10)).sum()/max(w.sum(),1e-9))
    normal,reverse = orientation_cost(False),orientation_cost(True)
    if reverse+.1 < normal:
        for key in b: b[key] = b[key][:,::-1].copy()
        result['backward_reversed'] = True
    delta = np.linalg.norm(f['points']-b['points'],axis=-1)
    usable_both = (f['weight']>.02)&(b['weight']>.02)&np.isfinite(delta)
    consistency = np.ones_like(delta)
    consistency[usable_both] = 1/(1+(delta[usable_both]/scale)**2)
    for direction in directions: direction['weight'] *= consistency
    # A bimodal distribution can still discriminate between body paths even
    # when its global variance makes a single decoded coordinate unreliable.
    # Score the full marginals separately; do not multiply their evidence by
    # coordinate precision again. Compare marginals before downweighting them.
    overlap=np.sqrt(f['probabilities']*b['probabilities']).sum(-1).mean(-1).clip(0,1)
    distribution_both=(f['distribution_weight']>.05)&(b['distribution_weight']>.05)
    for direction in directions:
        direction['distribution_weight']*=np.where(distribution_both,overlap,1.)
    for i,mask in enumerate(d['masks']):
        entries=[]
        for direction in directions:
            probs=direction['probabilities'][i]
            # Integrate a small spatial neighborhood; candidate centerlines
            # and physical texture points need not coincide to subpixel scale.
            mass=np.empty_like(probs)
            for axis,extent in enumerate([mask.shape[1],mask.shape[0]]):
                radius=max(1,int(np.ceil(.25*scale*256/extent)))
                mass[:,axis]=ndi.uniform_filter1d(probs[:,axis],2*radius+1,axis=-1,mode='constant')*(2*radius+1)
            entries.append(dict(points=direction['points'][i],weight=direction['weight'][i],mass=mass,
                                distribution_weight=direction['distribution_weight'][i],
                                peak_mass=mass.max(-1),extent=np.array([mask.shape[1],mask.shape[0]])))
        both=distribution_both[i]&np.isfinite(delta[i])
        result['frames'].append(dict(directions=entries, scale=scale,
            diagnostic=dict(tap_effective_points=float(sum(e['weight'].sum() for e in entries)/2),
                tap_distribution_effective_points=float(sum(e['distribution_weight'].sum() for e in entries)/2),
                tap_distribution_overlap=float(np.median(overlap[i,both])) if both.any() else None,
                tap_bidirectional_points=int(both.sum()),
                tap_bidirectional_error_px=float(np.median(delta[i,both])) if both.any() else None,
                tap_conflict_fraction=float(np.mean(delta[i,both]>scale)) if both.any() else None,
                tap_backward_reversed=result['backward_reversed'])))
    return result


def candidate_cost(points, frame):
    """Soft, bounded evidence cost; geometric admissibility is enforced outside."""
    p=np.asarray(points)[INDICES];total=0.
    for e in frame['directions']:
        residual=np.nan_to_num(np.linalg.norm(p-e['points'],axis=-1)/frame['scale'],nan=1e3)
        spatial=np.minimum(residual**2,16.)
        pixel_bins=p/e['extent']*256-.5
        likelihood=[]
        for axis in range(2):
            mass=np.array([np.interp(x,np.arange(256),e['mass'][j,axis],left=0.,right=0.) for j,x in enumerate(pixel_bins[:,axis])])
            likelihood.append(np.minimum(-np.log(np.maximum(mass,1e-8)/np.maximum(e['peak_mass'][:,axis],1e-8)),8.))
        loss=e['weight']*spatial+.5*e['distribution_weight']*np.maximum(np.mean(likelihood,axis=0),0.)
        total+=float(np.mean(loss))
    return total/2
