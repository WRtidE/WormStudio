"""Boundary tests for the existing single-worm method's Studio adapter."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from extract_tapnext import measurement_output
import analyze_phenotypes
from tapnext_support import validate_single_worm, availability
from worm_pipeline import build_stages, complete


class TapnextIntegrationTests(unittest.TestCase):
    def test_nonzero_roi_is_added_once_and_global_crop_is_not_added(self):
        curves = np.ones((2, 101, 2)) * [3, 5]
        base = [dict(topology='simple'), dict(topology='paired_contact')]
        good = dict(pose_metrics_valid=True, mask_metrics_valid=True, review_reason='',
            candidate='tapnext', endpoint_a_observed=True, endpoint_b_observed=True,
            temporal_reconstructed=False, status='auto_supported')
        bad = dict(good, pose_metrics_valid=False, review_reason='whole_event_temporal_inference',
            temporal_reconstructed=True, endpoint_b_observed=False, status='temporal_recovered')
        meta = dict(tracking_roi=[40, 60, 200, 200], crop_xyxy=[300, 400, 600, 700], anchors=[0])
        points, quality, diagnostic = measurement_output(curves, base, [good, bad], meta)
        np.testing.assert_array_equal(points[0], np.tile([43, 65], (101, 1)))
        self.assertTrue(np.isnan(points[1]).all())
        np.testing.assert_array_equal(diagnostic[1], np.tile([43, 65], (101, 1)))
        self.assertFalse(quality[1]['centerline_valid'])
        self.assertTrue(quality[1]['endpoint_b_inferred'])
        self.assertEqual(quality[1]['quality_reasons'], ['whole_event_temporal_inference'])
        self.assertTrue(np.isfinite(curves).all())  # Do not mutate solver output.

    def test_missing_candidate_never_becomes_valid_even_if_solver_claims_valid(self):
        row = dict(pose_metrics_valid=True, mask_metrics_valid=True, review_reason='',
            candidate='none', endpoint_a_observed=False, endpoint_b_observed=False,
            temporal_reconstructed=False, status='unavailable')
        p, q, _ = measurement_output(np.full((1, 101, 2), np.nan), [dict(topology='simple')],
            [row], dict(tracking_roi=[0, 0, 100, 100], anchors=[]))
        self.assertTrue(np.isnan(p).all())
        self.assertFalse(q[0]['centerline_valid'])
        self.assertTrue(q[0]['quality_reasons'])

    def test_single_worm_and_large_requirements(self):
        validate_single_worm(1, 'large')
        for count, model in [(0, 'large'), (2, 'large'), (1, 'tiny')]:
            with self.assertRaises(ValueError): validate_single_worm(count, model)

    def test_pipeline_routes_to_real_adapter_and_standard_exports(self):
        stages = build_stages(dict(sample='routing', centerline_method='tapnext'),
            dict(ids=[9], frames=8), Path('config.json'), Path('phenotypes.json'))
        self.assertEqual([s['name'] for s in stages],
            ['tracking', 'cleanup', 'quality', 'tapnext-worm-9', 'metrics', 'export'])
        self.assertTrue(any(p.endswith('extract_tapnext.py') for p in stages[3]['command']))
        self.assertIn('tapnext', stages[4]['command'])
        self.assertIn('centerlines-tapnext', stages[-1]['command'])

    def test_changed_local_algorithm_invalidates_cached_fit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            np.save(root/'centerlines.npy', np.full((5, 101, 2), np.nan))
            (root/'quality.json').write_text(json.dumps([dict(centerline_valid=False)]*5))
            (root/'run.json').write_text(json.dumps(dict(algorithm_signature='original')))
            stage = dict(name='tapnext-worm-9', phase='fit', outputs=[
                str(root/name) for name in ('centerlines.npy', 'run.json', 'quality.json')])
            with patch('tapnext_support.signature', return_value='original'):
                self.assertTrue(complete(stage, {}, dict(frames=5)))
            with patch('tapnext_support.signature', return_value='changed'):
                self.assertFalse(complete(stage, {}, dict(frames=5)))

    def test_unavailable_dependency_has_readable_ui_status(self):
        with patch('tapnext_support.resources', side_effect=ValueError('missing local package')):
            self.assertEqual(availability(), dict(available=False, detail='missing local package'))

    def test_utf8_mask_manifest_with_chinese_workspace_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root/'outputs'/'sample'/'centerlines-tapnext'
            output.mkdir(parents=True)
            source = str(root/'线虫实验'/'sam2-tracking')
            (output/'mask-source.json').write_text(json.dumps(dict(mask_source=source),
                ensure_ascii=False), encoding='utf-8')
            with patch.object(analyze_phenotypes, 'ROOT', root):
                self.assertEqual(analyze_phenotypes.fitted_mask_source('sample', 'centerlines-tapnext'), source)


if __name__ == '__main__':
    unittest.main()
