import base64
import json
import unittest
from types import SimpleNamespace

import cv2
import numpy as np

from measurements.annotation_metrics import measure_annotation, measure_mask


class AnnotationMetricTests(unittest.TestCase):
    def setUp(self):
        self.mask = np.zeros((180, 360), np.uint8)
        cv2.ellipse(self.mask, (180, 90), (130, 12), 0, 0, 360, 255, -1)

    def measure(self, mask=None, mode='physical', scale=1, origin=(0, 0)):
        return measure_mask(self.mask if mask is None else mask, frame=7, worm_id=1,
                            measurement_mode=mode, scale_um_per_px=scale, origin=origin)

    def values(self, result):
        return {m['key']: m['value'] for m in result['metrics']}

    def body(self, **overrides):
        rgba = cv2.cvtColor(self.mask, cv2.COLOR_GRAY2BGRA)
        rgba[:, :, :3] = 255
        rgba[:, :, 3] = self.mask
        _, encoded = cv2.imencode('.png', rgba)
        result = dict(frame=7, worm_id=1, confirmed=True, measurement_mode='physical',
                      scale_um_per_px=2, roi=[10, 10, 350, 170],
                      mask='data:image/png;base64,'+base64.b64encode(encoded).decode())
        result.update(overrides)
        return result

    def test_straight_body_has_physical_geometry_and_no_temporal_metrics(self):
        result = self.measure()
        values = self.values(result)
        self.assertTrue(result['pose_available'])
        self.assertEqual(len(result['centerline']), 101)
        self.assertAlmostEqual(values['length'], 260, delta=3)
        self.assertAlmostEqual(values['extension'], 1, delta=.002)  # Pixel skeleton of an ellipse is not perfectly straight.
        self.assertEqual(values['area'], int(np.count_nonzero(self.mask)))
        self.assertEqual(result['scope'], 'annotation_frame')
        self.assertEqual(result['frame'], 7)
        self.assertEqual(set(values), {'length', 'width', 'area', 'extension', 'ratio', 'volume'})
        mid = np.rint(result['body_center']).astype(int)
        self.assertTrue(self.mask[mid[1], mid[0]])

    def test_scale_powers_and_pixel_units_are_consistent(self):
        a, b = self.values(self.measure()), self.values(self.measure(scale=2))
        for key, power in [('length', 1), ('width', 1), ('area', 2), ('volume', 3), ('ratio', 0), ('extension', 0)]:
            self.assertAlmostEqual(b[key], a[key] * 2 ** power)
        pixel = self.measure(mode='pixels', scale=None)
        self.assertEqual(self.values(pixel), a)
        self.assertEqual([m['unit'] for m in pixel['metrics']], ['px', 'px', 'px²', '', '', 'px³'])
        self.assertIsNone(pixel['scale_um_per_px'])
        json.dumps(pixel, allow_nan=False)

    def test_fragmented_and_closed_loop_frames_keep_area_but_do_not_invent_length(self):
        fragmented = self.mask.copy(); fragmented[10:15, 10:15] = 255
        ring = np.zeros_like(self.mask); cv2.circle(ring, (180, 90), 40, 255, 10)
        for mask in [fragmented, ring]:
            result = self.measure(mask)
            self.assertFalse(result['pose_available'])
            values = self.values(result)
            self.assertIsNone(values['length'])
            self.assertIsNone(values['volume'])
            self.assertGreater(values['area'], 0)
            self.assertTrue(result['note'])

    def test_boundary_truncation_is_not_presented_as_full_body_length(self):
        result = self.measure(self.mask[:, 60:])
        self.assertFalse(result['pose_available'])
        self.assertIn('边界', result['note'])
        area = next(m for m in result['metrics'] if m['key'] == 'area')
        self.assertIn('可见部分', area['note'])

    def test_tiny_speckle_does_not_hide_clear_centerline_and_is_disclosed(self):
        mask = self.mask.copy(); mask[5, 5] = 255
        result = self.measure(mask)
        self.assertTrue(result['pose_available'])
        self.assertEqual(result['ignored_component_pixels'], 1)
        self.assertIn('1 个离散像素', result['note'])
        self.assertEqual(self.values(result)['area'], np.count_nonzero(mask))
        self.assertEqual(self.values(result)['length'], self.values(self.measure())['length'])

    def test_request_uses_alpha_and_returns_annotation_coordinates(self):
        annotation = SimpleNamespace(frame=7, image=np.zeros((180, 360, 3)))
        result = measure_annotation(annotation, self.body())
        expected = self.measure(scale=2)
        self.assertEqual(self.values(result)['area'], self.values(expected)['area'])
        np.testing.assert_allclose(result['body_center'], expected['body_center'], atol=1)
        for invalid in [self.body(frame=8), self.body(confirmed=False), self.body(scale_um_per_px=0),
                        self.body(roi=[120, 20, 350, 170]), self.body(worm_id=True), self.body(mask='bad')]:
            with self.assertRaises(ValueError):
                measure_annotation(annotation, invalid)


if __name__ == '__main__':
    unittest.main()
