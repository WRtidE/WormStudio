"""Input rules shared by the CLI, workspace and annotation sessions."""
import re


RESERVED_NAMES = {'con', 'prn', 'aux', 'nul', *(f'{p}{i}' for p in ('com', 'lpt') for i in range(1, 10))}


def reserved_filename(name):
    return name.split('.')[0].lower() in RESERVED_NAMES


def valid_sample_name(name):
    return (isinstance(name, str) and bool(re.fullmatch(r'[\w][\w.-]{0,79}', name))
            and not name.endswith('.') and name.split('.')[0].lower() != 'all' and not reserved_filename(name))


def annotation_mask(value, shape):
    import base64
    import cv2
    import numpy as np
    prefix = 'data:image/png;base64,'
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError('掩膜必须为 PNG')
    try:
        data = base64.b64decode(value[len(prefix):], validate=True)
        pixels = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    except (ValueError, cv2.error) as error:
        raise ValueError('虫体掩膜无法读取') from error
    if pixels is None or pixels.shape[:2] != shape:
        raise ValueError('掩膜尺寸与标注图不匹配')
    if pixels.ndim == 2:
        return pixels > 127
    mask = pixels[:, :, 0] > 127
    if pixels.shape[2] == 4:
        mask &= pixels[:, :, 3] > 0
    return mask
