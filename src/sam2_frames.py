"""Keep the Mac tracker's normalized JPEG inputs on demand."""
from pathlib import Path

from platforms.system import is_darwin
from tempfile import TemporaryDirectory


def sam2_input_transform(mode, capture=None, box=None):
    """Prepare model input for one source-coordinate chamber. The caller positions the decoder afterwards."""
    if mode in ('none', 'edge_refine'):
        return lambda frame: frame
    import cv2
    if mode == 'clahe':
        equalizer = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

        def enhance(frame):
            image = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
            image[:, :, 0] = equalizer.apply(image[:, :, 0])
            return cv2.cvtColor(image, cv2.COLOR_YCrCb2BGR)

        return enhance
    if mode != 'static_background':
        raise ValueError(f'Unknown SAM2 input contrast mode: {mode}')
    import numpy as np
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    count = min(17, total if total % 2 else total - 1)
    if count < 3:
        raise ValueError('固定背景抑制至少需要三帧')
    x, y, right, bottom = box
    samples = np.empty((bottom-y, right-x, count), dtype=np.uint8)
    for j, frame in enumerate(np.linspace(0, total-1, count, dtype=int)):
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(frame))
        ok, source = capture.read()
        if not ok:
            raise ValueError(f'无法读取固定背景参照帧 {frame}')
        samples[:, :, j] = cv2.cvtColor(source[y:bottom, x:right], cv2.COLOR_BGR2GRAY)
    middle = count // 2
    samples.partition(middle, axis=-1)
    background = samples[:, :, middle].copy()

    def suppress_background(frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Retain some original contrast where the animal hardly moves.
        signal = cv2.addWeighted(gray, 2.85, background, -2.5, 83.2)
        return cv2.cvtColor(signal, cv2.COLOR_GRAY2BGR)

    return suppress_background


def refine_mask_edges(image, mask):
    """Fit the SAM2 outline to local image colors while retaining thin paths and visible image-edge contacts."""
    import cv2
    import numpy as np
    from skimage.morphology import skeletonize

    x, y, width, height = cv2.boundingRect(mask.view(np.uint8))
    if not width:
        return mask
    local = mask[y:y+height, x:x+width]
    radius = max(2, round(.2 * np.count_nonzero(local) / max(width, height)))
    pad = radius + 3
    ys = slice(max(0, y-pad), min(mask.shape[0], y+height+pad))
    xs = slice(max(0, x-pad), min(mask.shape[1], x+width+pad))
    inside = mask[ys, xs].astype(np.uint8)
    depth = cv2.distanceTransform(inside, cv2.DIST_L2, 5)
    if depth.max() <= radius:
        return mask
    outer = cv2.distanceTransform(1-inside, cv2.DIST_L2, 5) > radius
    if np.count_nonzero(outer) < 5:
        return mask
    labels = np.where(inside, cv2.GC_PR_FGD, cv2.GC_PR_BGD).astype(np.uint8)
    labels[outer] = cv2.GC_BGD
    labels[(depth > radius) | skeletonize(inside)] = cv2.GC_FGD
    # Preserve how the body meets the crop, including single-pixel contacts.
    for side in (0, -1):
        labels[side, :] = inside[side, :]
        labels[:, side] = inside[:, side]
    if np.count_nonzero(labels == cv2.GC_FGD) < 5:
        return mask
    roi = image[ys, xs]
    if np.array_equal(roi.min(axis=(0, 1)), roi.max(axis=(0, 1))):
        return mask
    cv2.setRNGSeed(3)
    cv2.grabCut(roi, labels, None, np.zeros((1, 65)), np.zeros((1, 65)), 2, cv2.GC_INIT_WITH_MASK)
    # Only trim the halo. A similarly colored pillar outside the predicted body
    # must not grow a new branch. Smooth short notches from internal texture.
    trim = (labels & 1) & inside
    size = 2 * max(1, round(.6 * radius)) + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    trim = cv2.morphologyEx(trim, cv2.MORPH_CLOSE, kernel) & inside
    for side in (0, -1):
        trim[side, :] = inside[side, :]
        trim[:, side] = inside[:, side]
    result = mask.copy()
    result[ys, xs] = trim
    return result


class TrackingFrames:
    def __init__(self, directory, count, image_size):
        import torch
        self.directory = Path(directory)
        self.count = count
        self.image_size = image_size
        self.mean = torch.tensor([.485, .456, .406], dtype=torch.float32)[:, None, None]
        self.std = torch.tensor([.229, .224, .225], dtype=torch.float32)[:, None, None]

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        from sam2.utils.misc import _load_img_as_tensor
        if not 0 <= index < self.count:
            raise IndexError(index)
        frame, _, _ = _load_img_as_tensor(self.directory / f'{index:06d}.jpg', self.image_size)
        # Match the dtype and layout of the upstream preallocated NCHW tensor.
        return frame.float().contiguous().sub_(self.mean).div_(self.std)


def init_tracking_state(model, directory, count, object_count=1):
    options = dict(offload_video_to_cpu=True, offload_state_to_cpu=True)
    if not is_darwin():
        if getattr(getattr(model, 'device', None), 'type', None) != 'cuda':
            return model.init_state(str(directory), **options)
        import torch
        free, _ = torch.cuda.mem_get_info(model.device)
        # Leave activation space for the backbone and full-resolution mask logits.
        budget = min(free // 2, max(0, free - 2 * 1024**3))
        pixels = model.image_size ** 2 * count
        history_bytes = 2 * pixels * object_count  # low-res masks and memory features
        state_on_gpu = history_bytes <= budget
        images_on_gpu = state_on_gpu and 12 * pixels + history_bytes <= budget
        try:
            state = model.init_state(str(directory), offload_video_to_cpu=not images_on_gpu,
                                     offload_state_to_cpu=not state_on_gpu)
        except torch.cuda.OutOfMemoryError:
            if not (state_on_gpu or images_on_gpu):
                raise
        else:
            print(f'SAM2 帧缓存 {"GPU" if images_on_gpu else "CPU"}，跟踪状态 {"GPU" if state_on_gpu else "CPU"}', flush=True)
            return state
        torch.cuda.empty_cache()
        print('显存不足，SAM2 帧和状态改存内存，推理继续使用 GPU。', flush=True)
        return model.init_state(str(directory), **options)
    # Upstream warms its backbone on frame zero during init_state. Give it only
    # that frame, then supply random access for prompts and both directions.
    with TemporaryDirectory(prefix='wormstudio-sam2-init-') as seed:
        try:
            (Path(seed) / '000000.jpg').symlink_to((directory / '000000.jpg').resolve())
        except OSError:
            import shutil
            shutil.copy2(directory / '000000.jpg', Path(seed) / '000000.jpg')
        state = model.init_state(seed, **options)
    state['images'] = TrackingFrames(directory, count, model.image_size)
    state['num_frames'] = count
    return state


def propagate_frames(model, state, start_frame_idx, reverse=False):
    """Release obsolete non-prompt tensors during a sequential Mac tracking pass."""
    stream = model.propagate_in_video(state, start_frame_idx=start_frame_idx, reverse=reverse)
    if not is_darwin():
        yield from stream
        return
    # A strided lookup can reach one extra stride beyond its aligned frame.
    window = max(model.num_maskmem * model.memory_temporal_stride_for_eval,
                 model.max_obj_ptrs_in_encoder, 1)
    for frame, ids, masks in stream:
        yield frame, ids, masks
        for outputs in state['output_dict_per_obj'].values():
            history = outputs['non_cond_frame_outputs']
            for index in list(history):
                # The reverse pass starts at the same anchor and can read the
                # forward outputs immediately after it, including non-prompt starts.
                needed_for_reverse = not reverse and start_frame_idx > 0 and abs(index - start_frame_idx) <= window
                if abs(index - frame) > window and not needed_for_reverse:
                    del history[index]
