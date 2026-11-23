"""Unicode-safe image reads and snapshot-safe PNG writes."""
from pathlib import Path

import cv2
import numpy as np
from run_storage import replace_bytes


def read_image(path, gray=False):
    im = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), np.uint8), 0 if gray else 1)
    if im is None:
        raise ValueError(f"Cannot decode {path}")
    return im


def write_png(path, image):
    replace_bytes(path, png(image))


def png(image):
    ok, data = cv2.imencode('.png', image)
    if not ok: raise ValueError('PNG encoding failed')
    return data.tobytes()
