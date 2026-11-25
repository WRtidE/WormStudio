"""Editable annotation state, persistence and queued video transitions."""
import base64
import copy
import gc
import json
import math
import multiprocessing
from pathlib import Path
import secrets
import threading

import cv2
import numpy as np

from centerline_methods import display_method, analysis_method, CURRENT_METHOD, CURRENT_HEAD_DETECTION
from compute_device import release_device_cache
from image_io import png
from input_validation import annotation_mask, valid_sample_name
from platforms.system import is_darwin
from resource_paths import PROJECT_ROOT
from sam2_models import input_contrast
from sam2_mask_predictor import MaskPredictor, annotation_worker


def preview_frame_at(capture, seconds, count, fps):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError('视频预览时间无效')
    if seconds == 0:
        return 0
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError('无法确定视频帧率，请从首帧开始标注')
    guess = min(count-1, math.floor(min(seconds, count/fps) * fps + 1e-7))
    target_ms = seconds * 1000 + .001

    def timestamp(index):
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        if not capture.grab():
            raise ValueError(f'无法定位源视频的第 {index} 帧')
        return capture.get(cv2.CAP_PROP_POS_MSEC)

    # Check the usual constant-rate position first. Some decoders expose no timestamps.
    start = timestamp(guess)
    if not math.isfinite(start) or start < 0 or (guess > 0 and start == 0):
        return guess
    if start <= target_ms:
        if guess == count-1:
            return guess
        following = timestamp(guess+1)
        if following > target_ms or following <= start:
            return guess
        low, high = guess+1, count
    else:
        low, high = 0, guess
    while low+1 < high:
        middle = (low+high)//2
        if timestamp(middle) <= target_ms:
            low = middle
        else:
            high = middle
    return low


