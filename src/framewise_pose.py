"""Single moving dark worm in a fixed field. All public coordinates are field-local XY."""
import cv2
import numpy as np
from scipy import ndimage

VERSION = 'registered-whole-worm-v2'


def arc_midpoint(line):
    """Interpolate at half the polyline length, including nonuniform samples."""
    points = np.asarray(line, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2 or not np.isfinite(points).all():
        return None
    arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    if arc[-1] <= 0:
        return None
    return np.array([np.interp(arc[-1] / 2, arc, axis) for axis in points.T])


def observed_pose(mask):
    """Return a measured path only when supported by a continuous visible silhouette."""
    from analyze_phenotypes import centerline
    x, y, w, h = cv2.boundingRect(mask.astype(np.uint8))
    if min(w, h) < 3:
        return None, 'missing_mask'
    if x == 0 or y == 0 or x+w == mask.shape[1] or y+h == mask.shape[0]:
        return None, 'image_boundary_truncation'
    patch = mask[y:y+h, x:x+w].astype(np.uint8)
    ratio = min(1., 320 / max(w, h))
    if min(w,h)*ratio < 1:
        return None, 'unresolved_body_width'
    small = cv2.resize(patch, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_NEAREST_EXACT)
    padded = np.pad(small, 2)
    try:
        path, _, quality = centerline(padded > 0)
    except ValueError:
        return None, 'centerline_missing'
    if quality['skeleton_path_fraction'] < .80 or quality['centerline_inside_fraction'] < .97:
        return None, 'branched_or_contact_skeleton'
    if np.linalg.norm(path[0]-path[-1]) < .12*np.linalg.norm(np.diff(path, axis=0), axis=1).sum():
        return None, 'overlapping_tips'
    line = (path - 2 + .5) * [w / small.shape[1], h / small.shape[0]] + [x-.5, y-.5]
    # At a tapered tip the skeleton pixel is inside the resized mask, but its
    # mapped subpixel position can round just outside the native silhouette.
    tolerance = int(np.ceil(1 / ratio))
    for end in (0, -1):
        tx, ty = np.rint(line[end]).astype(int)
        if 0 <= ty < mask.shape[0] and 0 <= tx < mask.shape[1] and mask[ty,tx]:
            continue
        left, top = max(0,tx-tolerance), max(0,ty-tolerance)
        yy, xx = np.nonzero(mask[top:ty+tolerance+1, left:tx+tolerance+1])
        if not len(xx):
            return None, 'landmark_outside_mask'
        pixels = np.column_stack([xx+left, yy+top])
        distance = np.linalg.norm(pixels-line[end],axis=1)
        if distance.min() > tolerance:
            return None, 'landmark_outside_mask'
        line[end] = pixels[distance.argmin()]
    mid = arc_midpoint(line)
    index = np.rint(np.vstack([line[0], mid, line[-1]])).astype(int)
    if (index < 0).any() or (index >= mask.shape[::-1]).any() or not mask[index[:,1], index[:,0]].all():
        return None, 'landmark_outside_mask'
    return line, ''


class FieldDetector:
    """A bounded set of registered frames estimates the stationary well and pillars."""
    def __init__(self, capture, field, frame_count):
        self.field = field
        x, y, r, b = field
        scale = min(1., 960 / max(r-x, b-y))
        self.size = max(1, round((r-x)*scale)), max(1, round((b-y)*scale))
        self.factor = np.array(self.size) / [r-x, b-y]
        samples = []
        for i in np.unique(np.linspace(0, frame_count-1, min(23, frame_count)).astype(int)):
            capture.set(cv2.CAP_PROP_POS_FRAMES, int(i))
            ok, im = capture.read()
            if not ok:
                raise ValueError(f'无法读取背景采样帧 {i}')
            samples.append(self.gray(im))
        self.reference = samples[len(samples)//2].astype(np.float32)
        self.window = cv2.createHanningWindow(self.size, cv2.CV_32F) if min(self.size)>1 else None
        aligned = [self.warp(im, -self.shift(im)) for im in samples]
        self.background = np.percentile(aligned, 80, axis=0).astype(np.uint8)

    def gray(self, frame):
        x, y, r, b = self.field
        crop = frame[y:b, x:r]
        return cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), self.size, interpolation=cv2.INTER_AREA)

    def shift(self, gray):
        if self.window is None:
            return np.zeros(2)
        offset, score = cv2.phaseCorrelate(self.reference.copy(), gray.astype(np.float32), self.window)
        return np.asarray(offset) if score > .2 and np.linalg.norm(offset) < .12*min(self.size) else np.zeros(2)

    def warp(self, image, shift):
        return cv2.warpAffine(image, np.float32([[1, 0, shift[0]], [0, 1, shift[1]]]),
                              self.size, borderMode=cv2.BORDER_REPLICATE)

    def detect(self, gray, previous_xy, expected_area=None):
        """Search the whole field anew. History ranks present components, never supplies pixels."""
        contrast = cv2.subtract(self.warp(self.background, self.shift(gray)), gray)
        strong = cv2.morphologyEx((contrast > 17).astype(np.uint8), cv2.MORPH_CLOSE,
                                  cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        _, labels, stats, centers = cv2.connectedComponentsWithStats(strong)
        area = stats[:, cv2.CC_STAT_AREA]
        select = (area >= 20) & (area < gray.size*.18)
        select[0] = False
        reference_area = expected_area * self.factor.prod() if expected_area else None
        if reference_area:
            select &= (area > reference_area*.23) & (area < reference_area*2.4)
        candidates = np.flatnonzero(select)
        x, y, r, b = self.field
        size = (r-x, b-y)
        if not len(candidates):
            return np.zeros(size[::-1], bool)
        distances = np.linalg.norm(centers[candidates]-previous_xy*self.factor, axis=1)
        distance_scale = max(16., np.sqrt(reference_area or area[candidates].max())*2.5)
        scores = np.log(area[candidates]) - np.log1p((distances/distance_scale)**2)
        target = candidates[np.argmax(scores)]
        bx, by, w, h, _ = stats[target]
        ys = slice(max(0, by-6), min(gray.shape[0], by+h+6))
        xs = slice(max(0, bx-6), min(gray.shape[1], bx+w+6))
        seed = labels[ys,xs] == target
        grown = ndimage.binary_propagation(seed, mask=seed | (contrast[ys,xs] > 8))
        grown = cv2.morphologyEx(grown.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3,3), np.uint8))
        # Pillar rims may join the silhouette through a thin edge. Estimate the
        # worm width from its interior before removing attached narrow ridges.
        distance = cv2.distanceTransform(grown, cv2.DIST_L2, 5)
        radius = max(1, min(12, round(.33*np.quantile(distance[grown>0], .88))))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*radius+1, 2*radius+1))
        opened = cv2.morphologyEx(grown, cv2.MORPH_OPEN, kernel)
        n, parts, sizes, _ = cv2.connectedComponentsWithStats(opened)
        mask = np.zeros_like(gray)
        if n > 1:
            mask[ys,xs] = parts == 1 + sizes[1:,cv2.CC_STAT_AREA].argmax()
        return cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST_EXACT).astype(bool)


