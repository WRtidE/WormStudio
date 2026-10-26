"""Local browser annotation with optional SAM2 assisted first-frame masks."""
import base64
import gc
import json
import math
import re
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler
from centerline_methods import METHODS, validate_method
from sam2_models import model_spec
from pathlib import Path

import cv2
import numpy as np

from resource_paths import PROJECT_ROOT as ROOT, RESOURCE_ROOT, sam2_checkpoint


def png(image):
    ok, data = cv2.imencode('.png', image)
    if not ok: raise ValueError('PNG encoding failed')
    return data.tobytes()


class Annotation:
    def __init__(self, settings, frame=0):
        self.settings = settings
        sample = settings['sample']
        reserved={'all','con','prn','aux','nul'}|{f'{p}{i}' for p in ('com','lpt') for i in range(1,10)}
        if not re.fullmatch(r'[\w][\w.-]{0,79}', sample) or sample.endswith('.') or sample.split('.')[0].lower() in reserved:
            raise ValueError('请使用普通的 SAMPLE_NAME 文件夹名')
        video = Path(settings['video']).resolve()
        if not video.is_file(): raise ValueError(f'视频不存在：{video}')
        cap = cv2.VideoCapture(str(video))
        self.n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if type(frame) is not int or not 0 <= frame < self.n or self.n < 5:
            cap.release(); raise ValueError('标注帧超出范围，或视频不足 5 帧')
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, image = cap.read(); cap.release()
        if not ok: raise ValueError('无法读取标注帧')
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
        self.predictor = None
        self.predictor_roi = None
        self.prediction_cache = {}
        self.model_size = settings.get('sam2_model','large')
        if self.model_size not in ('large','tiny'): raise ValueError('SAM2_MODEL 必须为 large 或 tiny')

    def predict(self, body):
        """Predict one first-frame worm mask from user points/box on the CUDA GPU."""
        if self.done.is_set(): raise ValueError('本次标注已保存')
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
        if not torch.cuda.is_available(): raise RuntimeError('SAM2 首帧预测需要 CUDA GPU')
        if self.predictor is None:
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor
            spec=model_spec(self.model_size)
            checkpoint = sam2_checkpoint(spec['checkpoint'])
            if not checkpoint.is_file(): raise RuntimeError(f'找不到 SAM2 权重：{checkpoint}')
            model = build_sam2(spec['config'], str(checkpoint), device='cuda')
            self.predictor = SAM2ImagePredictor(model)
        if self.predictor_roi != roi:
            with torch.inference_mode(), torch.autocast('cuda',dtype=torch.bfloat16):
                self.predictor.set_image(cv2.cvtColor(self.image[y0:y1, x0:x1], cv2.COLOR_BGR2RGB))
            self.predictor_roi = roi.copy()
        previous=self.prediction_cache.get(worm_id)
        mask_input=None
        if previous and body.get('prediction_token')==previous['token']:
            old=previous['points']
            idx=body.get('previous_candidate',previous['winner'])
            if type(idx) is not int or not 0<=idx<len(previous['candidates']):
                raise ValueError('候选编号无效')
            if (previous['roi']==roi and previous['box']==box and len(points)>len(old)
                    and points[:len(old)]==old and previous['logits'] is not None):
                mask_input=previous['logits'][idx:idx+1]
        with torch.inference_mode(), torch.autocast('cuda',dtype=torch.bfloat16):
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
                                            logits=logits,candidates=options,winner=winner)
        return dict(**options[winner], candidate=winner,candidates=options,
                    prediction_token=token,refined=mask_input is not None)

    def release_predictor(self):
        self.predictor = None
        self.predictor_roi = None
        self.prediction_cache.clear()
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available(): torch.cuda.empty_cache()
        except ImportError:
            pass

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
            config['centerline_method'] = validate_method(json.loads(selection.read_text(encoding='utf-8'))['centerline_method'])
        # Apply current fit settings; the pipeline will validate cached results.
        config['wormtracer'] = self.settings.get('wormtracer', {})
        config['head_detection'] = self.settings.get('head_detection',config.get('head_detection','twardis'))
        config['review_intervals'] = self.settings.get('review_intervals', [])
        for key in ('identity_confirmations', 'source_invalid_frames'):
            if key in self.settings:config[key]=self.settings[key]
        if self.settings.get('scale_um_per_px') is not None:
            config['scale_um_per_px'] = self.settings['scale_um_per_px']
            config['scale_source'] = self.settings.get('scale_source', 'User override')
        return config

    def initial_state(self):
        """Restore saved, reviewed masks without restoring stale model prompts."""
        result=dict(tapnext=dict(available=False,detail='正在检查可选算法…'),sample=self.settings['sample'],frame=self.frame,
            width=self.image.shape[1],height=self.image.shape[0],token=self.token,
            measurement_mode=self.settings.get('measurement_mode','physical'),scale_um_per_px=self.settings.get('scale_um_per_px'),video_name=Path(self.video).name,
            frame_count=self.n,sam2_model=self.model_size,
            head_detection=self.settings.get('head_detection','twardis'),
            centerline_method=self.settings.get('centerline_method','wormtracer'),previous=None)
        if not self.path.exists(): return result
        record=json.loads(self.path.read_text(encoding='utf-8'))
        if record.get('input')!=self.identity: return result
        config=record['config']
        selection=self.output/'last-selection.json'
        if selection.is_file():
            config['centerline_method']=validate_method(json.loads(selection.read_text(encoding='utf-8'))['centerline_method'])
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
            from head_tail import normalize_anchors
            heads=[a for a in normalize_anchors(config.get('head_annotations',{}).get(str(entry['id']))) if a['frame']==self.frame]
            head_point=[heads[0]['point'][0]+x,heads[0]['point'][1]+y] if len(heads)==1 else None
            worms.append(dict(id=entry['id'],area_px=int((mask>127).sum()),head_point=head_point,
                mask='data:image/png;base64,'+base64.b64encode(png(rgba)).decode('ascii')))
        result['previous']=dict(measurement_mode=config.get('measurement_mode','physical'),worms=worms,roi=[x,y,x1,y1],scale_um_per_px=config['scale_um_per_px'],
            calibration=record.get('calibration'),head_detection=config.get('head_detection','twardis'),
            centerline_method=validate_method(config.get('centerline_method','wormtracer')))
        return result

    def save(self, body):
        if self.done.is_set(): raise ValueError('本次标注已保存')
        method=validate_method(body.get('centerline_method',self.settings.get('centerline_method','wormtracer')))
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
        if method == 'tapnext':
            from tapnext_support import validate_single_worm, resources, dependency_path
            validate_single_worm(len(entries), self.model_size)
            resources()
            dependency_path()
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
            from run_storage import fresh_sample
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
                      scale_um_per_px=scale, scale_source='User-entered scale' if not calibration else 'User-selected two-point scale bar',
                      groups=[dict(name='manual', crop_xyxy=absolute, anchor_frame=self.frame, worms=worms)],
                      review_intervals=self.settings.get('review_intervals', []), wormtracer=self.settings.get('wormtracer', {}),
                      centerline_method=method,sam2_model=self.model_size,
                      head_annotations=heads,head_detection=head_mode)
        for key in ('identity_confirmations', 'source_invalid_frames'):
            if key in self.settings and (key=='source_invalid_frames' or not has_run or same_inputs):
                config[key] = self.settings[key]
        if previous_sample or self.settings.get('previous_sample'):
            config['previous_sample'] = previous_sample or self.settings['previous_sample']
        record = dict(input=self.identity, config=config, calibration=calibration,
                      provenance=body.get('provenance', 'Masks drawn and confirmed in the local browser'),
                      overlap_px=int((np.stack(list(masks.values())).sum(axis=0) > 1).sum()))
        if not keep_original: self.path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        (self.output/'last-selection.json').write_text(json.dumps({'centerline_method':method}), encoding='utf-8')
        self.result = config
        self.done.set()
        return dict(saved=True, path=str(self.path), worm_count=len(worms), overlap_px=record['overlap_px'],
                    centerline_method=method,method_label=METHODS[method]['label'])


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

        def send_artifact(self):
            from urllib.parse import parse_qs, urlsplit, quote
            query = parse_qs(urlsplit(self.path).query)
            path = progress.artifacts.get(query.get('key', [''])[0])
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
            self.send_header('Content-Type', 'video/mp4' if inline and path.suffix.lower()=='.mp4' else 'application/octet-stream')
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
            if progress and self.path.startswith('/artifact?'): self.send_artifact()
            else: self.send_response(404); self.end_headers()

        def do_GET(self):
            annotation = self.server.annotation
            if self.path in ('/studio', '/studio.css', '/studio.js', '/studio-results.js', '/result-viewport.js', '/result-player.js', '/workspace-controls.js', '/studio-evaluation.js', '/studio-evaluation.css', '/studio-embed.css'):
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
                from head_tail import normalize_anchors
                sample=parse_qs(urlsplit(self.path).query).get('run',[''])[0]
                if not progress.config or progress.config['sample']!=sample:
                    self.respond(409,dict(error='结果会话已变化'));return
                self.respond(200,dict(sample=sample,anchors={w:normalize_anchors(a) for w,a in progress.config.get('head_annotations',{}).items()}))
            elif annotation and self.path == '/api/draft':
                from workspace_sessions import get_draft
                self.respond(200,get_draft(annotation))
            elif progress and self.path == '/api/progress':
                self.respond(200, progress.snapshot())
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
            elif progress and self.path in ('/processing.css','/processing.js','/phenotype-results.js','/frame-metrics.js'):
                self.respond(200,(ROOT/'web'/self.path[1:]).read_bytes(), 'text/css; charset=utf-8' if self.path.endswith('.css') else 'text/javascript; charset=utf-8')
            elif progress and self.path.startswith('/artifact?'):
                self.send_artifact()
            elif annotation and self.path == '/': self.respond(200, (ROOT/'web'/'manual_worm_annotation.html').read_bytes(), 'text/html; charset=utf-8')
            elif self.path == '/annotation.css': self.respond(200,(ROOT/'web'/'manual_worm_annotation.css').read_bytes(),'text/css; charset=utf-8')
            elif self.path == '/annotation.js': self.respond(200,(ROOT/'web'/'manual_worm_annotation.js').read_bytes(),'text/javascript; charset=utf-8')
            elif self.path == '/annotation-readiness.js': self.respond(200,(ROOT/'web'/'annotation-readiness.js').read_bytes(),'text/javascript; charset=utf-8')
            elif self.path == '/annotation-coordinates.js': self.respond(200,(ROOT/'web'/'annotation-coordinates.js').read_bytes(),'text/javascript; charset=utf-8')
            elif annotation and self.path == '/image.png': self.respond(200, png(annotation.image), 'image/png')
            elif annotation and self.path == '/state': self.respond(200,dict(annotation.initial_state(),progress_enabled=progress is not None))
            elif annotation and self.path == '/api/algorithms':
                # Run outside /state and outside the single HTTP request thread.
                # Slow filesystem/hash checks must not freeze masks or other methods.
                if not hasattr(self.server, 'optional_probe'):
                    self.server.optional_probe = dict(available=False, checking=True, detail='正在检查 TAPNext++ 依赖与权重…')
                    server = self.server
                    def probe():
                        try:
                            from tapnext_support import availability
                            server.optional_probe = dict(availability(), checking=False)
                        except Exception as exc:
                            server.optional_probe = dict(available=False, checking=False, detail=f'可选方法检查失败：{exc}')
                    threading.Thread(target=probe, daemon=True).start()
                self.respond(200, dict(tapnext=self.server.optional_probe))
            else: self.respond(404, {})

        def do_POST(self):
            annotation = self.server.annotation
            try:
                if progress and self.path in ('/api/open-run','/api/resume-run','/api/confirm-head','/api/auto-head','/api/remove-head','/api/review-identity','/api/remove-identity-review','/api/cancel'):
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond(403,dict(error='Invalid processing session')); return
                    if self.path == '/api/cancel':
                        status = progress.snapshot()['status']
                        if status not in ('running','preparing'): raise ValueError('当前没有正在运行的任务')
                        progress.cancel_requested.set()
                        if annotation and status == 'preparing':
                            annotation.result = None
                            annotation.done.set()
                            progress.fail('已取消标注；保存过的草稿仍可恢复', cancelled=True)
                        self.respond(200,dict(cancelling=True));return
                    if progress.snapshot()['status'] not in ('ready','complete','failed','cancelled') or progress.next_ready.is_set():
                        raise ValueError('请先结束当前任务')
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=8192: raise ValueError('任务请求无效')
                    body=json.loads(self.rfile.read(size))
                    from workspace_sessions import reopen,load_config
                    if self.path=='/api/open-run': reopen(progress,ROOT,body.get('sample'))
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
                        progress.cancel_requested.clear()
                        progress.server.annotation=None
                        progress.next_settings=dict(settings,_resume_saved=True)
                        progress.data.update(status='preparing',sample=settings['sample'],detail='正在恢复保存的任务')
                        progress.next_ready.set()
                    self.respond(200,dict(ok=True));return
                if annotation and self.path in ('/api/annotation-frame','/api/draft'):
                    if self.headers.get('X-Annotation-Token')!=annotation.token:
                        self.respond(403,dict(error='Invalid annotation session'));return
                    if annotation.done.is_set(): raise ValueError('标注已保存')
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=64*1024*1024: raise ValueError('请求过大或为空')
                    body=json.loads(self.rfile.read(size))
                    if self.path=='/api/draft':
                        from workspace_sessions import save_draft
                        self.respond(200,save_draft(annotation,body));return
                    candidate=Annotation(annotation.settings,frame=body.get('frame'))
                    annotation.release_predictor()
                    # Keep the existing Event: collect_annotation is waiting on it.
                    for key in ('image','frame','identity','crop'):
                        setattr(annotation,key,getattr(candidate,key))
                    annotation.prediction_cache={}
                    self.respond(200,dict(ok=True));return
                if self.path == '/api/evaluation/save':
                    from evaluation_http import save
                    save(self, progress)
                    return
                if progress and self.path == '/api/reannotate':
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond(403, dict(error='Invalid processing session')); return
                    if progress.snapshot()['status'] not in ('complete','failed','cancelled') or not progress.config:
                        self.respond(409, dict(error='当前没有可重新标注的已结束任务')); return
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 8192: raise ValueError('标注帧请求无效')
                    frame = json.loads(self.rfile.read(size)).get('frame')
                    if type(frame) is not int: raise ValueError('请选择整数帧号')
                    from run_storage import fresh_sample
                    settings = dict(progress.config, sample=fresh_sample(progress.config['sample'], ROOT/'outputs'),
                                    previous_sample=progress.config['sample'], crop_xyxy=None, groups=[], review_intervals=[])
                    candidate = Annotation(settings, frame=frame)
                    progress.queue_video(candidate)
                    self.respond(200, dict(annotation_url='/')); return
                if progress and (self.path == '/api/new-video' or self.path.startswith('/api/upload-video?')):
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond(403, dict(error='Invalid processing session')); return
                    if progress.snapshot()['status'] not in ('ready','complete','failed','cancelled'):
                        self.respond(409, dict(error='当前任务尚未结束')); return
                    from run_storage import fresh_sample
                    from urllib.parse import parse_qs, urlsplit
                    uploaded = None
                    size = int(self.headers.get('Content-Length', '0'))
                    if self.path == '/api/new-video':
                        if not 0 < size <= 8192: raise ValueError('视频路径请求无效')
                        body = json.loads(self.rfile.read(size))
                        video = Path(str(body.get('video_path', '')).strip().strip('"')).expanduser()
                        if not video.is_absolute(): raise ValueError('请填写视频的完整路径')
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
                        settings = dict(sample=fresh_sample(video.stem, ROOT/'outputs'), video=str(video.resolve()),
                            crop_xyxy=None, scale_um_per_px=None, measurement_mode='pixels', groups=[], review_intervals=[],
                            wormtracer=defaults.get('wormtracer', {}),
                            centerline_method=defaults.get('centerline_method', 'topology'), sam2_model=defaults.get('sam2_model', 'large'),
                            head_detection=defaults.get('head_detection','taper_seed'))
                        candidate = Annotation(settings)
                    except Exception:
                        if uploaded:
                            uploaded.unlink(missing_ok=True); uploaded.parent.rmdir()
                        raise
                    progress.queue_video(candidate)
                    self.respond(200, dict(annotation_url='/', video_name=video.name)); return
                if progress and self.path == '/close':
                    if self.headers.get('X-Annotation-Token') != progress.token:
                        self.respond(403, dict(error='Invalid processing session')); return
                    if progress.snapshot()['status'] not in ('ready','complete','failed','cancelled'):
                        self.respond(409, dict(error='处理仍在进行，请等待完成')); return
                    self.respond(200, dict(closed=True)); progress.closed.set(); return
                if annotation is None: self.respond(404, {}); return
                if self.path not in ('/save', '/predict'): self.respond(404, {}); return
                if self.headers.get('X-Annotation-Token') != annotation.token:
                    self.respond(403, dict(error='Invalid annotation session')); return
                size = int(self.headers.get('Content-Length', '0'))
                limit = 64 * 1024 * 1024 if self.path == '/save' else 8192
                if not 0 < size <= limit: raise ValueError('标注请求过大或为空')
                body = json.loads(self.rfile.read(size))
                action = {'/predict': annotation.predict, '/save': annotation.save}
                result = action[self.path](body)
                if self.path == '/save' and progress: result['progress_url'] = '/progress'
                self.respond(200, result)
            except (ValueError, KeyError, TypeError) as error:
                self.respond(400, dict(error=str(error)))
            except Exception as error:
                print(f'SAM2 标注服务出错：{error}', flush=True)
                self.respond(500, dict(error=f'SAM2 预测失败：{error}'))
    # Single-request server serializes saves and avoids model/state races.
    from http.server import HTTPServer
    server = HTTPServer(('127.0.0.1', 0), Handler)
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
    print(f'请在外部浏览器完成标注并点击“保存并开始处理”：{url}', flush=True)
    if progress and not reused_server: progress.bind_server(server)
    if open_browser and not reused_server: webbrowser.open(url, new=2)
    try:
        while not annotation.done.is_set():
            if progress: annotation.done.wait(.2)
            else: server.handle_request()
    finally:
        if not progress: server.server_close()
        annotation.release_predictor()
    return annotation.result
