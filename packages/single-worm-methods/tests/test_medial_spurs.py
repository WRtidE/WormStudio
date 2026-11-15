"""Small contour spurs and true competing paths must produce different poses."""
import unittest

import cv2
import numpy as np

from wormstudio_single.baseline import resolve_sequence
from wormstudio_single.contact import candidate_states
from wormstudio_single.skeleton import body_path_source, extract_body_geometry, extract_niu


class MedialSpurTests(unittest.TestCase):
    def worm(self, spur):
        mask = np.zeros((110, 430), np.uint8)
        cv2.line(mask, (20, 70), (400, 70), 1, 17)
        cv2.line(mask, (120, 68), (120, 70 - spur), 1, 9)
        return mask.astype(bool)

    def test_local_spur_keeps_observed_body_through_baseline_and_fusion(self):
        mask = self.worm(8)
        geometry = extract_niu(mask)
        self.assertEqual(geometry['endpoints'], 3)
        self.assertEqual(geometry['topology'], 'complex')
        self.assertEqual(body_path_source(geometry), 'medial_with_short_spurs')
        baseline, quality, _ = resolve_sequence(mask[None])
        self.assertTrue(quality[0]['centerline_valid'])
        ends = baseline[0, [0, -1]]
        np.testing.assert_allclose(np.sort(ends[:, 0]), [20, 400], atol=3)
        np.testing.assert_allclose(ends[:, 1], 70, atol=2)
        states = candidate_states(mask, quality[0]['reference_length_px'])
        self.assertEqual({s['name'] for s in states}, {'medial_with_short_spurs'})

    def test_two_scale_fit_uses_original_image_coordinates_and_mask(self):
        mask = self.worm(12)
        original = mask.copy()
        self.assertIsNone(body_path_source(extract_niu(mask)))
        fitted, source = extract_body_geometry(mask)
        self.assertEqual(source, 'scale_stable_medial')
        np.testing.assert_array_equal(original, mask)
        points = np.rint(fitted['ordered']).astype(int)
        self.assertTrue(mask[points[:, 1], points[:, 0]].all())
        np.testing.assert_allclose(np.sort(points[[0, -1], 0]), [20, 400], atol=4)
        np.testing.assert_array_equal(fitted['raw_xy'], extract_niu(mask)['raw_xy'])

    def test_competing_branch_loop_and_separate_component_remain_ambiguous(self):
        loop = self.worm(8).astype(np.uint8)
        cv2.circle(loop, (220, 48), 22, 1, 5)
        separate = self.worm(8)
        separate[8:14, 15:40] = True
        for mask in (self.worm(34), loop.astype(bool), separate):
            with self.subTest(pixels=mask.sum()):
                self.assertIsNone(extract_body_geometry(mask)[1])
