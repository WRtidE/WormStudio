import unittest

import numpy as np

from wormstudio_single.baseline import recover_endpoint_jumps


class EndpointWindowTest(unittest.TestCase):
    def poses(self, bends=(0, 0, 21, 22, 23)):
        # The thin terminal region moves while the trunk translates steadily.
        x = np.linspace(0, 100, 101)
        profile = (x / 100) ** 32
        poses = np.array([np.column_stack((x + 3*i, bend * profile)) for i, bend in enumerate(bends)])
        poses[1::2] = poses[1::2, ::-1]
        baseline = poses.copy()
        baseline[2] = np.nan
        quality = [dict(centerline_valid=i != 2, mask_valid=True, topology='simple',
                        reference_length_px=100.,
                        quality_reasons=['centerline_temporal_jump'] if i == 2 else [])
                   for i in range(5)]
        return baseline, quality, poses

    def test_preserves_observed_pose_across_translation_and_reversed_order(self):
        baseline, quality, observed = self.poses()
        recover_endpoint_jumps(baseline, quality, observed)
        np.testing.assert_array_equal(baseline[2], observed[2])
        self.assertTrue(quality[2]['centerline_valid'])
        self.assertEqual(quality[2]['endpoint_jump_review']['supporting_frames'], [0, 1, 3, 4])

    def test_both_sides_and_both_distances_must_support_the_jump(self):
        for bends in [(0, 0, 21, 0, 0), (0, 0, 21, 22, 0), (0, 0, 30, 32, 32)]:
            with self.subTest(bends=bends):
                baseline, quality, observed = self.poses(bends)
                recover_endpoint_jumps(baseline, quality, observed)
                self.assertTrue(np.isnan(baseline[2]).all())
                self.assertFalse(quality[2]['centerline_valid'])

    def test_rejects_ambiguous_endpoint_mapping_and_other_quality_failures(self):
        for issue in ('excluded_neighbor', 'contact', 'length_outlier', 'ambiguous_order'):
            with self.subTest(issue=issue):
                baseline, quality, observed = self.poses()
                if issue == 'excluded_neighbor':
                    quality[0]['mask_valid'] = False
                elif issue == 'contact':
                    quality[2]['topology'] = 'unresolved_contact'
                elif issue == 'length_outlier':
                    quality[2]['quality_reasons'].append(issue)
                else:
                    baseline[0, -1] = baseline[0, 0]
                recover_endpoint_jumps(baseline, quality, observed)
                self.assertFalse(quality[2]['centerline_valid'])
                self.assertTrue(np.isnan(baseline[2]).all())