class Annotation:
    def __init__(self, settings, frame=0, *, root=PROJECT_ROOT, preview_seconds=None):
        self.root = root
        self.settings = dict(settings)
        if 'centerline_method' in settings:
            self.settings['centerline_method'] = analysis_method(settings['centerline_method'])
        sample = settings['sample']
        if not valid_sample_name(sample):
            raise ValueError('请使用普通的 SAMPLE_NAME 文件夹名')
        video = Path(settings['video']).resolve()
        if not video.is_file(): raise ValueError(f'视频不存在：{video}')
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            cap.release(); raise ValueError('无法打开此视频，文件可能损坏或编码不受支持。请确认能正常播放，或转换为 H.264 MP4 后重试。')
        self.n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if self.n < 5:
            cap.release(); raise ValueError(f'视频只有 {self.n} 帧，分析至少需要 5 帧。请选择更长的视频。')
        fps = cap.get(cv2.CAP_PROP_FPS)
        try:
            if preview_seconds is not None:
                frame = preview_frame_at(cap, preview_seconds, self.n, fps)
            if type(frame) is not int or not 0 <= frame < self.n:
                raise ValueError(f'标注帧超出范围，请输入 0–{self.n - 1} 之间的整数。')
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
            ok, image = cap.read()
            self.frame_seconds = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
            if frame and self.frame_seconds == 0 and fps > 0:
                self.frame_seconds = frame/fps
        finally:
            cap.release()
        if not ok: raise ValueError(f'无法解码第 {frame} 帧，请尝试另一帧，或转换视频编码后重试。')
        h, w = image.shape[:2]
        self.crop = settings.get('crop_xyxy') or [0, 0, w, h]
        x, y, x1, y1 = self.crop
        if any(type(v) is not int for v in self.crop) or not (0 <= x < x1 <= w and 0 <= y < y1 <= h):
            raise ValueError('CROP_XYXY 超出原视频范围；可设为 None 使用完整画面')
        self.image = image[y:y1, x:x1]
        self.frame = frame
        self.video = str(video)
        self.output = self.root / 'outputs' / sample / 'manual-initialization'
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

    @classmethod
    def from_video(cls, video, *, root=PROJECT_ROOT, preview_seconds=None):
        from run_storage import fresh_sample
        settings = dict(sample=fresh_sample(video.stem, root/'outputs'), video=str(video.resolve()),
                        crop_xyxy=None, scale_um_per_px=None, measurement_mode='pixels', groups=[], review_intervals=[],
                        centerline_method=CURRENT_METHOD,
                        sam2_model='large', analysis_profile='current', head_detection=CURRENT_HEAD_DETECTION)
        return cls(settings, root=root, preview_seconds=preview_seconds)

    def predict(self, body, *, token=None):
        with self.predict_lock:
            if token is not None and token != self.token:
                raise ValueError('标注帧已切换，请重新点选')
            if self.done.is_set():
                raise ValueError('本次标注已保存')
            if not is_darwin():
                if self.local_predictor is None:
                    self.local_predictor = MaskPredictor(self.image, self.model_size, self.video, self.crop[:2])
                return self.local_predictor.predict(body)
            if self.worker is None:
                ctx = multiprocessing.get_context('spawn')
                parent, child = ctx.Pipe()
                worker = ctx.Process(target=annotation_worker, args=(child, self.image, self.model_size, self.video, self.crop[:2]), daemon=True)
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
            frame_count=self.n,frame_seconds=self.frame_seconds if math.isfinite(self.frame_seconds) and self.frame_seconds>=0 else None,
            sam2_model=self.model_size,sam2_input_contrast=self.settings.get('sam2_input_contrast','none'),
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
            from head_evidence import anchors_in_crop
            records=anchors_in_crop(config.get('head_annotations',{}).get(str(entry['id'])),config['crop_xyxy'],config['crop_xyxy'])
            heads=[a for a in records if a['frame']==self.frame]
            head_point=[heads[0]['point'][0]+x,heads[0]['point'][1]+y] if len(heads)==1 else None
            worms.append(dict(id=entry['id'],area_px=int((mask>127).sum()),head_point=head_point,
                head_points=[dict(frame=a['frame'], point=[a['point'][0]+x,a['point'][1]+y]) for a in records if a['frame']!=self.frame],
                mask='data:image/png;base64,'+base64.b64encode(png(rgba)).decode('ascii')))
        result['previous']=dict(measurement_mode=config.get('measurement_mode','physical'),worms=worms,roi=[x,y,x1,y1],scale_um_per_px=config['scale_um_per_px'],
            calibration=record.get('calibration'),head_detection=config.get('head_detection','twardis'),
            sam2_input_contrast=config.get('sam2_input_contrast','none'),
            centerline_method=config.get('centerline_method', CURRENT_METHOD))
        return result

    def save(self, body):
        if self.done.is_set(): raise ValueError('本次标注已保存')
        contrast = input_contrast(self.correction_source.get('sam2_input_contrast','none') if hasattr(self,'correction_source')
                                  else body.get('sam2_input_contrast', self.settings.get('sam2_input_contrast','none')))
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
        from measurement_units import validate_scale
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
            binary = annotation_mask(entry.get('mask'), (h, w))
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
            if entry.get('head_points'):
                from head_evidence import normalize_anchors
                anchors = [heads[str(worm)]] if point is not None else []
                seen_frames = {self.frame}
                for anchor in normalize_anchors(entry['head_points'], self.n):
                    frame = anchor['frame']
                    px, py = anchor['point']
                    if frame in seen_frames or not (x <= px < x1 and y <= py < y1):
                        raise ValueError(f'线虫 {worm} 的抽帧标记重复或超出分析区域')
                    seen_frames.add(frame)
                    anchors.append(dict(frame=frame, point=[px-x, py-y]))
                heads[str(worm)] = sorted(anchors, key=lambda a: a['frame'])
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
                       and old_config.get('sam2_input_contrast','none') == contrast
                       and old_config.get('head_annotations',{}) == heads
                       and old_config.get('head_detection','twardis') == head_mode)
        previous_sample = None
        keep_original = has_run and same_inputs
        if has_run and not same_inputs:
            from run_storage import fresh_sample
            previous_sample = self.settings['sample']
            self.settings = dict(self.settings, sample=fresh_sample(previous_sample, self.root/'outputs'))
            self.output = self.root/'outputs'/self.settings['sample']/'manual-initialization'
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
        if contrast != 'none':
            config['sam2_input_contrast'] = contrast
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
        from annotation_metrics import measure_mask, VERSION as ANNOTATION_METRICS_VERSION
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
            # A still-open page for the old mode must not write to the other draft.
            annotation.token = secrets.token_urlsafe(24)
            annotation.release_predictor()


def save_chambers(progress, annotation, body):
    """Save one single-worm run per ROI and publish the video to the queue as a unit."""
    from contextlib import ExitStack
    from shutil import rmtree
    from run_storage import fresh_sample
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
    outputs = annotation.root / 'outputs'
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
                           head_point=chamber.get('head_point'), head_points=chamber.get('head_points'))]))
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
            candidate = type(annotation).from_video(video, root=annotation.root)
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
        candidate = type(annotation).from_video(video, root=annotation.root)
        candidate.sequence = (paths, saved)
        if samples is not None:
            candidate.prior_chamber_samples = samples
        progress.queue_video(candidate, deferred=True)
    except (OSError, ValueError) as error:
        progress.fail(f'前 {len(saved)} 个标注已保存。无法打开下一项 {video.name}：{error}')
        return dict(progress_url='/progress')
    return dict(next_annotation=True)
