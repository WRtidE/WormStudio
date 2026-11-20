import unittest

import numpy as np

from head_tail.endpoint_order import fast_turn_link, hairpin_link, order_endpoints, sampled


class EndpointOrderTests(unittest.TestCase):
    def test_hairpin_tips_slide_on_separate_arms(self):
        def pose(start, stop):
            t = np.linspace(start, stop, 201)
            return sampled(np.column_stack((100 * (1 - t*t), 30 * t)))
        previous = pose(-1., .85)
        current = pose(-.8, 1.1)
        self.assertEqual(hairpin_link(previous, current)['flip'], 0)
        self.assertEqual(hairpin_link(previous, current[::-1])['flip'], 1)
        self.assertIsNone(hairpin_link(previous, current + [120, 0]))

    def test_fast_turn_uses_the_following_pose_in_either_storage_order(self):
        along = np.linspace(-50, 50, 101)[:, None]
        def pose(angle):
            radians = np.radians(angle)
            return along * [np.cos(radians), np.sin(radians)]
        for following in (pose(65), pose(65)[::-1]):
            self.assertEqual(fast_turn_link(pose(0), pose(53)[::-1], following)['flip'], 1)
        self.assertIsNone(fast_turn_link(pose(0), pose(90), pose(90)))

    def fixture(self, n):
        rows = [dict(display_source='formal', source_valid=True, mask_valid=True,
                     identity_valid=True, identity_segment=1, identity_link_source='seed_continuous')
                for _ in range(n)]
        heads = [dict(head_segment=-1, head_input_flipped=False, head_endpoint=None,
                      head_identity_confirmed=False, head_anchor_conflict=False, head_anchor_count=0,
                      head_reason='posture_unreliable', head_evidence_score=0.,
                      head_status='unconfirmed_A_B_endpoints') for _ in rows]
        p = np.column_stack((np.linspace(20,120,101), np.zeros(101)))
        curves = {(1,i): (p[::-1] if i%2 else p) + [abs(2-i), i] for i in range(n)}
        return rows, heads, curves

    def seed(self, head, endpoint):
        head.update(head_endpoint=endpoint,head_identity_confirmed=True,head_reason='manual_anchor',
                    head_evidence_score=1.,head_status='confirmed_by_user_point')

    def test_reversed_vertices_review_frame_and_backward_motion(self):
        rows, heads, curves = self.fixture(5)
        self.seed(heads[2], 0)
        rows[1]['display_source']='mask_refit'
        order_endpoints(rows,heads,curves,1,12,True,True)
        self.assertEqual([h['head_endpoint'] for h in heads],[0,1,0,1,0])
        self.assertEqual([h['head_identity_confirmed'] for h in heads],[False,False,True,False,False])
        self.assertEqual([r['endpoint_order_reversed'] for r in rows],[False,True,False,True,False])

    def test_identity_break_blocks_manual_label_and_automatic_reseeding(self):
        rows,heads,curves=self.fixture(5)
        self.seed(heads[0],0)
        rows[2]['identity_valid']=False
        heads[3].update(head_endpoint=0,head_reason='taper_seed_initialization')
        order_endpoints(rows,heads,curves,1,12,True,True)
        self.assertEqual([h['head_endpoint'] for h in heads],[0,1,None,None,None])
        self.assertNotEqual(rows[0]['endpoint_order_block'],rows[3]['endpoint_order_block'])

    def test_conflicting_manual_anchors_abstain(self):
        rows,heads,curves=self.fixture(4)
        self.seed(heads[0],0); self.seed(heads[3],0)
        order_endpoints(rows,heads,curves,1,12,True,True)
        self.assertTrue(all(h['head_endpoint'] is None for h in heads))

    def test_short_single_seed_mask_hole(self):
        for single in (True,False):
            rows,heads,curves=self.fixture(3)
            self.seed(heads[0],0)
            rows[1].update(mask_valid=False,identity_link_source=None)
            rows[2]['identity_segment']=2
            order_endpoints(rows,heads,curves,1,12,True,single)
            self.assertEqual(heads[2]['head_endpoint'],0 if single else None)
            self.assertFalse(heads[2]['head_identity_confirmed'])

    def test_uncertain_contact_cannot_carry_labels(self):
        rows, heads, curves = self.fixture(3)
        self.seed(heads[0], 0)
        rows[1]['contact_bridge'] = dict(anchors=[0,2], tip_agreement=False, energy_gap=3.)
        order_endpoints(rows, heads, curves, 1, 12, True, True)
        self.assertIsNone(heads[1]['head_endpoint'])
        self.assertIsNone(heads[2]['head_endpoint'])

    def test_frozen_appearance_recovers_reversed_block_without_confirming(self):
        rows, heads, curves = self.fixture(12)
        self.seed(heads[0], 0)
        profile = np.linspace(-1.7, 1.7, 101)
        profiles = np.array([profile[::-1] if i%2 else profile for i in range(12)])
        for i in range(6,12):
            curves[1,i] += [500.,0.]
        order_endpoints(rows,heads,curves,1,12,True,True,profiles)
        self.assertNotEqual(rows[5]['endpoint_order_block'],rows[6]['endpoint_order_block'])
        for i in range(6,12):
            self.assertEqual(heads[i]['head_endpoint'],i%2)
            self.assertEqual(heads[i]['head_reason'],'appearance_temporal_reference')
            self.assertFalse(heads[i]['head_identity_confirmed'])

    def test_contact_accepts_solver_order_and_maps_exit_to_native_tip(self):
        rows, heads, curves = self.fixture(4)
        self.seed(heads[0], 0)
        for i in (1,2):
            rows[i].update(display_source='diagnostic_candidate',
                           contact_bridge=dict(anchors=[0,3], tip_agreement=True,
                                               energy_gap=2., right_reversed=True))
            curves[1,i] = curves[1,0] + [i,0]
        order_endpoints(rows,heads,curves,1,12,True,True)
        self.assertEqual([h['head_endpoint'] for h in heads],[0,0,0,1])
        self.assertEqual([h['head_identity_confirmed'] for h in heads],[True,False,False,False])
