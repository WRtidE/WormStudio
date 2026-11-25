"""Read-only measurements of a confirmed annotation frame, without tracking."""
from input_validation import annotation_mask
import math

import cv2
import numpy as np
from scipy import ndimage

VERSION = 'annotation-frame-metrics-v2'


def measure_mask(mask, *, frame, worm_id, measurement_mode, scale_um_per_px=None, origin=(0, 0)):
    from measurement_units import validate_scale
    from framewise_pose import observed_pose, arc_midpoint
    from centerline_topology import geometry_quality
    from skeleton_geometry import silhouette_dimensions
    from phenotype_statistics import measure_posture

    units = dict(measurement_mode=measurement_mode, scale_um_per_px=scale_um_per_px)
    validate_scale(units)
    scale = 1. if measurement_mode == 'pixels' else float(scale_um_per_px)
    unit = 'px' if measurement_mode == 'pixels' else 'μm'
    mask = np.asarray(mask, dtype=bool)
    area = int(mask.sum())
    if mask.ndim != 2 or area < 20:
        raise ValueError('请先确认面积足够的虫体轮廓。')
    row = dict(mask_valid=True, centerline_valid=True)
    values = dict(length=None, width=None, area=float(area * scale ** 2),
                  extension=None, ratio=None, volume=None)
    line, reason, ignored = None, '', 0
    labels, count = ndimage.label(mask, structure=np.ones((3, 3)))
    sizes = np.bincount(labels.ravel()); sizes[0] = 0
    main = labels == sizes.argmax()
    extra = area - int(sizes.max())
    # Permit only tiny isolated speckles, never reconnect a broken body. Keep the
    # original mask/area intact and disclose every excluded pixel to the user.
    speckles_only = extra <= min(20, .001 * area)
    if count > 1 and not speckles_only:
        reason = '轮廓不连续，请补全虫体或擦除分离的小块。'
    else:
        pose_mask = main if count > 1 else mask
        ignored = extra
        line, failure = observed_pose(pose_mask)
        if line is None:
            reason = ('虫体触及分析区域边界，请扩大区域后重算。' if failure == 'image_boundary_truncation'
                      else '此帧折叠、遮挡或轮廓不清，单帧不能可靠确定整条中线；请换清晰帧，或进行整段分析。')
        else:
            quality = geometry_quality(line)
            if quality['local_hairpin'] or quality['projection_crossings']:
                line = None
                reason = '此帧中线存在折叠或交叉，需要结合前后帧分析。'
            elif ignored:
                reason = f'中线按主体提取；另有 {ignored} 个离散像素，面积仍包含全部标注轮廓。'
    center = None
    if line is not None:
        bounds = cv2.boundingRect(pose_mask.view(np.uint8))
        width_px, volume_px3 = silhouette_dimensions(pose_mask, line, bounds)
        length = float(np.linalg.norm(np.diff(line, axis=0), axis=1).sum() * scale)
        width = width_px * scale
        row.update(length_um=length, mean_width_um=width)
        measure_posture(row, line, scale)
        center = arc_midpoint(line)
        values.update(length=length, width=width, extension=row['body_extension'],
                      ratio=length / width if width > 0 else None,
                      volume=volume_px3 * scale ** 3)
    specs = [('length', '当前帧体长', unit), ('width', '当前帧平均体宽', unit),
             ('area', '当前帧面积', unit+'²'), ('extension', '当前帧身体伸展度', ''),
             ('ratio', '当前帧长宽比', ''), ('volume', '当前帧体积估算', unit+'³')]
    return dict(version=VERSION, scope='annotation_frame', frame=frame, worm_id=worm_id,
                measurement_mode=measurement_mode, scale_um_per_px=units['scale_um_per_px'],
                pose_available=line is not None, note=reason, ignored_component_pixels=ignored,
                metrics=[dict(key=key, label=label, value=float(values[key]) if values[key] is not None and math.isfinite(values[key]) else None,
                              unit=suffix, note=(reason if values[key] is None else
                                                '按圆形截面估算，非三维实测。' if key == 'volume' else
                                                '轮廓触及边界，仅为可见部分面积。' if key == 'area' and reason.startswith('虫体触及') else ''))
                         for key, label, suffix in specs],
                centerline=(line + origin).tolist() if line is not None else [],
                body_center=(center + origin).tolist() if center is not None else None)


def measure_annotation(annotation, body):
    if body.get('frame') != annotation.frame:
        raise ValueError('标注帧已改变，请刷新后重新计算。')
    worm_id = body.get('worm_id')
    if type(worm_id) is not int or not 1 <= worm_id <= 10000:
        raise ValueError('线虫编号无效。')
    if body.get('confirmed') is not True:
        raise ValueError('请先确认虫体轮廓，再计算当前帧指标。')
    h, w = annotation.image.shape[:2]
    roi = body.get('roi') or [0, 0, w, h]
    if (not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi)
            or not (0 <= roi[0] < roi[2] <= w and 0 <= roi[1] < roi[3] <= h)):
        raise ValueError('分析区域超出标注画面。')
    mask = annotation_mask(body.get('mask'), (h, w))
    x, y, right, bottom = roi
    inside = mask[y:bottom, x:right]
    if inside.sum() != mask.sum():
        raise ValueError('虫体轮廓超出分析区域，请调整区域或轮廓。')
    return measure_mask(inside, frame=annotation.frame, worm_id=worm_id,
                        measurement_mode=body.get('measurement_mode', 'physical'),
                        scale_um_per_px=body.get('scale_um_per_px'), origin=(x, y))
