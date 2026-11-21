"""Explicit pixel/physical units; never label an uncalibrated value as micrometres."""
import math


def pixel_mode(record):
    return record.get('measurement_mode') == 'pixels'


def metric_key(key, record):
    return key.replace('_um', '_px') if pixel_mode(record) else key


def validate_scale(record):
    mode = record.get('measurement_mode', 'physical')
    if mode not in ('pixels', 'physical'):
        raise ValueError('measurement_mode 必须为 pixels 或 physical')
    if mode == 'pixels':
        record.update(measurement_mode=mode, scale_um_per_px=None,
                      scale_source='Uncalibrated pixel measurements; user selected pixel mode')
        return
    value = record.get('scale_um_per_px')
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('请填写正数 μm/px，或明确选择仅像素单位')


def pixel_fields(record):
    """Convert internal scale-one working fields before any public serialization."""
    record['measurement_mode'] = 'pixels'
    for key in list(record):
        if '_um' in key and key != 'scale_um_per_px':
            record[key.replace('_um', '_px')] = record.pop(key)
    return record
