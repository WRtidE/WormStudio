import unittest
import numpy as np
from adaptive_endpoints import BodyScale, estimate_scale, link_order, match_partial


class AdaptiveEndpointTests(unittest.TestCase):
    def test_length_width_and_gates_follow_image_scale(self):
        reference = None
        for zoom in (1., 2.):
            line = np.column_stack((np.linspace(0, 200, 101), np.zeros(101))) * zoom
            row = dict(source_valid=True, display_source='formal', centerline_topology='simple',
                       display_inside_fraction=1.)
            guard = dict(area_px=2000 * zoom**2, mask_valid=True, source_frame_invalid=False,
                         contact_ids=[], boundary_truncated=False, fragmented=False)
            scale = estimate_scale([row]*4,[line]*3+[line*.2],
                                   [guard]*3+[dict(guard, boundary_truncated=True)])
            self.assertEqual(scale.samples,3)
            self.assertEqual(scale.length_px, 200 * zoom)
            self.assertEqual(scale.width_px, 10 * zoom)
            values = np.array([scale.tip_radius, scale.branch_length])/zoom
            if reference is not None:
                np.testing.assert_allclose(values,reference)
            reference = values
            moved = (line+np.array([30.,30.])*zoom)[::-1]
            self.assertEqual(link_order(line,moved,None,[True,False],scale,1/12,True)[0],1)

    def test_occluded_end_is_not_filled_from_midpoint(self):
        scale = BodyScale(200,12,8,'measured_simple')
        last = [(10,np.array([10.,10.])),(10,np.array([190.,10.]))]
        self.assertEqual(set(match_partial([[18,11],[100,9]],last,11,12,scale)),{0})
        self.assertFalse(match_partial([[18,11]],last,20,12,scale))
