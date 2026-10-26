"""Array and serialization utilities from the validated pipeline."""
import numpy as np

def resample(points, count=101):
    s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    keep = np.r_[True, np.diff(s) > 1e-8]
    if s[-1] < 1:
        raise ValueError('Degenerate centerline')
    return np.column_stack([np.interp(np.linspace(0, s[-1], count), s[keep], points[keep, j]) for j in (0, 1)])

def runs(valid):
    indices=np.flatnonzero(valid)
    return np.split(indices,np.where(np.diff(indices)>1)[0]+1) if len(indices) else []
