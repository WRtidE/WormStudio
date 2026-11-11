"""Ordered frame transport, real child processes, and run isolation without a GPU."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

import cv2
import numpy as np

from live_preview import report_preview, report_geometry, preview_batch
from pipeline_progress import PipelineProgress
from manual_worm_annotation import create_server
from worm_pipeline import run_stage


class LivePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='worm-live-')
        self.base = Path(self.temp.name)
        self.progress = PipelineProgress()
        self.config = dict(sample='live-test', video='unused.mp4', centerline_method='niu')
        self.stages = [dict(name='tracking', phase='tracking'), dict(name='niu-worm-1', phase='fit')]
        self.info = dict(ids=[1], frames=3)
        self.progress.configure(self.config, self.info, self.stages, self.base)
        self.env = patch.dict(os.environ, WORM_PREVIEW_DIR=str(self.progress.preview_dir), WORM_PREVIEW_STAGE='test')
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.progress.close()
        self.temp.cleanup()

    def test_forward_reverse_and_duplicate_source_frames_are_not_dropped(self):
        image = np.zeros((20, 30, 3), np.uint8)
        for frame in [0, 1, 2, 2, 1, 0]:
            self.assertTrue(report_preview(image, frame, 3, 'tracking'))
        batch = self.progress.preview(self.progress.run_id)
        self.assertEqual([r['sequence'] for r in batch['frames']], list(range(1, 7)))
        self.assertEqual([r['frame'] for r in batch['frames']], [0, 1, 2, 2, 1, 0])
        self.assertEqual([r['sequence'] for r in preview_batch(self.progress.preview_dir, 2, 2)['frames']], [3, 4])
        self.assertEqual(self.progress.preview(self.progress.run_id, 6)['frames'], [])

    def test_partial_unpublished_frame_is_invisible_and_images_are_bounded(self):
        image = np.full((1500, 2500, 3), 128, np.uint8)
        report_preview(image, 0, 3)
        (self.progress.preview_dir / '000000002.jpg').write_bytes(b'partial')
        batch = self.progress.preview(self.progress.run_id)
        self.assertEqual(batch['latest'], 1)
        decoded = cv2.imdecode(np.frombuffer(self.progress.preview(self.progress.run_id, sequence=1), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape, (576, 960, 3))
        self.assertTrue(np.all(image == 128))

    def test_geometry_preview_is_optional_and_never_mutates_measurement_inputs(self):
        mask = np.zeros((40, 60), bool); mask[10:30, 10:50] = True
        line = np.array([[10., 20.], [50., 20.]])
        image = np.zeros((40, 60, 3), np.uint8)
        original = (mask.copy(), line.copy(), image.copy())
        self.assertTrue(report_geometry(mask, line, 0, 3, image=image))
        for actual, expected in zip((mask, line, image), original):
            np.testing.assert_array_equal(actual, expected)
        with patch.dict(os.environ, WORM_PREVIEW_DIR=''):
            self.assertFalse(report_preview(None, 0, 3))
        with patch('cv2.imencode', side_effect=OSError('disk/encoder unavailable')):
            self.assertFalse(report_preview(image, 0, 3))
        self.assertEqual(self.progress.preview(self.progress.run_id)['latest'], 1)

    def test_sequence_continues_across_real_stage_processes(self):
        for stage in range(2):
            log = self.base / f'stage-{stage}.log'
            self.progress.begin(stage, log)
            command = [sys.executable, '-c', 'import numpy as np; from live_preview import report_preview; '
                       '[report_preview(np.zeros((20,30,3),np.uint8),f,3) for f in range(3)]']
            run_stage(dict(name='preview-test', command=command), log, self.progress)
            self.progress.done(stage)
        frames = self.progress.preview(self.progress.run_id)['frames']
        self.assertEqual(len(frames), 6)
        self.assertEqual(frames[0]['stage'], '虫体识别与跟踪')
        self.assertEqual(frames[3]['stage'], '姿态分析 · 线虫 1')

    def test_api_rejects_other_runs_and_traversal_and_handles_run_reset(self):
        report_preview(np.zeros((20,30,3),np.uint8), 0, 3)
        self.progress.bind_server(create_server(progress=self.progress))
        base = self.progress.url.removesuffix('/progress') + '/api/live-preview?run='
        old_run = self.progress.run_id
        with urllib.request.urlopen(base + old_run + '&after=0') as response:
            self.assertEqual(json.load(response)['latest'], 1)
        with urllib.request.urlopen(base + old_run + '&sequence=1') as response:
            self.assertEqual(response.headers.get_content_type(), 'image/jpeg')
            self.assertTrue(response.read().startswith(b'\xff\xd8'))
        for suffix in ['other&sequence=1', old_run+'&sequence=../config', old_run+'&sequence=0', old_run+'&after=-1']:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + suffix)
            self.assertEqual(error.exception.code, 409)
        self.progress.configure(self.config, self.info, self.stages, self.base)
        self.assertNotEqual(self.progress.run_id, old_run)
        with self.assertRaises(ValueError): self.progress.preview(old_run)
        self.assertEqual(self.progress.preview(self.progress.run_id)['latest'], 0)


if __name__ == '__main__':
    unittest.main()
