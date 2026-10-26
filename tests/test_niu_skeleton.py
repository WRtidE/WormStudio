"""Topology and same-input invariants for the separate NIU comparison."""
import unittest
import cv2
import numpy as np
from skimage.morphology import skeletonize
from compare_niu_skeleton import extract_niu, skeleton_graph, line_metrics, sequence_metrics, align


class NIUSkeletonTests(unittest.TestCase):
    def test_raw_matches_full_frame_blog_call_and_preserves_input(self):
        mask = np.zeros((180, 210), np.uint8)
        cv2.ellipse(mask, (105, 90), (60, 30), 0, 15, 280, 1, 13)
        cv2.circle(mask, (15, 20), 4, 1, -1)  # disconnected part is retained in raw output
        mask = mask.astype(bool)
        original = mask.copy()
        result = extract_niu(mask)
        xy = np.argwhere(skeletonize(mask))[:, ::-1]
        np.testing.assert_array_equal(result['raw_xy'], xy)
        np.testing.assert_array_equal(mask, original)
        selected = np.random.default_rng(42).choice(len(xy), size=5, replace=False)
        np.testing.assert_array_equal(result['nodes_xy'], xy[selected])
        self.assertEqual(result['components'], 2)

    def test_diagonal_staircase_is_not_a_false_cycle(self):
        sk = np.zeros((12, 12), bool)
        for i in range(2, 8):
            sk[i, i] = sk[i, i + 1] = True
        coords, graph = skeleton_graph(sk)
        degree = np.diff(graph.indptr)
        self.assertEqual(int(np.sum(degree == 1)), 2)
        self.assertFalse(np.any(degree > 2))
        self.assertEqual(graph.nnz // 2, len(coords) - 1)

    def test_closed_ring_does_not_invent_endpoints_or_borrow_line(self):
        mask = np.zeros((180, 180), np.uint8)
        cv2.circle(mask, (90, 90), 55, 1, 13)
        result = extract_niu(mask.astype(bool))
        self.assertGreater(len(result['raw_xy']), 100)
        self.assertEqual(result['endpoints'], 0)
        self.assertIsNone(result['ordered'])
        self.assertGreaterEqual(result['cycles'], 1)

    def test_simple_worm_has_ordered_path_and_shared_metrics(self):
        mask = np.zeros((160, 220), np.uint8)
        cv2.line(mask, (30, 80), (190, 80), 1, 17)
        result = extract_niu(mask.astype(bool))
        self.assertEqual(result['topology'], 'simple')
        self.assertEqual(result['ordered'].shape, (101, 2))
        metric = line_metrics(result['ordered'], mask.astype(bool), result['raw_xy'], result['radius'])
        self.assertTrue(metric['geometric_pass'])
        self.assertGreater(metric['skeleton_coverage'], .99)

    def test_empty_mask_keeps_missing_output(self):
        result = extract_niu(np.zeros((20, 20), bool))
        self.assertIsNone(result['ordered'])
        self.assertEqual(len(result['nodes_xy']), 0)
        self.assertFalse(line_metrics(None, np.zeros((20, 20), bool), result['raw_xy'], 1)['available'])

    def test_endpoint_direction_and_translation_do_not_create_fake_jumps(self):
        p = np.column_stack([np.linspace(10, 110, 101), np.ones(101) * 20])
        q = (p + [12, 4])[::-1]
        np.testing.assert_allclose(align(q, p), p + [12, 4])
        values = sequence_metrics(q, p, np.array([62, 24]), np.array([50, 20]), 100)
        self.assertLess(values['endpoint_change_fraction'], 1e-8)
        self.assertLess(values['length_change_fraction'], 1e-8)
        self.assertTrue(np.isnan(sequence_metrics(None, p, None, None, None)['length_change_fraction']))


if __name__ == '__main__':
    unittest.main()
