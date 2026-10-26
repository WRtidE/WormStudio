"""Configuration and missing-data regressions for the generic entry point."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from analyze_phenotypes import finite_stat, json_safe
from review_configured_video import selected_frames
from worm_colors import worm_color
from worm_pipeline import validate_config, complete, compatible_identity, compatible_tracking


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='worm-pipeline-test-')
        cls.root = Path(cls.temp.name)
        video = cls.root / 'short.mp4'
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'mp4v'), 12, (128, 80))
        if not writer.isOpened(): raise RuntimeError('Test video encoder unavailable')
        for _ in range(12): writer.write(np.zeros((80, 128, 3), np.uint8))
        writer.release()
        cls.config = dict(sample='short-single', video=str(video), crop_xyxy=[0, 0, 127, 79],
                          scale_um_per_px=1.0, groups=[dict(name='whole', crop_xyxy=[0, 0, 127, 79],
                          anchor_frame=0, worms=[dict(id=7, points=[[30, 30]], labels=[1], box=[10, 10, 70, 60])])])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_short_single_with_arbitrary_id_and_odd_crop(self):
        c, info = validate_config(self.config)
        self.assertEqual(info['ids'], [7])
        self.assertEqual(info['frames'], 12)
        self.assertEqual(c['crop_xyxy'], [0, 0, 127, 79])
        self.assertEqual(c['sam2_model'],'large')
        self.assertEqual(selected_frames(5), [0, 1, 2, 3, 4])
        self.assertEqual(selected_frames(12)[-1], 11)
        self.assertEqual(len(worm_color(7)), 3)

    def test_duplicate_ids_rejected(self):
        c = copy.deepcopy(self.config)
        c['groups'][0]['worms'].append(copy.deepcopy(c['groups'][0]['worms'][0]))
        with self.assertRaises(ValueError): validate_config(c)

    def test_wrong_frame_and_local_coordinate_rejected(self):
        for field, value in [('anchor_frame', 175), ('crop_xyxy', [0, 0, 200, 80])]:
            c = copy.deepcopy(self.config); c['groups'][0][field] = value
            with self.assertRaises(ValueError): validate_config(c)
        c = copy.deepcopy(self.config); c['groups'][0]['worms'][0]['points'] = [[200, 10]]
        with self.assertRaises(ValueError): validate_config(c)

    def test_unsafe_output_and_invalid_scale_rejected(self):
        for field, value in [('sample', '../outside'), ('sample', 'CON'), ('scale_um_per_px', float('nan'))]:
            c = copy.deepcopy(self.config); c[field] = value
            with self.assertRaises(ValueError): validate_config(c)

    def test_review_interval_must_match_objects_and_frames(self):
        c = copy.deepcopy(self.config)
        c['review_intervals'] = [dict(worm_ids=[1], frames=[0, 3])]
        with self.assertRaises(ValueError): validate_config(c)
        c['review_intervals'] = [dict(worm_ids=[7], frames=[0, 12])]
        with self.assertRaises(ValueError): validate_config(c)

    def test_missing_posture_is_json_null_not_zero(self):
        value = json_safe(dict(length=finite_stat([]), volume=finite_stat([np.nan]), speed=finite_stat([1, 3])))
        self.assertEqual(value, dict(length=None, volume=None, speed=2.0))
        self.assertEqual(json.loads(json.dumps(value, allow_nan=False)), value)

    def test_bad_cached_frame_count_is_not_reused(self):
        c, info = validate_config(self.config)
        path = self.root / 'centerlines.npy'; np.save(path, np.zeros((11, 101, 2)))
        meta = self.root / 'run.json'; meta.write_text('{}')
        stage = dict(name='fit-worm-7', phase='fit', outputs=[str(path), str(meta)])
        self.assertFalse(complete(stage, c, info))

    def test_tiny_or_unknown_tracking_cannot_be_reused_as_large(self):
        c,info=validate_config(self.config)
        base=self.root/'model-cache';folder=base/'sam2-tracking';folder.mkdir(parents=True)
        path=folder/'summary.json'
        summary=dict(video=c['video'],frames=info['frames'],fps=info['fps'],crop_xyxy=c['crop_xyxy'],
                     worm_ids=info['ids'],initialization=c['groups'],scale_um_per_px=c['scale_um_per_px'])
        stage=dict(phase='tracking',outputs=[str(path)])
        for model in ('SAM2 Hiera Tiny',None):
            path.write_text(json.dumps(dict(summary,model=model)))
            self.assertFalse(complete(stage,c,info))
            with self.assertRaises(ValueError):compatible_tracking(c,info,base)
        path.write_text(json.dumps(dict(summary,model='SAM2 Hiera Large',sam2_model='large',
                                       model_config='configs/sam2/sam2_hiera_l.yaml',checkpoint='sam2_hiera_large.pt')))
        self.assertTrue(complete(stage,c,info))
        compatible_tracking(c,info,base)

    def test_changed_fit_parameters_are_not_silently_reused(self):
        c, info = validate_config(self.config)
        path = self.root / 'changed-centerlines.npy'; np.save(path, np.zeros((12, 101, 2)))
        meta = self.root / 'changed-run.json'
        meta.write_text(json.dumps(dict(params=dict(rescale=.1, continuity_loss_weight=100,
                                                   smoothness_loss_weight=5000, length_loss_weight=5))))
        stage = dict(name='fit-worm-7', phase='fit', outputs=[str(path), str(meta)])
        with self.assertRaises(ValueError): complete(stage, c, info)

    def test_method_and_calibration_description_change_can_resume(self):
        c, _ = validate_config(self.config)
        old = dict(config=c, manual_masks={'worm.png':'original-sha'},video_mtime_ns=10)
        new = copy.deepcopy(old)
        new['config'].update(centerline_method='niu',scale_source='User-entered scale')
        self.assertTrue(compatible_identity(old, new))
        self.assertEqual(old['config']['centerline_method'], 'wormtracer')

    def test_actual_inputs_still_cannot_reuse_mismatched_results(self):
        c, _ = validate_config(self.config)
        old = dict(config=c, manual_masks={'worm.png':'original-sha'},video_mtime_ns=10)
        for field in ('mask','scale','video','crop','fit'):
            new = copy.deepcopy(old)
            if field=='mask': new['manual_masks']['worm.png']='changed-sha'
            elif field=='scale': new['config']['scale_um_per_px']*=1.01
            elif field=='video': new['video_mtime_ns']=11
            elif field=='crop': new['config']['crop_xyxy'][0]+=1
            else: new['config']['wormtracer']['rescale']=.1
            self.assertFalse(compatible_identity(old,new),field)
        self.assertFalse(compatible_identity({}, old))


if __name__ == '__main__':
    unittest.main()
