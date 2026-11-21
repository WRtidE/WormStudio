"""Keep the Mac tracker's normalized JPEG inputs on demand."""
from pathlib import Path
import sys

from platforms.system import is_darwin
from tempfile import TemporaryDirectory


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


def init_tracking_state(model, directory, count):
    options = dict(offload_video_to_cpu=True, offload_state_to_cpu=True)
    if not is_darwin():
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
