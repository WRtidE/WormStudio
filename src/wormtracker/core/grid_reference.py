"""Register a rigid microfluidic chip to its reference walls at the counting line."""
import cv2
import numpy as np


class ChannelGridReference:
    def __init__(self, frame, grid, line_ratio):
        self.shape = frame.shape[:2]
        self.line_ratio = line_ratio
        self.grid = grid
        bounds = grid['channel_bounds']
        width = self.shape[1]
        pitch = (bounds[-1][1] - bounds[0][0]) / len(bounds)
        margin = max(4, round(pitch))
        self.left = max(0, bounds[0][0] - margin)
        self.right = min(width, bounds[-1][1] + margin)
        self.template = self._profile(frame)[:, self.left:self.right].copy()

    def _profile(self, frame):
        h = frame.shape[0]
        y = int(h * self.line_ratio)
        radius = max(5, h // 14)
        strip = frame[max(0, y-radius):min(h, y+radius+1):max(1, radius//24)]
        if strip.ndim == 3:
            strip = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
        profile = strip.mean(axis=0, dtype=np.float32)[None, :]
        return cv2.GaussianBlur(profile, (5, 1), 0) - cv2.GaussianBlur(profile, (31, 1), 0)

    def locate(self, frame):
        if frame.shape[:2] != self.shape or self.template.std() < 0.5:
            raise RuntimeError('无法定位参考通道壁')
        scores = cv2.matchTemplate(self._profile(frame), self.template, cv2.TM_CCOEFF_NORMED).ravel()
        peak = int(scores.argmax())
        if scores[peak] < 0.65:
            raise RuntimeError('通道壁与参考帧不匹配')
        # Reject an equally strong maximum in another lane instead of guessing
        # the channel number from a periodic section of the chip.
        separation = max(4, round(self.grid['channel_width'] / 2))
        if (np.any(scores[:max(0, peak-separation)] >= scores[peak] - .005)
                or np.any(scores[peak+separation:] >= scores[peak] - .005)):
            raise RuntimeError('存在多个通道壁匹配位置')
        offset = peak - self.left
        grid = self.grid.copy()
        for key in ('channel_bounds', 'wall_bounds'):
            grid[key] = [(l + offset, r + offset) for l, r in grid[key]]
        grid.update(roi_left=grid['roi_left']+offset, roi_right=grid['roi_right']+offset, offset_x=offset)
        return grid