def scan_video(video, field, anchor_frame, seed_xy, on_frame, anchor_masks=None):
    """Visit every frame from the anchor in both directions. Reverse decoding is batched."""
    capture = cv2.VideoCapture(str(video))
    try:
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not capture.isOpened() or not 0 <= anchor_frame < count:
            raise ValueError('视频或标注帧无效')
        detector = FieldDetector(capture, field, count)
        for reverse in (False, True):
            center = np.asarray(seed_xy, float)
            anchors = anchor_masks or {}
            areas = [int(anchors[anchor_frame].sum())] if anchor_frame in anchors else []
            start = anchor_frame
            if not reverse:
                capture.set(cv2.CAP_PROP_POS_FRAMES, start)
            while (start > 0 if reverse else start < count):
                lo, hi = (max(0, start-16), start) if reverse else (start, min(count, start+16))
                if reverse:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, lo)
                batch = []
                for index in range(lo, hi):
                    ok, frame = capture.read()
                    if not ok:
                        raise ValueError(f'视频在第 {index} 帧解码失败')
                    batch.append((index, detector.gray(frame)))
                for index, gray in reversed(batch) if reverse else batch:
                    if index in anchors:
                        mask = anchors[index]
                        areas = [int(mask.sum())]
                    else:
                        mask = detector.detect(gray, center, np.median(areas) if areas else None)
                    moments = cv2.moments(mask.astype(np.uint8), binaryImage=True)
                    area = moments['m00']
                    if area:
                        center = np.array([moments['m10'], moments['m01']])/area
                        if not areas or .65*np.median(areas) < area < 1.4*np.median(areas):
                            areas = (areas+[area])[-15:]
                    on_frame(index, mask)
                start = lo if reverse else hi
    finally:
        capture.release()
