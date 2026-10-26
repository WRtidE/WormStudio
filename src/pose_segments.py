"""Split pose work at rejected masks; never optimize across inter-worm contact."""
import numpy as np


def accepted_segments(count, worm, bad, points=None):
    valid=np.array([not bad.get((f,worm),False) for f in range(count)])
    if points is not None: valid &= np.isfinite(points).all(axis=(1,2))
    indexes=np.flatnonzero(valid)
    return [part for part in np.split(indexes,np.where(np.diff(indexes)>1)[0]+1) if len(part)]


def rejected_quality(count):
    return [dict(frame=f,mask_valid=False,centerline_valid=False,topology='mask_excluded',
        quality_reasons=['tracking_contact_or_area_outlier'],selected_candidate='none',branch_score_margin=None) for f in range(count)]
