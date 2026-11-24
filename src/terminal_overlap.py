"""Identify an end by a continuous length of its shaft occupying the same image coordinates."""
import numpy as np
from scipy.spatial.distance import cdist


# Search the terminal third, allowing the tip to slide along the shaft.
STATIONS = np.arange(33) * .01
TERMINAL_FRACTION = STATIONS[-1]
WINDOW = 9  # nine stations at 1% body length span 8% of the worm


def _stations(path, length):
    arc = np.r_[0.,np.cumsum(np.linalg.norm(np.diff(path,axis=0),axis=1))]
    distances = STATIONS * length
    distances = distances[distances <= arc[-1]]
    return np.column_stack([np.interp(distances,arc,path[:,axis]) for axis in (0,1)])


def _window_errors(reference, branch):
    """Best worst-point distance for every reference run, in both branch directions."""
    starts = np.arange(len(reference)-WINDOW+1)
    if len(branch) < WINDOW:
        missing = np.full(len(starts),np.inf)
        return missing,missing
    offsets = np.arange(WINDOW)
    left = starts[:,None,None]+offsets
    right = np.arange(len(branch)-WINDOW+1)[None,:,None]+offsets
    distances = cdist(reference,branch)
    forward = distances[left,right].max(axis=2).min(axis=1)
    reverse = distances[left,right[...,::-1]].max(axis=2).min(axis=1)
    return forward,np.minimum(forward,reverse)


def match_shaft(lines, start, end, side, branches, length):
    """Find one spatially fixed, uniquely supported run throughout a short break.

    Compare curves in the original image coordinates. No translation or rotation
    is fitted. The matching section may sit at a different distance from its tip.
    """
    step = 1 if end > start else -1
    frames = list(range(start,end+step,step))
    if len(frames) == 2:
        frames = [start-step,*frames,end+step]
    reference = _stations(lines[start,::-1] if side else lines[start],length)
    count = len(reference)-WINDOW+1
    if count <= 0:
        return None
    indices = np.arange(count)
    allowed = np.linalg.norm(reference[WINDOW-1:]-reference[:count],axis=1) >= .06*length
    tolerance = max(1.5,.015*length)
    separation = max(2.,.025*length)
    worst = np.zeros(count)
    choices = {}
    tip_tracks = []
    for frame in frames:
        paths = branches[frame] if frame in branches else (lines[frame],lines[frame,::-1])
        if not len(paths):
            return None
        forward, either = (np.stack(values) for values in zip(*[
            _window_errors(reference,_stations(path,length)) for path in paths]))
        winner = forward.argmin(axis=0)
        error = forward[winner,indices]
        # An opposite arm occupying the same strip also vetoes the match when
        # that arm runs through it in the opposite direction.
        either[winner,indices] = np.inf
        allowed &= (error <= tolerance) & (either.min(axis=0) > error+separation)
        if frame in (start,start-step):
            allowed &= winner == side
        if frame == end+step:
            allowed &= winner == choices[end]
        if not allowed.any():
            return None
        worst = np.maximum(worst,error)
        choices[frame] = winner
        tip_tracks.append(np.asarray([path[0] for path in paths])[winner])
    # A stationary strip cannot identify a distant tip that jumps onto it.
    allowed &= np.all(np.linalg.norm(np.diff(tip_tracks,axis=0),axis=2) <= .35*length,axis=0)
    start_tips = tip_tracks[frames.index(start)]
    end_tips = tip_tracks[frames.index(end)]
    allowed &= np.linalg.norm(end_tips-start_tips,axis=1) <= .45*length
    valid = indices[allowed]
    if not len(valid) or any(len(np.unique(v[valid])) != 1 for v in choices.values()):
        return None
    best = valid[worst[valid].argmin()]
    observed = [dict(frame=i,xy=paths[choices[i][best]][0].tolist()) for i,paths in branches.items()]
    return int(choices[end][best]), dict(frames=frames, run_xy=reference[best:best+WINDOW].tolist(),
        run_length_px=STATIONS[WINDOW-1]*length, max_distance_px=float(worst[best]), observed_gap_tips=observed)
