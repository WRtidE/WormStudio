import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import manual_worm_annotation as manual
from worm_pipeline import validate_config


class ManualAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='manual-worm-test-')
        self.root = Path(self.temp.name)
        self.patch = patch.object(manual, 'ROOT', self.root); self.patch.start()
        video = self.root / 'input.mp4'
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'mp4v'), 12, (128, 80))
        if not writer.isOpened(): raise RuntimeError('Test encoder unavailable')
        for _ in range(5): writer.write(np.zeros((80, 128, 3), np.uint8))
        writer.release()
        self.settings = dict(sample='manual-test', video=str(video), crop_xyxy=[10, 10, 110, 70],
                             scale_um_per_px=None, wormtracer={})
        self.mask = np.zeros((60, 100), np.uint8); self.mask[15:30, 20:65] = 255
        self.body = dict(worms=[dict(id=9, mask='data:image/png;base64,' + base64.b64encode(manual.png(self.mask)).decode())],
                         roi=[5, 5, 95, 55], scale_um_per_px=2,
                         calibration=dict(points=[[5, 5], [55, 5]], length_um=100))

    def tearDown(self):
        self.patch.stop(); self.temp.cleanup()

    def test_current_profile_overrides_legacy_choices_without_changing_masks(self):
        annotation=manual.Annotation(dict(self.settings,analysis_profile='current'))
        self.assertEqual(annotation.initial_state()['analysis_method'],'tapnext')
        with patch('tapnext_support.resources'),patch('tapnext_support.dependency_path'):
            annotation.save(dict(self.body,centerline_method='niu',head_detection='twardis'))
        self.assertEqual(annotation.result['centerline_method'],'tapnext')
        self.assertEqual(annotation.result['head_detection'],'taper_seed')
        saved=cv2.imread(annotation.result['groups'][0]['worms'][0]['mask_path'],0)
        np.testing.assert_array_equal(saved,self.mask[5:55,5:95])

    def test_current_profile_cannot_submit_multiple_worms_from_legacy_draft(self):
        annotation=manual.Annotation(self.settings)
        body=dict(self.body,analysis_profile='current',centerline_method='niu',
                  worms=[self.body['worms'][0],dict(self.body['worms'][0],id=10)])
        with self.assertRaisesRegex(ValueError,'仅支持单虫'):annotation.save(body)
        self.assertFalse(annotation.output.exists())

    def test_corrupt_video_is_not_reported_as_an_out_of_range_frame(self):
        bad=self.root/'broken.mp4';bad.write_bytes(b'not a video')
        with self.assertRaisesRegex(ValueError,'无法打开此视频'):
            manual.Annotation(dict(self.settings,video=str(bad)))

    def test_short_video_and_invalid_frame_have_separate_recovery_messages(self):
        with self.assertRaisesRegex(ValueError,'0–4'):
            manual.Annotation(self.settings,frame=9)
        short=self.root/'short.mp4'
        writer=cv2.VideoWriter(str(short),cv2.VideoWriter_fourcc(*'mp4v'),12,(128,80))
        writer.write(np.zeros((80,128,3),np.uint8));writer.release()
        with self.assertRaisesRegex(ValueError,'至少需要 5 帧'):
            manual.Annotation(dict(self.settings,video=str(short)))

    def test_saved_mask_geometry_scale_and_reuse(self):
        annotation = manual.Annotation(self.settings)
        result = annotation.save(self.body)
        self.assertEqual(result['worm_count'], 1)
        config = annotation.result
        self.assertEqual(config['crop_xyxy'], [15, 15, 105, 65])
        self.assertEqual(config['scale_um_per_px'], 2)
        saved = cv2.imdecode(np.frombuffer(Path(config['groups'][0]['worms'][0]['mask_path']).read_bytes(), np.uint8), 0)
        np.testing.assert_array_equal(saved, self.mask[5:55, 5:95])
        checked, info = validate_config(config)
        self.assertEqual(info['ids'], [9])
        self.assertEqual(manual.Annotation(self.settings).previous()['groups'], checked['groups'])

    def test_empty_mask_and_wrong_crop_are_rejected(self):
        annotation = manual.Annotation(self.settings)
        body = json.loads(json.dumps(self.body))
        body['roi'] = [30, 20, 90, 50]
        with self.assertRaises(ValueError): annotation.save(body)
        body['worms'][0]['mask'] = 'data:image/png;base64,' + base64.b64encode(manual.png(np.zeros_like(self.mask))).decode()
        with self.assertRaises(ValueError): annotation.save(body)

    def test_tapnext_multiworm_rejected_before_saving_masks(self):
        annotation = manual.Annotation(self.settings)
        body = json.loads(json.dumps(self.body))
        body['centerline_method'] = 'tapnext'
        body['worms'].append(dict(body['worms'][0], id=10))
        with self.assertRaisesRegex(ValueError, '仅支持单虫'):
            annotation.save(body)
        self.assertFalse(annotation.path.exists())
        self.assertFalse(annotation.done.is_set())

    def test_head_point_crop_coordinates_and_later_anchor_restore(self):
        annotation=manual.Annotation(self.settings,frame=3)
        body=json.loads(json.dumps(self.body));body['worms'][0]['head_point']=[21,22]
        annotation.save(body)
        self.assertEqual(annotation.result['head_annotations'],{'9':dict(frame=3,point=[16,17])})
        validate_config(annotation.result)
        restored=manual.Annotation(self.settings,frame=3).initial_state()
        self.assertEqual(restored['previous']['worms'][0]['head_point'],[21,22])
        invalid=json.loads(json.dumps(annotation.result));invalid['head_annotations']['9']['frame']=20
        with self.assertRaises(ValueError):validate_config(invalid)
        for point in ([200,22],[float('nan'),22],[90,48]):
            bad=json.loads(json.dumps(body));bad['worms'][0]['head_point']=point
            with self.assertRaises(ValueError):manual.Annotation(self.settings).save(bad)

    def test_resaving_changed_mask_preserves_original_annotation_and_seeds(self):
        annotation=manual.Annotation(self.settings);annotation.save(self.body)
        original_path=annotation.path
        original_seed=Path(annotation.result['groups'][0]['worms'][0]['mask_path'])
        old_record=original_path.read_bytes();old_seed=original_seed.read_bytes()
        (original_path.parent.parent/'pipeline-state.json').write_text('{}')
        body=json.loads(json.dumps(self.body));changed=self.mask.copy();changed[16,19]=255
        body['worms'][0]['mask']='data:image/png;base64,'+base64.b64encode(manual.png(changed)).decode()
        next_annotation=manual.Annotation(self.settings);next_annotation.save(body)
        self.assertNotEqual(next_annotation.result['sample'],self.settings['sample'])
        self.assertEqual(original_path.read_bytes(),old_record)
        self.assertEqual(original_seed.read_bytes(),old_seed)
        new_seed=Path(next_annotation.result['groups'][0]['worms'][0]['mask_path'])
        self.assertNotEqual(new_seed.read_bytes(),old_seed)
        self.assertEqual(next_annotation.result['previous_sample'],self.settings['sample'])

    def test_identical_resave_keeps_original_bytes_and_can_switch_method(self):
        annotation=manual.Annotation(self.settings);annotation.save(self.body)
        old_record=annotation.path.read_bytes()
        (annotation.output.parent/'pipeline-state.json').write_text('{}')
        body=dict(self.body,centerline_method='niu')
        next_annotation=manual.Annotation(self.settings);next_annotation.save(body)
        self.assertEqual(next_annotation.result['sample'],self.settings['sample'])
        self.assertEqual(next_annotation.result['centerline_method'],'niu')
        self.assertEqual(annotation.path.read_bytes(),old_record)
        self.assertEqual(manual.Annotation(self.settings).previous()['centerline_method'],'niu')
        self.assertEqual(manual.Annotation(self.settings).initial_state()['previous']['centerline_method'],'niu')

    def test_save_api_requires_session_and_returns_readable_configuration(self):
        annotation = manual.Annotation(self.settings)
        server = manual.create_server(annotation)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(url + '/state') as response: state = json.load(response)
            data = json.dumps(self.body).encode()
            request = urllib.request.Request(url + '/save', data, headers={'Content-Type': 'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            request.add_header('X-Annotation-Token', state['token'])
            with urllib.request.urlopen(request) as response: result = json.load(response)
            self.assertTrue(result['saved'])
            self.assertTrue(annotation.done.is_set())
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def test_sam2_prediction_maps_roi_mask_to_full_annotation(self):
        annotation = manual.Annotation(self.settings)
        predictor = MagicMock()
        candidates = np.zeros((3, 30, 40), dtype=bool)
        candidates[1, 5:20, 8:25] = True
        predictor.predict.return_value = (candidates, np.array([0.2, 0.9, 0.3]), None)
        annotation.predictor = predictor
        with patch('torch.cuda.is_available', return_value=True):
            result = annotation.predict(dict(roi=[20, 10, 60, 40],
                points=[dict(x=30, y=20, label=1)], box=[22, 12, 58, 38]))
        self.assertEqual(result['candidate'], 1)
        self.assertEqual(result['area_px'], 15 * 17)
        encoded = base64.b64decode(result['mask'].split(',', 1)[1])
        rgba = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_UNCHANGED)
        self.assertEqual(rgba.shape, (60, 100, 4))
        self.assertEqual(rgba[15, 28, 3], 255)
        self.assertEqual(rgba[15, 27, 3], 0)
        self.assertEqual(rgba[9, 28, 3], 0)
        self.assertEqual(predictor.set_image.call_args.args[0].shape, (30, 40, 3))
        np.testing.assert_array_equal(predictor.predict.call_args.kwargs['point_coords'], [[10, 10]])
        with self.assertRaisesRegex(ValueError, '分析区域外'):
            annotation.predict(dict(roi=[20, 10, 60, 40], points=[dict(x=2, y=20, label=1)]))

    def test_predict_api_requires_token(self):
        annotation = manual.Annotation(self.settings)
        server = manual.create_server(annotation)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        url = f'http://127.0.0.1:{server.server_port}/predict'
        request = urllib.request.Request(url, json.dumps(dict(points=[dict(x=30, y=20, label=1)])).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            request.add_header('X-Annotation-Token', annotation.token)
            with patch.object(annotation, 'predict', return_value=dict(mask='image', score=0.9, area_px=250, candidate=0)):
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)['area_px'], 250)
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def test_click_refinement_is_isolated_per_worm_and_revision(self):
        annotation=manual.Annotation(self.settings)
        predictor=MagicMock()
        masks=np.ones((3,60,100),dtype=bool)
        logits=np.stack([np.full((256,256),i,dtype=np.float32) for i in range(3)])
        predictor.predict.return_value=(masks,np.array([.3,.9,.5]),logits)
        annotation.predictor=predictor
        p1=[dict(x=30,y=20,label=1)]
        first=annotation.predict(dict(worm_id=9,points=p1))
        self.assertEqual(len(first['candidates']),3)
        self.assertIsNone(predictor.predict.call_args.kwargs['mask_input'])
        second=annotation.predict(dict(worm_id=9,points=p1+[dict(x=35,y=22,label=1)],
            prediction_token=first['prediction_token'],previous_candidate=2))
        np.testing.assert_array_equal(predictor.predict.call_args.kwargs['mask_input'],logits[2:3])
        self.assertFalse(predictor.predict.call_args.kwargs['multimask_output'])
        self.assertTrue(second['refined'])
        annotation.predict(dict(worm_id=10,points=p1,prediction_token=second['prediction_token']))
        self.assertIsNone(predictor.predict.call_args.kwargs['mask_input'])
        annotation.predict(dict(worm_id=9,points=p1,prediction_token=first['prediction_token']))
        self.assertIsNone(predictor.predict.call_args.kwargs['mask_input'])

    def test_all_methods_save_and_restore_with_full_mask_geometry(self):
        for method in ('wormtracer','niu','hybrid'):
            annotation=manual.Annotation(self.settings)
            annotation.save(dict(self.body,centerline_method=method))
            fresh=manual.Annotation(self.settings)
            self.assertEqual(fresh.previous()['centerline_method'],method)
            state=fresh.initial_state()['previous']
            self.assertEqual(state['centerline_method'],method)
            self.assertEqual(state['roi'],[5,5,95,55])
            self.assertEqual(state['calibration'],self.body['calibration'])
            rgba=cv2.imdecode(np.frombuffer(base64.b64decode(state['worms'][0]['mask'].split(',')[1]),np.uint8),-1)
            np.testing.assert_array_equal(rgba[:,:,3]>0,self.mask>0)
        annotation=manual.Annotation(self.settings)
        with self.assertRaises(ValueError):annotation.save(dict(self.body,centerline_method='fake'))


if __name__ == '__main__':
    unittest.main()
