"""SAM2 annotation inference and its isolated model worker."""
import base64
import math
import secrets

import cv2
import numpy as np

from image_io import png
from sam2_models import WORM21, checkpoint_path, input_contrast, model_spec


class MaskPredictor:
    def __init__(self, image, model_size, video=None, origin=(0, 0)):
        self.image = image
        self.model_size = model_size
        self.video = video
        self.origin = origin
        self.predictor = None
        self.predictor_view = None
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
        from compute_device import selected_device, inference_context
        device = selected_device()
        if self.predictor is None:
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor
            spec=model_spec(self.model_size)
            checkpoint = checkpoint_path(spec, verify=True)
            if not checkpoint.is_file(): raise RuntimeError(f'找不到 SAM2 权重：{checkpoint}')
            model = build_sam2(spec['config'], str(checkpoint), device=device,
                               apply_postprocessing=self.model_size != WORM21)
            self.predictor = SAM2ImagePredictor(model)
        contrast = input_contrast(body.get('sam2_input_contrast', 'none'))
        view = (tuple(roi), contrast)
        if self.predictor_view != view:
            from sam2_frames import sam2_input_transform
            image = self.image[y0:y1, x0:x1]
            if contrast == 'static_background':
                ox, oy = self.origin
                cap = cv2.VideoCapture(self.video)
                try:
                    transform = sam2_input_transform(contrast, cap, [ox+x0, oy+y0, ox+x1, oy+y1])
                finally:
                    cap.release()
                image = transform(image)
            elif contrast == 'clahe':
                image = sam2_input_transform(contrast)(image)
            with torch.inference_mode(), inference_context(device):
                self.predictor.set_image(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            self.predictor_view = view
        previous=self.prediction_cache.get(worm_id)
        mask_input=None
        if previous and body.get('prediction_token')==previous['token']:
            old=previous['points']
            idx=body.get('previous_candidate',previous['winner'])
            if type(idx) is not int or not 0<=idx<previous['candidate_count']:
                raise ValueError('候选编号无效')
            if (previous['view']==view and previous['box']==box and len(points)>len(old)
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
            if contrast == 'edge_refine':
                from sam2_frames import refine_mask_edges
                candidate = refine_mask_edges(self.image[y0:y1, x0:x1], candidate.astype(bool))
            mask = np.zeros((h, w), np.uint8)
            mask[y0:y1,x0:x1]=candidate.astype(np.uint8)*255
            rgba = np.full((h,w,4),255,np.uint8);rgba[:,:,3]=mask
            options.append(dict(index=index,score=float(scores[index]),area_px=int(np.count_nonzero(mask)),
                mask='data:image/png;base64,'+base64.b64encode(png(rgba)).decode('ascii')))
        if options[winner]['area_px']<20:
            raise ValueError('未找到足够大的虫体，请追加虫体点或框住虫体')
        self.prediction_cache[worm_id]=dict(points=points,box=box,view=view,token=token,
                                            logits=logits,candidate_count=len(options),winner=winner)
        return dict(**options[winner], candidate=winner,candidates=options,
                    prediction_token=token,refined=mask_input is not None)


def annotation_worker(connection, image, model_size, video, origin):
    # The child owns the model and Metal allocations for one annotation frame.
    with connection:
        predictor = MaskPredictor(image, model_size, video, origin)
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
