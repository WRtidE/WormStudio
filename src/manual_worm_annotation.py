"""Local browser annotation with optional SAM2 assisted first-frame masks."""
import base64
import copy
import gc
import json
import math
import multiprocessing
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pose.centerline_methods import display_method, analysis_method, CURRENT_METHOD, CURRENT_HEAD_DETECTION
from tracking.sam2_models import model_spec
from pathlib import Path

import cv2
import numpy as np

from runtime.resource_paths import PROJECT_ROOT as ROOT, RESOURCE_ROOT, sam2_checkpoint
from platforms.device import release_device_cache

from platforms.system import is_darwin

def png(image):
    ok, data = cv2.imencode('.png', image)
    if not ok: raise ValueError('PNG encoding failed')
    return data.tobytes()


class MaskPredictor:
    def __init__(self, image, model_size):
        self.image = image
        self.model_size = model_size
        self.predictor = None
        self.predictor_roi = None
        self.prediction_cache = {}

    def predict(self, body):
        """Predict first-frame mask candidates from points or a box."""
        h, w = self.image.shape[:2]
        roi = body.get('roi') or [0, 0, w, h]
        if not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi):
            raise ValueError('分析区域必须是 [左,上,右,下] 四个整数')
        x0, y0, x1, y1 = roi
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h): raise ValueError('分析区域超出标注画面')
        points = body.get('points') or []
        if not isinstance(points, list) or len(points) > 100:
            raise ValueError('SAM2 点位格式错误或数量过多')
        coords, labels = [], []
        for point in points:
            if not isinstance(point, dict): raise ValueError('SAM2 点位格式错误')
            xx, yy, label = point.get('x'), point.get('y'), point.get('label')
            if type(label) is not int or label not in (0, 1) or not all(
                isinstance(v, (int, float)) and math.isfinite(v) for v in (xx, yy)):
                raise ValueError('SAM2 点位和正负标签无效')
            if not (x0 <= xx < x1 and y0 <= yy < y1): raise ValueError('SAM2 点位在分析区域外')
            coords.append([xx - x0, yy - y0]); labels.append(label)
        box = body.get('box')
        if box is not None:
            if (not isinstance(box, list) or len(box) != 4 or
                not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)):
                raise ValueError('SAM2 框选坐标格式错误')
            a, b, c, d = box
            if not (x0 <= a < c <= x1 and y0 <= b < d <= y1 and (c-a)*(d-b) >= 16):
                raise ValueError('SAM2 框选区域过小或超出分析区域')
            local_box = np.asarray([a-x0, b-y0, c-x0, d-y0], np.float32)
        else:
            local_box = None
        if not any(labels) and local_box is None:
            raise ValueError('请先点一个虫体正点，或框住虫体')
        worm_id=body.get('worm_id',1)
        if type(worm_id) is not int or not 1<=worm_id<=10000: raise ValueError('线虫编号无效')
        import torch
        from platforms.device import selected_device, inference_context
        device = selected_device()
        if self.predictor is None:
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor
            spec=model_spec(self.model_size)
            checkpoint = sam2_checkpoint(spec['checkpoint'])
            if not checkpoint.is_file(): raise RuntimeError(f'找不到 SAM2 权重：{checkpoint}')
            model = build_sam2(spec['config'], str(checkpoint), device=device)
            self.predictor = SAM2ImagePredictor(model)
        if self.predictor_roi != roi:
            with torch.inference_mode(), inference_context(device):
                self.predictor.set_image(cv2.cvtColor(self.image[y0:y1, x0:x1], cv2.COLOR_BGR2RGB))
            self.predictor_roi = roi.copy()
        previous=self.prediction_cache.get(worm_id)
        mask_input=None
        if previous and body.get('prediction_token')==previous['token']:
            old=previous['points']
            idx=body.get('previous_candidate',previous['winner'])
            if type(idx) is not int or not 0<=idx<previous['candidate_count']:
                raise ValueError('候选编号无效')
            if (previous['roi']==roi and previous['box']==box and len(points)>len(old)
                    and points[:len(old)]==old and previous['logits'] is not None):
                mask_input=previous['logits'][idx:idx+1]
        with torch.inference_mode(), inference_context(device):
            candidates, scores, logits = self.predictor.predict(
                point_coords=np.asarray(coords, np.float32) if coords else None,
                point_labels=np.asarray(labels, np.int32) if labels else None,
                box=local_box, mask_input=mask_input, multimask_output=mask_input is None)
        winner = int(np.argmax(scores))
        token=secrets.token_urlsafe(18)
        options=[]
        for index, candidate in enumerate(candidates):
            mask = np.zeros((h, w), np.uint8)
            mask[y0:y1,x0:x1]=candidate.astype(np.uint8)*255
            rgba = np.full((h,w,4),255,np.uint8);rgba[:,:,3]=mask
            options.append(dict(index=index,score=float(scores[index]),area_px=int(np.count_nonzero(mask)),
                mask='data:image/png;base64,'+base64.b64encode(png(rgba)).decode('ascii')))
        if options[winner]['area_px']<20:
            raise ValueError('未找到足够大的虫体，请追加虫体点或框住虫体')
        self.prediction_cache[worm_id]=dict(points=points,box=box,roi=roi,token=token,
                                            logits=logits,candidate_count=len(options),winner=winner)
        return dict(**options[winner], candidate=winner,candidates=options,
                    prediction_token=token,refined=mask_input is not None)


def annotation_worker(connection, image, model_size):
    # The child owns the model and Metal allocations for one annotation frame.
    with connection:
        predictor = MaskPredictor(image, model_size)
        while True:
            try:
                body = connection.recv()
            except EOFError:
                return
            try:
                result = predictor.predict(body)
            except ValueError as error:
                connection.send(('invalid', str(error)))
            except Exception as error:
                connection.send(('failed', str(error)))
                return
            else:
                connection.send(('ok', result))
                del result


