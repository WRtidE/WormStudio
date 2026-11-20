"""Raw-image optical flow used by the body-sequence solver."""
import cv2
import numpy as np


def lk_step(a, b, p, fallback_velocity=None):
    """Raw-image optical flow, with forward/backward consistency checks."""
    x = p.astype(np.float32).reshape(-1, 1, 2)
    def track(points, level):
        options = dict(winSize=(31, 31), maxLevel=level,
                       criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 35, .01))
        nxt, status, error = cv2.calcOpticalFlowPyrLK(a, b, points, None, **options)
        back, status2, _ = cv2.calcOpticalFlowPyrLK(b, a, nxt, None, **options)
        fb = np.linalg.norm(back[:, 0] - points[:, 0], axis=1)
        delta = nxt[:, 0] - points[:, 0]
        good = ((status[:, 0] > 0) & (status2[:, 0] > 0) & (fb < 3) & (error[:, 0] < 30)
                & (np.linalg.norm(delta, axis=1) < 100))
        good &= np.isfinite(delta).all(axis=1) & np.isfinite(fb)
        return delta, good, fb
    delta, good, fb = track(x, 4)
    # A coarse pyramid can drag points toward borders/background. Retry failed
    # points locally, retaining exactly the same consistency/appearance gates.
    failed = np.flatnonzero(~good)
    if len(failed):
        retry, reliable, retry_fb = track(x[failed], 1)
        accepted = failed[reliable]
        delta[accepted] = retry[reliable]
        fb[accepted] = retry_fb[reliable]
        good[accepted] = True
    fallback = np.zeros_like(p) if fallback_velocity is None else np.clip(fallback_velocity, -35, 35)
    delta = np.where(good[:, None], delta, fallback)
    return p + delta, good, fb
