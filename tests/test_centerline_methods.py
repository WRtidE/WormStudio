"""Only the current body-sequence profile is computable; archived names stay readable."""
import unittest
from pathlib import Path

from pose.centerline_methods import (ARCHIVED, CURRENT_METHOD, analysis_method,
                                display_method, method_source)
from worm_pipeline import build_stages


class CenterlineMethodTests(unittest.TestCase):
    def test_current_method_routes_one_body_sequence_stage(self):
        stages = build_stages(dict(sample='routing-test', centerline_method=CURRENT_METHOD),
                              dict(ids=[1], frames=5), Path('cfg.json'), Path('metrics.json'))
        self.assertEqual([s['name'] for s in stages],
                         ['tracking', 'cleanup', 'quality', 'body_sequence-worm-1', 'metrics', 'export'])
        self.assertEqual([s['command'][2:4] for s in stages], [
            ['-m', 'tracking.track_configured_video'],
            ['-m', 'tracking.clean_tracking_components'],
            ['-m', 'quality.quality_stage'],
            ['-m', 'pose.extract_body_sequence'],
            ['-m', 'measurements.analyze_phenotypes'],
            ['-m', 'app.review_configured_video'],
        ])
        self.assertIn('centerlines-body-sequence', stages[-1]['command'])

    def test_archived_methods_are_readable_but_not_computable(self):
        for name in ARCHIVED:
            self.assertEqual(display_method(name)['source'], method_source(name))
            if name == 'tapnext':
                # Retired in 1.0.0: its tasks continue with the current method.
                self.assertEqual(analysis_method(name), CURRENT_METHOD)
            else:
                with self.assertRaises(ValueError):
                    analysis_method(name)

    def test_unknown_method_is_rejected(self):
        with self.assertRaises(ValueError):
            analysis_method('skeleton')


if __name__ == '__main__':
    unittest.main()