class Annotation:
    def __init__(self, settings, frame=0):
        self.settings = dict(settings)
        if 'centerline_method' in settings:
            self.settings['centerline_method'] = analysis_method(settings['centerline_method'])
        sample = settings['sample']
        reserved={'all','con','prn','aux','nul'}|{f'{p}{i}' for p in ('com','lpt') for i in range(1,10)}
        if not re.fullmatch(r'[\w][\w.-]{0,79}', sample) or sample.endswith('.') or sample.split('.')[0].lower() in reserved:
            raise ValueError('请使用普通的 SAMPLE_NAME 文件夹名')
        video = Path(settings['video']).resolve()
        if not video.is_file(): raise ValueError(f'视频不存在：{video}')
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            cap.release(); raise ValueError('无法打开此视频，文件可能损坏或编码不受支持。请确认能正常播放，或转换为 H.264 MP4 后重试。')
        self.n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if self.n < 5:
            cap.release(); raise ValueError(f'视频只有 {self.n} 帧，分析至少需要 5 帧。请选择更长的视频。')
        if type(frame) is not int or not 0 <= frame < self.n:
            cap.release(); raise ValueError(f'标注帧超出范围，请输入 0–{self.n - 1} 之间的整数。')
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, image = cap.read(); cap.release()
        if not ok: raise ValueError(f'无法解码第 {frame} 帧，请尝试另一帧，或转换视频编码后重试。')
        h, w = image.shape[:2]
        self.crop = settings.get('crop_xyxy') or [0, 0, w, h]
        x, y, x1, y1 = self.crop
        if any(type(v) is not int for v in self.crop) or not (0 <= x < x1 <= w and 0 <= y < y1 <= h):
            raise ValueError('CROP_XYXY 超出原视频范围；可设为 None 使用完整画面')
        self.image = image[y:y1, x:x1]
        self.frame = frame
        self.video = str(video)
        self.output = ROOT / 'outputs' / sample / 'manual-initialization'
        self.path = self.output / 'annotation.json'
        stat = video.stat()
        self.identity = dict(video=self.video, size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                             frame=frame, display_crop=self.crop)
        self.token = secrets.token_urlsafe(24)
        self.done = threading.Event()
        self.result = None
        self.model_size = settings.get('sam2_model','large')
        if self.model_size not in ('large','tiny'): raise ValueError('SAM2_MODEL 必须为 large 或 tiny')
        self.local_predictor = None
        self.worker = None
        self.connection = None
        self.predict_lock = threading.Lock()

    def predict(self, body):
        with self.predict_lock:
            if self.done.is_set():
                raise ValueError('本次标注已保存')
            if not is_darwin():
                if self.local_predictor is None:
                    self.local_predictor = MaskPredictor(self.image, self.model_size)
                return self.local_predictor.predict(body)
            if self.worker is None:
                ctx = multiprocessing.get_context('spawn')
                parent, child = ctx.Pipe()
                worker = ctx.Process(target=annotation_worker, args=(child, self.image, self.model_size), daemon=True)
                try:
                    worker.start()
                except BaseException:
                    parent.close()
                    raise
                finally:
                    child.close()
                self.worker, self.connection = worker, parent
            try:
                self.connection.send(body)
                status, result = self.connection.recv()
            except (EOFError, OSError) as error:
                self._close_worker()
                raise RuntimeError('SAM2 标注进程已退出，可重新点选') from error
            if status == 'invalid':
                raise ValueError(result)
            if status == 'failed':
                self._close_worker()
                raise RuntimeError(result)
            return result

    def _close_worker(self):
        if self.worker is None:
            return
        self.connection.close()
        self.worker.join(timeout=2)
        if self.worker.is_alive():
            self.worker.kill()
            self.worker.join()
        self.worker.close()
        self.worker = self.connection = None

    def release_predictor(self):
        with self.predict_lock:
            self._close_worker()
            if self.local_predictor is not None:
                model = self.local_predictor.predictor
                device = model.device.type if model is not None else None
                del model
                self.local_predictor = None
                gc.collect()
                release_device_cache(device)

    def previous(self):
        if not self.path.exists(): return None
        record = json.loads(self.path.read_text(encoding='utf-8'))
        if record['input'] != self.identity:
            return None  # Show the new input and preserve the old record on save.
        config = record['config']
        if not all(Path(w['mask_path']).is_file() for w in config['groups'][0]['worms']):
            return None
        selection = self.output/'last-selection.json'
        if selection.is_file():
            config['centerline_method'] = analysis_method(json.loads(selection.read_text(encoding='utf-8'))['centerline_method'])
        if 'centerline_method' in config:
            config['centerline_method'] = analysis_method(config['centerline_method'])
        config['head_detection'] = self.settings.get('head_detection',config.get('head_detection','twardis'))
        config['review_intervals'] = self.settings.get('review_intervals', [])
        if 'chamber_id' in self.settings:
            config['chamber_id'] = self.settings['chamber_id']
        for key in ('identity_confirmations', 'source_invalid_frames'):
            if key in self.settings:config[key]=self.settings[key]
        if self.settings.get('scale_um_per_px') is not None:
            config['scale_um_per_px'] = self.settings['scale_um_per_px']
            config['scale_source'] = self.settings.get('scale_source', 'User override')
        return config

    def initial_state(self):
        """Restore saved, reviewed masks without restoring stale model prompts."""
        result=dict(algorithm=dict(available=False,checking=True,detail='正在检查分析组件…'),analysis_method=CURRENT_METHOD,sample=self.settings['sample'],frame=self.frame,
            width=self.image.shape[1],height=self.image.shape[0],token=self.token,
            measurement_mode=self.settings.get('measurement_mode','physical'),scale_um_per_px=self.settings.get('scale_um_per_px'),video_name=Path(self.video).name,
            frame_count=self.n,sam2_model=self.model_size,
            head_detection=self.settings.get('head_detection','twardis'),
            centerline_method=self.settings.get('centerline_method',CURRENT_METHOD),previous=None)
        if hasattr(self, 'correction_previous'):
            # Editing a historical run must keep its geometry pipeline compatible.
            result['analysis_method']=self.settings['centerline_method']
            result['previous'] = self.correction_previous
            result['mask_correction'] = dict(source_sample=self.correction_source['sample'], frame=self.frame)
            return result
        if not self.path.exists(): return result
        record=json.loads(self.path.read_text(encoding='utf-8'))
        if record.get('input')!=self.identity: return result
        config=record['config']
        selection=self.output/'last-selection.json'
        if selection.is_file():
            config['centerline_method']=analysis_method(json.loads(selection.read_text(encoding='utf-8'))['centerline_method'])
        x,y,x1,y1=config['crop_xyxy']
        x-=self.crop[0];x1-=self.crop[0];y-=self.crop[1];y1-=self.crop[1]
        if not 0<=x<x1<=result['width'] or not 0<=y<y1<=result['height']: return result
        worms=[]
        for entry in config['groups'][0]['worms']:
            path=Path(entry['mask_path'])
            if not path.is_file(): return result
            mask=cv2.imdecode(np.frombuffer(path.read_bytes(),np.uint8),0)
            if mask is None or mask.shape!=(y1-y,x1-x): return result
            rgba=np.full((*self.image.shape[:2],4),255,np.uint8);rgba[:,:,3]=0
            rgba[y:y1,x:x1,3]=(mask>127).astype(np.uint8)*255
            from head_tail.head_tail import anchors_in_crop
            heads=[a for a in anchors_in_crop(config.get('head_annotations',{}).get(str(entry['id'])),config['crop_xyxy'],config['crop_xyxy']) if a['frame']==self.frame]
            head_point=[heads[0]['point'][0]+x,heads[0]['point'][1]+y] if len(heads)==1 else None
            worms.append(dict(id=entry['id'],area_px=int((mask>127).sum()),head_point=head_point,
                mask='data:image/png;base64,'+base64.b64encode(png(rgba)).decode('ascii')))
        result['previous']=dict(measurement_mode=config.get('measurement_mode','physical'),worms=worms,roi=[x,y,x1,y1],scale_um_per_px=config['scale_um_per_px'],
            calibration=record.get('calibration'),head_detection=config.get('head_detection','twardis'),
            centerline_method=config.get('centerline_method', CURRENT_METHOD))
        return result

    def save(self, body):
        if self.done.is_set(): raise ValueError('本次标注已保存')
        current_profile = body.get('analysis_profile')=='current' or self.settings.get('analysis_profile')=='current'
        if current_profile:
            method=self.settings['centerline_method'] if hasattr(self,'correction_source') else CURRENT_METHOD
            head_mode=CURRENT_HEAD_DETECTION
        else:
            method=analysis_method(body.get('centerline_method',self.settings.get('centerline_method',CURRENT_METHOD)))
            head_mode=body.get('head_detection',self.settings.get('head_detection','twardis'))
        if head_mode not in ('twardis','manual_only','taper_seed','manual_seed'):raise ValueError('头尾检测选项无效')
        h, w = self.image.shape[:2]
        roi = body.get('roi') or [0, 0, w, h]
        if len(roi) != 4 or any(type(v) is not int for v in roi): raise ValueError('区域坐标格式错误')
        x, y, x1, y1 = roi
        if not (0 <= x < x1 <= w and 0 <= y < y1 <= h): raise ValueError('分析区域超出图像')
        scale = body.get('scale_um_per_px')
        from measurements.measurement_units import validate_scale
        unit_config = dict(measurement_mode=body.get('measurement_mode','physical'),scale_um_per_px=scale)
        validate_scale(unit_config)
        scale = unit_config['scale_um_per_px']
        calibration = body.get('calibration')
        if unit_config['measurement_mode'] == 'pixels': calibration = None
        if calibration:
            points = np.asarray(calibration['points'], float)
            length = float(calibration['length_um'])
            if points.shape != (2, 2) or not np.isfinite(points).all() or not math.isfinite(length) or length <= 0:
                raise ValueError('标尺数据无效')
            if np.any(points < 0) or np.any(points >= [w, h]): raise ValueError('标尺点超出图像')
            distance = float(np.linalg.norm(points[1] - points[0]))
            if distance < 1: raise ValueError('两个标尺点距离太近')
            scale = length / distance
        entries = body.get('worms', [])
        if not entries: raise ValueError('请至少标注一条虫')
        if method=='body_sequence':
            if len(entries)!=1:raise ValueError('当前方法每次分析一条线虫')
            if self.model_size!='large':raise ValueError('当前方法使用SAM2 Large')
        masks = {}; ids = set(); heads={}
        for entry in entries:
            worm = entry.get('id')
            if type(worm) is not int or not 1 <= worm <= 10000 or worm in ids:
                raise ValueError('线虫编号必须唯一且为正整数')
            ids.add(worm)
            prefix = 'data:image/png;base64,'
            data = entry.get('mask', '')
            if not data.startswith(prefix): raise ValueError('掩膜必须为 PNG')
            mask = cv2.imdecode(np.frombuffer(base64.b64decode(data[len(prefix):], validate=True), np.uint8), cv2.IMREAD_UNCHANGED)
            if mask is None or mask.shape[:2] != (h, w): raise ValueError('掩膜尺寸与标注图不匹配')
            if mask.ndim == 3:
                binary = mask[:, :, 0] > 127
                if mask.shape[2] == 4: binary &= mask[:, :, 3] > 0
            else: binary = mask > 127
            inside = binary[y:y1, x:x1]
            if int(inside.sum()) < 20: raise ValueError(f'线虫 {worm} 未画掩膜，或面积过小')
            if inside.sum() != binary.sum(): raise ValueError(f'线虫 {worm} 有像素位于分析区域外')
            masks[worm] = inside
            point=entry.get('head_point')
            if point is not None:
                if (not isinstance(point,list) or len(point)!=2 or
                    any(type(v) not in (int,float) or not math.isfinite(v) for v in point) or
                    not (x<=point[0]<x1 and y<=point[1]<y1)):
                    raise ValueError(f'线虫 {worm} 的头部点必须位于分析区域内')
                yy,xx=np.nonzero(binary)
                if np.min((xx-point[0])**2+(yy-point[1])**2)>12**2:
                    raise ValueError(f'线虫 {worm} 的头部点离当前虫体太远，请重新点选头端')
                heads[str(worm)]=dict(frame=self.frame,point=[point[0]-x,point[1]-y])
        absolute = [x + self.crop[0], y + self.crop[1], x1 + self.crop[0], y1 + self.crop[1]]
        if hasattr(self, 'correction_source'):
            from mask_correction import validate_edit
            validate_edit(self, absolute, masks)
        has_run = (self.output.parent/'pipeline-state.json').exists() or (self.output.parent/'sam2-tracking').exists()
        old_record = json.loads(self.path.read_text(encoding='utf-8')) if self.path.is_file() else {}
        old_config = old_record.get('config', {})
        old_paths = {w['id']:Path(w['mask_path']) for g in old_config.get('groups',[]) for w in g['worms'] if 'mask_path' in w}
        same_masks = set(old_paths) == set(masks)
        if same_masks:
            for worm, path in old_paths.items():
                old_mask = cv2.imdecode(np.frombuffer(path.read_bytes(),np.uint8),cv2.IMREAD_GRAYSCALE) if path.is_file() else None
                if old_mask is None or not np.array_equal(old_mask > 127, masks[worm]): same_masks = False; break
        same_inputs = (same_masks and old_record.get('input') == self.identity
                       and old_config.get('crop_xyxy') == absolute and old_config.get('scale_um_per_px') == scale
                       and old_record.get('calibration') == calibration
                       and old_config.get('head_annotations',{}) == heads
                       and old_config.get('head_detection','twardis') == head_mode)
        previous_sample = None
        keep_original = has_run and same_inputs
        if has_run and not same_inputs:
            from runtime.run_storage import fresh_sample
            previous_sample = self.settings['sample']
            self.settings = dict(self.settings, sample=fresh_sample(previous_sample, ROOT/'outputs'))
            self.output = ROOT/'outputs'/self.settings['sample']/'manual-initialization'
            self.path = self.output/'annotation.json'
        self.output.mkdir(parents=True, exist_ok=True)
        worms = []
        for worm, mask in masks.items():
            path = old_paths[worm] if keep_original else self.output / f'worm-{worm}.png'
            if not keep_original: path.write_bytes(png(mask.astype(np.uint8) * 255))
            worms.append(dict(id=worm, mask_path=str(path.resolve())))
        config = dict(sample=self.settings['sample'], video=self.video, crop_xyxy=absolute,
                      measurement_mode=unit_config['measurement_mode'],
                      scale_um_per_px=scale, scale_source='User-entered scale' if not calibration else (
                          'Click-detected scale bar' if calibration.get('method') == 'auto' else 'User-selected two-point scale bar'),
                      groups=[dict(name='manual', crop_xyxy=absolute, anchor_frame=self.frame, worms=worms)],
                      review_intervals=self.settings.get('review_intervals', []),
                      centerline_method=method,sam2_model=self.model_size,
                      head_annotations=heads,head_detection=head_mode)
        if "chamber_id" in self.settings:
            config["chamber_id"] = self.settings["chamber_id"]
        for key in ('identity_confirmations', 'source_invalid_frames'):
            if key in self.settings and (key=='source_invalid_frames' or not has_run or same_inputs):
                config[key] = self.settings[key]
        if previous_sample or self.settings.get('previous_sample'):
            config['previous_sample'] = previous_sample or self.settings['previous_sample']
        if hasattr(self, 'correction_source'):
            from mask_correction import merge_edit
            merge_edit(self, config, masks)
        record = dict(input=self.identity, config=config, calibration=calibration,
                      provenance=body.get('provenance', 'Masks drawn and confirmed in the local browser'),
                      overlap_px=int((np.stack(list(masks.values())).sum(axis=0) > 1).sum()))
        if not keep_original: self.path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        (self.output/'last-selection.json').write_text(json.dumps({'centerline_method':method}), encoding='utf-8')
        from measurements.annotation_metrics import measure_mask, VERSION as ANNOTATION_METRICS_VERSION
        frame_metrics = dict(version=ANNOTATION_METRICS_VERSION, scope='annotation_frame', frame=self.frame,
                             worms=[measure_mask(mask, frame=self.frame, worm_id=worm,
                                                 measurement_mode=unit_config['measurement_mode'],
                                                 scale_um_per_px=scale, origin=(x, y))
                                    for worm, mask in masks.items()])
        metrics_path = self.output/'annotation-frame-metrics.json'
        metrics_path.write_text(json.dumps(frame_metrics, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        self.result = config
        self.done.set()
        return dict(saved=True, path=str(self.path), worm_count=len(worms), overlap_px=record['overlap_px'],
                    centerline_method=method,method_label=display_method(method)['label'],frame_metrics_path=str(metrics_path))


def new_video_annotation(video, defaults):
    from runtime.run_storage import fresh_sample
    settings = dict(sample=fresh_sample(video.stem, ROOT/'outputs'), video=str(video.resolve()),
                    crop_xyxy=None, scale_um_per_px=None, measurement_mode='pixels', groups=[], review_intervals=[],
                    centerline_method=CURRENT_METHOD,
                    sam2_model='large', analysis_profile='current', head_detection=CURRENT_HEAD_DETECTION)
    return Annotation(settings)


def can_edit_annotation_mode(annotation, progress):
    return bool(progress and progress.batch and not hasattr(annotation, 'correction_source')
                and (getattr(annotation, 'start_on_submit', False) or hasattr(annotation, 'sequence')))


def change_annotation_mode(annotation, progress, chamber_mode):
    if type(chamber_mode) is not bool:
        raise ValueError('请选择标注方式')
    with progress.lock:
        if (not can_edit_annotation_mode(annotation, progress) or progress.server.annotation is not annotation
                or annotation.done.is_set() or progress.data['status'] != 'preparing' or progress.batch.running):
            raise ValueError('当前标注已结束或无法更改标注方式')
        if chamber_mode != getattr(annotation, 'chamber_mode', False):
            if chamber_mode and not hasattr(annotation, 'sequence'):
                annotation.sequence = ([Path(annotation.video)], [])
            elif not chamber_mode and getattr(annotation, 'start_on_submit', False):
                del annotation.sequence
            annotation.chamber_mode = chamber_mode
            annotation.release_predictor()
            # A still-open page for the old mode must not write to the other draft.
            annotation.token = secrets.token_urlsafe(24)


def save_chambers(progress, annotation, body):
    """Save one single-worm run per ROI and publish the video to the queue as a unit."""
    from contextlib import ExitStack
    from shutil import rmtree
    from runtime.run_storage import fresh_sample
    from workspace_sessions import save_draft

    chambers = body.get('chambers')
    if not isinstance(chambers, list) or not chambers:
        raise ValueError('请先框选培养槽并确认虫体')
    ids = set()
    for chamber in chambers:
        if not isinstance(chamber, dict):
            raise ValueError('培养槽格式错误')
        number = chamber.get('id')
        if type(number) is not int or not 1 <= number <= 10000 or number in ids:
            raise ValueError('槽号须为 1 至 10000 之间的唯一整数')
        if not isinstance(chamber.get('mask'), str):
            raise ValueError(f'槽 {number} 缺少虫体掩膜')
        if not chamber.get('roi'):
            raise ValueError(f'槽 {number} 缺少分析区域')
        ids.add(number)

    paths, finished_videos = annotation.sequence
    outputs = ROOT / 'outputs'
    outputs.mkdir(parents=True, exist_ok=True)
    configs = []
    with progress.lock, ExitStack() as rollback:
        if annotation.done.is_set() or progress.batch.running:
            raise ValueError('当前标注已提交或队列正在处理')
        for chamber in chambers:
            number = chamber['id']
            sample = fresh_sample(f'{Path(annotation.video).stem[:35]}-槽{number}', outputs)
            run = outputs / sample
            run.mkdir()
            rollback.callback(rmtree, run)
            child = copy.copy(annotation)
            child.settings = dict(annotation.settings, sample=sample, chamber_id=number)
            child.done = threading.Event()
            child.output = run / 'manual-initialization'
            child.path = child.output / 'annotation.json'
            child.save(dict(body, roi=chamber['roi'], worms=[dict(id=1, mask=chamber.get('mask'),
                           head_point=chamber.get('head_point'))]))
            configs.append(child.result)
        samples = getattr(annotation, 'prior_chamber_samples', finished_videos) + [c['sample'] for c in configs]
        last_video = len(finished_videos) + 1 == len(paths)
        progress.batch.add_many(configs, start_samples=samples if last_video else None)
        rollback.pop_all()
        annotation.result = configs[-1]
        annotation.done.set()
        finished_videos.append(configs[-1]['sample'])
        progress.config = copy.deepcopy(annotation.result)
        progress.data.update(status='ready', detail='培养槽标注已保存')
        annotation.release_predictor()
        try:
            save_draft(annotation, dict(submitted=True))
        except OSError:
            pass  # The committed queue and masks are already durable.
        if last_video:
            return dict(progress_url='/progress')
        video = paths[len(finished_videos)]
        try:
            candidate = new_video_annotation(video, annotation.settings)
            candidate.chamber_mode = True
            candidate.sequence = (paths, finished_videos)
            candidate.prior_chamber_samples = samples
            progress.queue_video(candidate, deferred=True)
        except (OSError, ValueError) as error:
            progress.fail(f'培养槽标注已保存，无法打开下一项 {video.name}：{error}')
            return dict(progress_url='/progress')
        return dict(next_annotation=True)


def advance_sequence(progress, annotation):
    paths, saved = annotation.sequence
    saved.append(annotation.result['sample'])
    samples = getattr(annotation, 'prior_chamber_samples', None)
    if samples is not None:
        samples = samples + [annotation.result['sample']]
    if len(saved) == len(paths):
        progress.batch.start(samples=samples if samples is not None else saved)
        return dict(progress_url='/progress')
    video = paths[len(saved)]
    try:
        candidate = new_video_annotation(video, annotation.settings)
        candidate.sequence = (paths, saved)
        if samples is not None:
            candidate.prior_chamber_samples = samples
        progress.queue_video(candidate, deferred=True)
    except (OSError, ValueError) as error:
        progress.fail(f'前 {len(saved)} 个标注已保存。无法打开下一项 {video.name}：{error}')
        return dict(progress_url='/progress')
    return dict(next_annotation=True)


def create_server(annotation=None, progress=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def respond(self, code, data, content_type='application/json; charset=utf-8'):
            if not isinstance(data, bytes): data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(data)

        def respond_before_body(self, code, data):
            """Send the response first, then briefly drain a body already in flight."""
            self.close_connection = True
            self.respond(code, data)
            try:
                remaining = int(self.headers.get('Content-Length', '0'))
                if remaining <= 0:
                    return
                # Closing with unread TCP data can discard the response on Windows.
                self.connection.shutdown(socket.SHUT_WR)
                deadline = time.monotonic() + 0.5
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (OSError, ValueError):
                # A disconnected peer or invalid length needs no further reads.
                return

        def send_artifact(self, library=False):
            from urllib.parse import parse_qs, urlsplit, quote
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if library:
                from workspace_library import media
                path = media(ROOT, query.get('id', [''])[0])
            else:
                key = query.get('key', [''])[0]
                sample = query.get('run', [None])[0]
                if 'group' in query:
                    from workspace_sessions import load_config, saved_videos
                    try:
                        with progress.lock:
                            items = progress.batch.group_items(query['group'][0] or None) if progress.batch else []
                        if not any(i['sample'] == sample and i['status'] == 'complete' for i in items):
                            self.respond(404, {}); return
                        path = saved_videos(*load_config(ROOT, sample))[key][1]
                    except (KeyError, OSError, TypeError, ValueError):
                        self.respond(404, {}); return
                else:
                    with progress.lock:
                        if sample is not None and sample != progress.data['sample']:
                            self.respond(409, dict(error='结果会话已改变')); return
                        path = progress.artifacts.get(key)
            if path is None or not path.is_file(): self.respond(404, {}); return
            total = path.stat().st_size
            start, end, code = 0, total-1, 200
            requested = self.headers.get('Range')
            if requested:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                if match and any(match.groups()):
                    left, right = match.groups()
                    start = int(left) if left else max(0, total-int(right))
                    end = min(int(right), total-1) if left and right else total-1
                    code = 206
                if not match or not any(match.groups()) or start > end or start >= total:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{total}')
                    self.send_header('Content-Length', '0'); self.end_headers(); return
            inline = query.get('inline') == ['1']
            self.send_response(code)
            from workspace_library import content_type
            self.send_header('Content-Type', content_type(path) if library else 'video/mp4' if inline and path.suffix.lower()=='.mp4' else 'application/octet-stream')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(end-start+1))
            self.send_header('Accept-Ranges', 'bytes')
            if code == 206: self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
            self.send_header('Content-Disposition', ('inline' if inline else 'attachment') + "; filename*=UTF-8''" + quote(path.name))
            self.end_headers()
            if self.command == 'HEAD': return
            try:
                with path.open('rb') as handle:
                    handle.seek(start)
                    remaining = end-start+1
                    while remaining:
                        chunk = handle.read(min(1024*1024, remaining))
                        if not chunk: break
                        self.wfile.write(chunk); remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

        def do_HEAD(self):
            if progress and self.path.startswith('/api/library-media?'): self.send_artifact(library=True)
            elif progress and self.path.startswith('/artifact?'): self.send_artifact()
            else: self.send_response(404); self.end_headers()

        def do_GET(self):
            from urllib.parse import parse_qs, urlsplit
            annotation = self.server.annotation
            if progress and self.path.startswith('/api/channel/'):
                route = urlsplit(self.path)
                query = parse_qs(route.query)
                file = query.get('file', [''])[0]
                run = query.get('run', [None])[0]
                try:
                    counts = self.server.channel_counts()
                    if route.path == '/api/channel/state':
                        view = counts.state(file, run)
                        if view['task'] and view['task']['status'] != 'complete':
                            view['task'].pop('events', None)
                        self.respond(200, view)
                    elif route.path == '/api/channel/comparison':
                        self.respond(200, counts.comparison())
                    elif route.path == '/api/channel/setup':
                        self.respond(200, counts.setup(file, int(query.get('channels', [32])[0]), int(query.get('frame', [0])[0]), run))
                    elif route.path == '/api/channel/frames':
                        self.respond(200, counts.frames(file, run, int(query.get('start', ['1'])[0]), int(query.get('limit', ['6'])[0])))
                    elif route.path == '/api/channel/event':
                        self.respond(200, counts.event(file, run, int(query.get('id', [0])[0])))
                    elif route.path == '/api/channel/export':
                        channel = int(query['channel'][0]) if 'channel' in query else None
                        self.respond(200, counts.export(file, run, channel), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                    else: self.respond(404, {})
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                except (ValueError, OSError, RuntimeError, cv2.error) as exc:
                    self.respond(400, dict(error=str(exc)))
                return
            if self.path == '/api/layout':
                from workspace_layout import read
                token = progress.token if progress else annotation.token if annotation else None
                if not token: self.respond(404, {}); return
                try: self.respond(200, dict(read(ROOT), token=token))
                except (ValueError, OSError) as exc: self.respond(500, dict(error=str(exc), token=token))
            elif progress and self.path == '/api/library':
                from workspace_library import listing
                try: self.respond(200, listing(ROOT))
                except (ValueError, OSError) as exc: self.respond(500, dict(error='文件工作区读取失败：'+str(exc)))
            elif progress and self.path.startswith('/api/library-media?'): self.send_artifact(library=True)
            elif self.path == '/wormstudio-icon.png':
                self.respond(200, (ROOT/'web/wormstudio-icon.png').read_bytes(), 'image/png')
            elif self.path in ('/studio', '/studio.css', '/studio-clean.css', '/channel-counts.css', '/channel-counts.js', '/channel-review.js', '/studio.js', '/studio-results.js', '/result-navigation.js', '/result-viewport.js', '/result-player.js', '/result-events.js', '/workspace-controls.js', '/library-tree.js', '/studio-evaluation.js', '/studio-evaluation.css', '/studio-embed.css', '/draft-autosave.js', '/workspace-usability.css', '/workspace-links.js'):
                name = 'studio.html' if self.path == '/studio' else self.path[1:]
                kind = 'text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript'
                self.respond(200, (ROOT/'web'/name).read_bytes(), kind + '; charset=utf-8')
            elif self.path.startswith('/api/evaluation/'):
                from evaluation_http import get
                get(self)
            elif progress and self.path == '/api/projects':
                from workspace_sessions import catalog
                self.respond(200,catalog(ROOT))
            elif progress and (self.path.startswith('/api/identity-links?') or self.path.startswith('/api/identity-frame?')):
                from urllib.parse import parse_qs,urlsplit
                from workspace_sessions import identity_links,identity_frame
                query=parse_qs(urlsplit(self.path).query);sample=query.get('run',[''])[0]
                if not progress.config or progress.config['sample']!=sample:
                    self.respond(409,dict(error='结果会话已变化'));return
                try:
                    if self.path.startswith('/api/identity-frame?'):
                        self.respond(200,identity_frame(ROOT,sample,int(query.get('worm',['0'])[0]),int(query.get('frame',['-1'])[0])),'image/jpeg')
                    else:self.respond(200,identity_links(ROOT,sample))
                except (ValueError,OSError,KeyError,TypeError) as exc:self.respond(400,dict(error=str(exc)))
            elif progress and self.path.startswith('/api/head-anchors?'):
                from urllib.parse import parse_qs,urlsplit
                from head_tail.head_tail import normalize_anchors
                sample=parse_qs(urlsplit(self.path).query).get('run',[''])[0]
                if not progress.config or progress.config['sample']!=sample:
                    self.respond(409,dict(error='结果会话已变化'));return
                self.respond(200,dict(sample=sample,anchors={w:normalize_anchors(a) for w,a in progress.config.get('head_annotations',{}).items()}))
            elif annotation and self.path == '/api/draft':
                from workspace_sessions import get_draft
                self.respond(200,get_draft(annotation))
            elif progress and self.path == '/api/progress':
                self.respond(200, progress.snapshot())
            elif progress and urlsplit(self.path).path in ('/api/chamber-summaries', '/api/batch-results', '/api/saved-timeline'):
                from workspace_sessions import chamber_summaries, saved_timeline
                route = urlsplit(self.path)
                query = parse_qs(route.query)
                try:
                    with progress.lock:
                        items = progress.batch.group_items(query.get('group', [None])[0]) if progress.batch else []
                    if route.path == '/api/saved-timeline':
                        sample = query.get('run', [''])[0]
                        with progress.lock:
                            active = progress.data['status'] == 'complete' and progress.data['sample'] == sample
                        if not active and not any(i['sample'] == sample and i['status'] == 'complete' for i in items):
                            raise ValueError('所选任务没有已完成的结果')
                        self.respond(200, saved_timeline(ROOT, sample, trend_only=query.get('detail') != ['1']))
                    elif route.path == '/api/batch-results':
                        self.respond(200, dict(items=items, reports=chamber_summaries(ROOT, items)))
                    else:
                        self.respond(200, chamber_summaries(ROOT, items))
                except (KeyError, OSError, TypeError, ValueError) as exc:
                    self.respond(400, dict(error=str(exc)))
            elif progress and self.path.startswith('/api/live-preview?'):
                from urllib.parse import parse_qs, urlsplit
                try:
                    query = parse_qs(urlsplit(self.path).query)
                    run = query.get('run', [''])[0]
                    sequence = int(query['sequence'][0]) if 'sequence' in query else None
                    data = progress.preview(run, int(query.get('after', ['0'])[0]), sequence,
                                            focus=query.get('view') == ['focus'])
                    self.respond(200, data, 'image/jpeg' if sequence is not None else 'application/json; charset=utf-8')
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    self.respond(409, dict(error=str(exc)))
            elif progress and self.path.startswith('/api/frame-metrics?'):
                from urllib.parse import parse_qs, urlsplit
                try:
                    sample = parse_qs(urlsplit(self.path).query).get('run', [''])[0]
                    self.respond(200, progress.frame_results(sample))
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    self.respond(409, dict(error=str(exc)))
            elif progress and self.path in ('/review', '/review.js'):
                name = 'review_result.html' if self.path == '/review' else 'review_result.js'
                self.respond(200, (ROOT/'web'/name).read_bytes(), 'text/html; charset=utf-8' if name.endswith('.html') else 'text/javascript; charset=utf-8')
            elif progress and self.path.startswith('/api/frame-preview?'):
                from urllib.parse import parse_qs, urlsplit
                cap = None
                try:
                    if progress.snapshot()['status'] not in ('complete', 'failed', 'cancelled') or not progress.config:
                        self.respond(409, dict(error='当前没有可重新标注的已结束任务')); return
                    frame = int(parse_qs(urlsplit(self.path).query).get('frame', ['-1'])[0])
                    cap = cv2.VideoCapture(str(progress.config['video']))
                    if not 0 <= frame < int(cap.get(cv2.CAP_PROP_FRAME_COUNT)):
                        raise ValueError('标注帧超出视频范围')
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
                    ok, preview = cap.read()
                    if not ok: raise ValueError('无法读取此帧')
                    if preview.shape[1] > 1280:
                        preview = cv2.resize(preview, (1280, round(preview.shape[0]*1280/preview.shape[1])))
                    self.respond(200, png(preview), 'image/png')
                except (ValueError, KeyError):
                    self.respond(400, dict(error='无法读取指定帧，请重新选择'))
                finally:
                    if cap is not None: cap.release()
            elif progress and self.path in ('/select-video', '/select-video.js'):
                name = 'select_video.html' if self.path == '/select-video' else 'select_video.js'
                self.respond(200, (ROOT/'web'/name).read_bytes(), 'text/html; charset=utf-8' if name.endswith('.html') else 'text/javascript; charset=utf-8')
            elif progress and (self.path == '/progress' or (self.path == '/' and (annotation is None or annotation.done.is_set()))):
                self.respond(200,(ROOT/'web'/'processing.html').read_bytes(),'text/html; charset=utf-8')
            elif progress and self.path in ('/processing.css','/processing.js','/live-preview.js','/phenotype-results.js','/frame-metrics.js'):
                self.respond(200,(ROOT/'web'/self.path[1:]).read_bytes(), 'text/css; charset=utf-8' if self.path.endswith('.css') else 'text/javascript; charset=utf-8')
            elif progress and self.path.startswith('/artifact?'):
                self.send_artifact()
            elif annotation and self.path == '/': self.respond(200, (ROOT/'web'/'manual_worm_annotation.html').read_bytes(), 'text/html; charset=utf-8')
            elif self.path == '/annotation.css': self.respond(200,(ROOT/'web'/'manual_worm_annotation.css').read_bytes(),'text/css; charset=utf-8')
            elif self.path == '/annotation.js': self.respond(200,(ROOT/'web'/'manual_worm_annotation.js').read_bytes(),'text/javascript; charset=utf-8')
            elif self.path == '/annotation-readiness.js': self.respond(200,(ROOT/'web'/'annotation-readiness.js').read_bytes(),'text/javascript; charset=utf-8')
            elif self.path == '/analysis-presentation.js': self.respond(200,(ROOT/'web'/'analysis-presentation.js').read_bytes(),'text/javascript; charset=utf-8')
            elif self.path == '/annotation-coordinates.js': self.respond(200,(ROOT/'web'/'annotation-coordinates.js').read_bytes(),'text/javascript; charset=utf-8')
            elif annotation and self.path == '/image.png': self.respond(200, png(annotation.image), 'image/png')
            elif annotation and self.path == '/state':
                paths, saved = getattr(annotation, 'sequence', ([], []))
                self.respond(200, dict(annotation.initial_state(), progress_enabled=progress is not None,
                                       can_edit_mode=can_edit_annotation_mode(annotation, progress),
                                       batch_deferred=getattr(annotation, 'batch_deferred', False),
                                       chamber_mode=bool(getattr(annotation, "chamber_mode", False)),
                                       sequence_index=len(saved)+1, sequence_total=len(paths)))
            elif annotation and urlsplit(self.path).path == '/api/algorithms':
                # Run outside /state and outside the single HTTP request thread.
                # Dependency checks must not freeze mask editing.
                if (parse_qs(urlsplit(self.path).query).get('retry')==['1']
                        and not getattr(self.server,'optional_probe',{}).get('checking')):
                    if hasattr(self.server,'optional_probe'):del self.server.optional_probe
                if not hasattr(self.server, 'optional_probe'):
                    self.server.optional_probe = dict(available=False, checking=True, detail='正在检查分析组件…')
                    server = self.server
                    def probe():
                        try:
                            from pose.body_sequence_solver import availability
                            server.optional_probe = dict(availability(), checking=False)
                        except Exception as exc:
                            server.optional_probe = dict(available=False, checking=False, detail=f'分析组件检查失败：{exc}')
                    threading.Thread(target=probe, daemon=True).start()
                self.respond(200, dict(algorithm=self.server.optional_probe))
            else: self.respond(404, {})

        def do_POST(self):
            annotation = self.server.annotation
            try:
                if progress and self.path == '/api/channel/task':
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond_before_body(403, dict(error='Invalid processing session')); return
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 8192: raise ValueError('计数请求无效')
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict): raise ValueError('计数请求无效')
                    self.respond(200, self.server.channel_counts().action(body.get('file'), body.get('action'), body))
                    return
                if progress and self.path == '/api/batch':
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond_before_body(403, dict(error='Invalid processing session')); return
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 8192: raise ValueError('队列请求无效')
                    body = json.loads(self.rfile.read(size))
                    with progress.lock:
                        if progress.batch is None: raise ValueError('请使用 Worm Studio 启动批处理')
                        action = body.get('action')
                        if action == 'stop':
                            if not progress.batch.running: raise ValueError('批处理尚未启动')
                            progress.cancel_requested.set()
                            if progress.batch.active is None: progress.batch.running = False
                        else:
                            if progress.data['status'] not in ('ready','complete','failed','cancelled') or progress.next_ready.is_set():
                                raise ValueError('请先保存当前标注或等待当前处理结束')
                            if action == 'start': progress.batch.start()
                            elif action == 'remove': progress.batch.remove(body.get('id'))
                            elif action == 'retry': progress.batch.retry(body.get('id'))
                            elif action == 'retry-one': progress.batch.retry(body.get('id'), start=True, group=body.get('group'))
                            else: raise ValueError('未知队列操作')
                    self.respond(200, progress.snapshot()); return
                if self.path == '/api/layout':
                    token = progress.token if progress else annotation.token if annotation else None
                    if not token or self.headers.get('X-Annotation-Token') != token:
                        self.respond_before_body(403, dict(error='Invalid layout session')); return
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 32768: raise ValueError('布局请求过大或为空')
                    from workspace_layout import update
                    self.respond(200, update(ROOT, json.loads(self.rfile.read(size)))); return
                if progress and self.path == '/api/edit-preview-mask':
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond_before_body(403, dict(error='Invalid processing session')); return
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 8192: raise ValueError('掩膜修正请求无效')
                    body = json.loads(self.rfile.read(size))
                    from mask_correction import request_edit
                    result = request_edit(progress, ROOT, body.get('run'), body.get('sequence'))
                    self.respond(202 if result.get('waiting') else 200, result); return
                if progress and (self.path.startswith('/api/library-upload?') or self.path in ('/api/library-selection','/api/library-tree','/api/library-link')):
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond_before_body(403,dict(error='Invalid processing session')); return
                    from workspace_library import upload, select, organize
                    from urllib.parse import parse_qs, urlsplit
                    size = int(self.headers.get('Content-Length', '0'))
                    if self.path == '/api/library-tree':
                        if not 0 < size <= 32768: raise ValueError('文件树请求过大或为空')
                        result=organize(ROOT,json.loads(self.rfile.read(size)))
                    elif self.path == '/api/library-link':
                        if not 0 < size <= 8192: raise ValueError('视频路径无效')
                        from workspace_library import register, VIDEO
                        path = Path(json.loads(self.rfile.read(size))['path']).expanduser()
                        if not path.is_absolute() or path.suffix.lower() not in VIDEO:
                            raise ValueError('请选择视频文件的完整路径')
                        result = register(ROOT, path)
                    elif self.path == '/api/library-selection':
                        if not 0 < size <= 8192: raise ValueError('文件选择请求无效')
                        result = select(ROOT, json.loads(self.rfile.read(size)).get('id'))
                    else:
                        q = parse_qs(urlsplit(self.path).query)
                        result = upload(ROOT, self.rfile, size, q.get('name',[''])[0], q.get('relativePath',[''])[0], q.get('signature',[''])[0],q.get('parent',[None])[0])
                    self.respond(200,result); return
                if progress and self.path == '/api/remove-history':
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond_before_body(403, dict(error='Invalid processing session')); return
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 8192: raise ValueError('任务请求无效')
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict): raise ValueError('任务请求无效')
                    from workspace_sessions import remove_history_entry
                    remove_history_entry(ROOT, body.get('sample'))
                    self.respond(200, dict(removed=True)); return
                if progress and self.path in ('/api/open-run','/api/resume-run','/api/refresh-video','/api/confirm-head','/api/auto-head','/api/remove-head','/api/review-identity','/api/remove-identity-review','/api/cancel'):
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond_before_body(403,dict(error='Invalid processing session')); return
                    if self.path == '/api/cancel':
                        status = progress.snapshot()['status']
                        if status not in ('running','preparing'): raise ValueError('当前没有正在运行的任务')
                        progress.cancel_requested.set()
                        if annotation and status == 'preparing':
                            annotation.result = None
                            annotation.done.set()
                            progress.fail('已取消标注；保存过的草稿仍可恢复', cancelled=True)
                        self.respond_before_body(200,dict(cancelling=True));return
                    if progress.snapshot()['status'] not in ('ready','complete','failed','cancelled') or progress.next_ready.is_set():
                        raise ValueError('请先结束当前任务')
                    if progress.batch and progress.batch.running: raise ValueError('请先停止批处理')
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=8192: raise ValueError('任务请求无效')
                    body=json.loads(self.rfile.read(size))
                    from workspace_sessions import reopen,load_config
                    if self.path=='/api/open-run': reopen(progress,ROOT,body.get('sample'))
                    elif self.path=='/api/refresh-video':
                        sample=body.get('sample')
                        if (sample!=progress.data.get('sample') or not progress.data.get('history_reopened')
                                or not progress.data.get('legacy_video') or progress.data.get('read_only')):
                            raise ValueError('请先打开需要更新视频的历史结果')
                        if 'source-video' not in progress.artifacts or not progress.artifacts['source-video'].is_file():
                            raise ValueError('请先将原视频放回原来的位置')
                        progress.next_settings={'_refresh_video':sample}
                        progress.data.update(status='preparing',detail='正在准备重新导出标注视频')
                        progress.next_ready.set()
                    else:
                        if self.path in ('/api/confirm-head','/api/auto-head','/api/remove-head','/api/review-identity','/api/remove-identity-review'):
                            if not progress.config or body.get('sample')!=progress.config['sample']:
                                raise ValueError('当前结果已改变，请重新加载后复核')
                            from workspace_sessions import confirm_head
                            if self.path=='/api/auto-head':
                                from workspace_sessions import derive_geometry_run
                                _,settings=load_config(ROOT,body.get('sample'))
                                settings['head_detection']=body.get('head_mode','twardis')
                                if settings['head_detection'] not in ('twardis','taper_seed','manual_seed'):raise ValueError('头尾初始化选项无效')
                                settings=derive_geometry_run(ROOT,body.get('sample'),settings)
                            elif self.path=='/api/review-identity':
                                from workspace_sessions import review_identity
                                settings=review_identity(ROOT,body.get('sample'),body.get('link_id'),body.get('decision'),body.get('reviewer'),body.get('evidence'))
                            elif self.path=='/api/remove-identity-review':
                                from workspace_sessions import remove_identity_review
                                settings=remove_identity_review(ROOT,body.get('sample'),body.get('review_id'))
                            elif self.path=='/api/confirm-head':
                                settings=confirm_head(ROOT,body.get('sample'),body.get('worm_id'),body.get('frame'),body.get('endpoint'))
                            else:
                                from workspace_sessions import remove_head
                                settings=remove_head(ROOT,body.get('sample'),body.get('worm_id'),body.get('anchor_id'))
                        else: _,settings=load_config(ROOT,body.get('sample'))
                        progress.path=None
                        progress.preview_dir=None
                        progress.cancel_requested.clear()
                        progress.server.annotation=None
                        progress.next_settings=dict(settings,_resume_saved=True)
                        progress.data.update(status='preparing',sample=settings['sample'],detail='正在恢复保存的任务')
                        progress.next_ready.set()
                    self.respond(200,dict(ok=True));return
                if annotation and self.path in ('/api/annotation-frame','/api/draft','/api/annotation-mode'):
                    if self.headers.get('X-Annotation-Token')!=annotation.token:
                        self.respond_before_body(403,dict(error='Invalid annotation session'));return
                    if annotation.done.is_set(): raise ValueError('标注已保存')
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=64*1024*1024: raise ValueError('请求过大或为空')
                    body=json.loads(self.rfile.read(size))
                    if self.path=='/api/annotation-mode':
                        change_annotation_mode(annotation, progress, body.get('chamber_mode'))
                        self.respond(200, dict(ok=True));return
                    if self.path=='/api/draft':
                        from workspace_sessions import save_draft
                        self.respond(200,save_draft(annotation,body));return
                    if hasattr(annotation, 'correction_source'):
                        raise ValueError('当前正在修正指定帧的掩膜，请保存后再选择其他帧')
                    candidate=Annotation(annotation.settings,frame=body.get('frame'))
                    annotation.release_predictor()
                    # Keep the existing Event: collect_annotation is waiting on it.
                    for key in ('image','frame','identity','crop'):
                        setattr(annotation,key,getattr(candidate,key))
                    self.respond(200,dict(ok=True));return
                if self.path == '/api/evaluation/save':
                    from evaluation_http import save
                    save(self, progress)
                    return
                if progress and self.path == '/api/reannotate':
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond_before_body(403, dict(error='Invalid processing session')); return
                    if progress.snapshot()['status'] not in ('complete','failed','cancelled') or not progress.config:
                        self.respond_before_body(409, dict(error='当前没有可重新标注的已结束任务')); return
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 8192: raise ValueError('标注帧请求无效')
                    frame = json.loads(self.rfile.read(size)).get('frame')
                    if type(frame) is not int: raise ValueError('请选择整数帧号')
                    from runtime.run_storage import fresh_sample
                    settings = dict(progress.config, sample=fresh_sample(progress.config['sample'], ROOT/'outputs'),
                                    previous_sample=progress.config['sample'], crop_xyxy=None, groups=[], review_intervals=[])
                    candidate = Annotation(settings, frame=frame)
                    candidate.start_on_submit = progress.batch is not None
                    progress.queue_video(candidate)
                    self.respond(200, dict(annotation_url='/')); return
                if progress and (self.path == '/api/new-video' or self.path.startswith('/api/upload-video?')):
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond_before_body(403, dict(error='Invalid processing session')); return
                    if progress.snapshot()['status'] not in ('ready','complete','failed','cancelled'):
                        self.respond_before_body(409, dict(error='当前任务尚未结束')); return
                    from urllib.parse import parse_qs, urlsplit
                    uploaded = None
                    sequence = []
                    size = int(self.headers.get('Content-Length', '0'))
                    if self.path == '/api/new-video':
                        if not 0 < size <= 2*1024*1024: raise ValueError('视频路径请求过大，请分批拖入')
                        body = json.loads(self.rfile.read(size))
                        video = Path(str(body.get('video_path', '')).strip().strip('"')).expanduser()
                        if not video.is_absolute(): raise ValueError('请填写视频的完整路径')
                        if 'sequence' in body:
                            if not isinstance(body['sequence'], list): raise ValueError('视频顺序无效')
                            sequence = list(dict.fromkeys(Path(p) for p in body['sequence']))
                            if not sequence or sequence[0] != video: raise ValueError('视频顺序无效')
                            for path in sequence:
                                if not path.is_absolute() or not path.is_file(): raise ValueError(f'无法读取视频：{path}')
                    else:
                        filename = parse_qs(urlsplit(self.path).query).get('name', [''])[0]
                        filename = filename.replace('\\','/').rsplit('/',1)[-1]
                        filename = re.sub(r'[<>:"|?*\x00-\x1f]', '_', filename).strip('. ')
                        if filename.split('.')[0].lower() in {'con','prn','aux','nul',*[f'{p}{i}' for p in ('com','lpt') for i in range(1,10)]}:
                            filename = 'video-' + filename
                        if not filename or Path(filename).suffix.lower() not in ('.mp4','.avi','.mov','.mkv','.m4v','.mpg','.mpeg','.wmv'):
                            raise ValueError('请选择 MP4、AVI、MOV 等视频文件')
                        if not 0 < size <= 8*1024**3: raise ValueError('视频为空或超过 8 GB，请改用完整路径导入')
                        folder = ROOT/'input_videos'/secrets.token_hex(8)
                        folder.mkdir(parents=True)
                        uploaded = video = folder/filename
                        try:
                            with video.open('wb') as handle:
                                remaining = size
                                while remaining:
                                    chunk = self.rfile.read(min(1024*1024, remaining))
                                    if not chunk: raise ValueError('视频传输中断，请重新选择')
                                    handle.write(chunk); remaining -= len(chunk)
                        except Exception:
                            video.unlink(missing_ok=True); folder.rmdir(); raise
                    try:
                        defaults = (annotation.result or annotation.settings) if annotation else getattr(self.server, 'studio_defaults', {})
                        candidate = new_video_annotation(video, defaults)
                        if self.path == '/api/new-video' and body.get('chamber_mode') is True:
                            candidate.chamber_mode = True
                            sequence = sequence or [video]
                        if sequence: candidate.sequence = (sequence, [])
                    except Exception:
                        if uploaded:
                            uploaded.unlink(missing_ok=True); uploaded.parent.rmdir()
                        raise
                    from workspace_library import register
                    library_file = register(ROOT, video)
                    deferred = bool(sequence) or (self.path == '/api/new-video' and body.get('batch') is True)
                    if deferred and progress.batch is None: raise ValueError('请使用 Worm Studio 启动批处理')
                    candidate.start_on_submit = bool(self.path == '/api/new-video' and body.get('edit_setup') is True
                                                     and progress.batch and not deferred)
                    progress.queue_video(candidate, deferred=deferred)
                    self.respond(200, dict(annotation_url='/', video_name=video.name, file=library_file)); return
                if progress and self.path == '/close':
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond_before_body(403, dict(error='Invalid processing session')); return
                    if progress.batch and progress.batch.running:
                        self.respond_before_body(409, dict(error='请先停止批处理')); return
                    if progress.snapshot()['status'] not in ('ready','complete','failed','cancelled'):
                        self.respond_before_body(409, dict(error='处理仍在进行，请等待完成')); return
                    progress.closed.set()
                    self.respond_before_body(200, dict(closed=True))
                    return
                if annotation is None: self.respond_before_body(404, {}); return
                if self.path not in ('/save', '/predict', '/api/annotation-metrics'): self.respond_before_body(404, {}); return
                if self.headers.get('X-Annotation-Token') != annotation.token:
                    self.respond_before_body(403, dict(error='Invalid annotation session')); return
                size = int(self.headers.get('Content-Length', '0'))
                limit = 64 * 1024 * 1024 if self.path in ('/save', '/api/annotation-metrics') else 8192
                if not 0 < size <= limit: raise ValueError('标注请求过大或为空')
                body = json.loads(self.rfile.read(size))
                if self.path == '/save' and annotation.done.is_set():
                    raise ValueError('当前标注已提交')
                if self.path == '/save' and getattr(annotation, 'chamber_mode', False):
                    result = save_chambers(progress, annotation, body)
                    self.respond(200, result)
                    return
                from measurements.annotation_metrics import measure_annotation
                action = {'/predict': annotation.predict, '/save': annotation.save,
                          '/api/annotation-metrics': lambda request: measure_annotation(annotation, request)}
                result = action[self.path](body)
                if self.path == '/save':
                    from workspace_sessions import save_draft
                    save_draft(annotation, dict(submitted=True))
                if self.path == '/save' and progress:
                    if getattr(annotation, 'start_on_submit', False):
                        with progress.lock:
                            progress.config = copy.deepcopy(annotation.result)
                            progress.next_settings = dict(annotation.result, _resume_saved=True)
                            progress.next_ready.set()
                        annotation.release_predictor()
                        result['progress_url'] = '/progress'
                    elif getattr(annotation, 'batch_deferred', False):
                        with progress.lock:
                            try:
                                progress.batch.add(annotation.result)
                            except Exception:
                                annotation.done.clear()
                                raise
                            progress.config = copy.deepcopy(annotation.result)
                            progress.data.update(status='ready', detail='标注已加入队列，可继续标注下一个视频')
                        annotation.release_predictor()
                        if hasattr(annotation, 'sequence'):
                            with progress.lock:
                                result.update(advance_sequence(progress, annotation))
                        else:
                            result['queued'] = True
                    else:
                        result['progress_url'] = '/progress'
                self.respond(200, result)
            except (ValueError, KeyError, TypeError) as error:
                self.respond(400, dict(error=str(error)))
            except Exception as error:
                print(f'SAM2 标注服务出错：{error}', flush=True)
                self.respond(500, dict(error=f'SAM2 预测失败：{error}'))
    # Single-request server serializes saves and avoids model/state races.
    from http.server import HTTPServer
    class WorkspaceServer(HTTPServer):
        counts = None

        def channel_counts(self):
            if self.counts is None:
                from tracking.channel_counts import ChannelCounts
                self.counts = ChannelCounts(ROOT)
            return self.counts

        def server_close(self):
            if self.annotation is not None:
                self.annotation.release_predictor()
            if self.counts is not None:
                self.counts.close()
            super().server_close()

    server = WorkspaceServer(('127.0.0.1', 0), Handler)
    server.annotation = annotation
    return server


def collect_annotation(settings, frame=0, reuse=True, check_only=False, open_browser=True, review_saved=True, progress=None):
    existing = getattr(progress.server, 'annotation', None) if progress and progress.server else None
    annotation = existing if existing and not existing.done.is_set() and existing.settings == settings else Annotation(settings, frame)
    if reuse:
        previous = annotation.previous()
        if previous is not None and (check_only or not review_saved):
            print(f'使用已保存的人工标注：{annotation.path}', flush=True)
            return previous
    if check_only:
        print(f'视频和标注帧可读取。运行时将打开人工标注页面；当前尚未保存标注。', flush=True)
        return None
    reused_server = progress is not None and progress.server is not None
    server = progress.server if reused_server else create_server(annotation, progress)
    server.annotation = annotation
    server.timeout = .5
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'请在工作台完成标注并点击“保存并开始处理”：{url}', flush=True)
    if progress and not reused_server: progress.bind_server(server)
    close_window = None
    if open_browser and not reused_server:
        from platforms.shell import open_view
        def window_closed():
            if progress:
                progress.cancel_requested.set()
                progress.closed.set()
            current = getattr(server, 'annotation', None)
            if current:
                current.done.set()
            annotation.done.set()
        close_window = open_view(url, window_closed)
    try:
        while not annotation.done.is_set():
            if progress: annotation.done.wait(.2)
            else: server.handle_request()
    finally:
        if not progress:
            if close_window: close_window()
            server.server_close()
        annotation.release_predictor()
    return annotation.result
