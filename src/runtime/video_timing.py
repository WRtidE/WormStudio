"""Verify the supported constant-frame-rate timebase before temporal measurement."""
import cv2
import numpy as np


def verify_timebase(path, count, fps):
    cap=cv2.VideoCapture(str(path)); timestamps=[]
    try:
        while True:
            ok,_=cap.read()
            if not ok: break
            timestamps.append(cap.get(cv2.CAP_PROP_POS_MSEC)/1000.)
    finally: cap.release()
    if len(timestamps)!=count: raise ValueError('视频逐帧解码数量不一致，不能计算时序指标')
    values=np.asarray(timestamps,float)
    if not np.isfinite(values).all() or len(values)<2 or not np.all(np.diff(values)>0):
        raise ValueError('视频时间戳缺失或非单调，不能使用名义帧率推算运动')
    intervals=np.diff(values)
    status='decoded_pts_verified_cfr'
    if not np.allclose(intervals,1/fps,atol=.001,rtol=.001):
        # A trimmed MP4 can begin partway through a frame. Only accept a short
        # first interval followed by a verified regular cadence.
        period=float(np.median(intervals[1:])) if len(intervals)>=3 else 0
        if not (0 < intervals[0] < period and np.allclose(intervals[1:], period, atol=.001,rtol=.001)
                and np.isclose(period,1/fps,atol=.001,rtol=.001)):
            raise ValueError('视频中途存在变帧率或时间跳变，无法可靠计算运动指标')
        status='decoded_pts_short_first_interval'
        fps=1/period
    values-=values[0]
    return dict(status=status,frames=count,fps=fps,
        timestamps_s=values.tolist(),first_to_last_seconds=float(values[-1]),
        method='OpenCV decoded CAP_PROP_POS_MSEC; all frame intervals checked')
