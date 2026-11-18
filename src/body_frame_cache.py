"""Exact source pixels with disk-backed storage for long body sequences."""
from contextlib import contextmanager, ExitStack
from tempfile import TemporaryFile

import cv2
import numpy as np


MEMORY_LIMIT = 256 * 1024 * 1024


@contextmanager
def cached_frames(source, get_mask, worm, progress=None, memory_limit=MEMORY_LIMIT):
    """Decode once; preserve full resolution, all frames and random-access order.

    Small clips use arrays. Large clips use temporary mappings so the OS can
    reclaim image pages while the solver still optimizes the whole sequence.
    Anonymous/delete-on-close files also disappear if the worker is terminated.
    """
    x, y, right, bottom = source['crop_xyxy']
    shape = (source['frames'], bottom-y, right-x)
    if min(shape) <= 0:
        raise ValueError('虫体帧缓存的帧数或裁剪尺寸无效')
    with ExitStack() as stack:
        on_disk = 2 * int(np.prod(shape)) > memory_limit

        def allocate(name, dtype):
            if not on_disk:
                return np.empty(shape, dtype=dtype)
            handle = stack.enter_context(TemporaryFile(prefix='wormstudio-body-'+name+'-'))
            array = np.memmap(handle, mode='w+', dtype=dtype, shape=shape)
            stack.callback(array._mmap.close)
            return array

        gray = allocate('gray.bin', np.uint8)
        masks = allocate('masks.bin', np.bool_)
        cap = cv2.VideoCapture(source['video'])
        try:
            for i in range(shape[0]):
                ok, image = cap.read()
                if not ok:
                    raise ValueError(f'源视频在 F{i} 读取失败')
                crop = cv2.cvtColor(image[y:bottom, x:right], cv2.COLOR_BGR2GRAY)
                mask = get_mask(worm, i)
                if crop.shape != shape[1:] or mask.shape != shape[1:]:
                    raise ValueError(f'源视频或掩膜在 F{i} 的尺寸与裁剪配置不匹配')
                gray[i] = crop
                masks[i] = mask
                if progress:
                    progress(i+1, shape[0])
        finally:
            cap.release()
        for array in (gray, masks):
            if isinstance(array, np.memmap):
                array.flush()
            array.flags.writeable = False
        yield gray, masks
