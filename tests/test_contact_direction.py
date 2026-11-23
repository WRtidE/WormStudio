import unittest

import numpy as np

from head_temporal import apply_contact_directions, orient_loop_reviews


class ContactDirections(unittest.TestCase):
    def fixture(self, left=1, right=None):
        rows = [dict(identity_valid=True, source_valid=True, mask_valid=True, identity_segment=0)
                for i in range(6)]
        heads = [dict(head_anchor_count=0, head_anchor_conflict=False, head_reason='posture_unreliable',
                      head_segment=0 if i<1 else 1 if i>=4 else -1, head_endpoint=None,
                      head_identity_confirmed=False, head_input_flipped=i==5, head_evidence_score=.3)
                 for i in range(6)]
        heads[0]['head_endpoint'], heads[4]['head_endpoint'] = left, right
        qa = [dict(contact_bridge=dict(anchors=[0,4], right_reversed=1,
                  energy_gap=2., tip_agreement=True)) if 0<i<4 else {} for i in range(6)]
        return heads, rows, qa

    def test_single_seed_orients_contact_and_oppositely_ordered_exit(self):
        heads, rows, qa = self.fixture()
        apply_contact_directions(heads, rows, qa, 'taper_seed')
        self.assertEqual([h['head_endpoint'] for h in heads], [1,1,1,1,0,1])
        self.assertFalse(any(h['head_identity_confirmed'] for h in heads))

    def test_no_seed_conflict_manual_only_and_identity_boundary(self):
        for issue in ['absent', 'conflicting', 'manual_only', 'identity', 'rejected_anchor']:
            heads, rows, qa = self.fixture(None if issue=='absent' else 1, 1 if issue=='conflicting' else None)
            if issue=='identity':
                rows[3]['identity_segment'] = 2
            if issue=='rejected_anchor':
                heads[2]['head_anchor_count']=1
            apply_contact_directions(heads,rows,qa,'manual_only' if issue=='manual_only' else 'taper_seed')
            self.assertTrue(all(heads[i]['head_endpoint'] is None for i in (1,2,3,5)))

    def test_uncertain_tip_stops_propagation_into_exit_segment(self):
        heads, rows, qa = self.fixture()
        qa[2]['contact_bridge']['tip_agreement'] = False
        apply_contact_directions(heads, rows, qa, 'taper_seed')
        self.assertIsNone(heads[2]['head_endpoint'])
        self.assertIsNone(heads[4]['head_endpoint'])

    def test_review_uses_both_boundaries_and_leaves_a_conflict_anonymous(self):
        x = np.linspace(5, 105, 101)
        display = {(1, i): np.column_stack((x + i, np.zeros_like(x))) for i in range(5)}
        display[1, 4] = display[1, 4][::-1]
        rows = [dict(source_valid=True, mask_valid=True, identity_valid=True, identity_segment=0,
                     display_source='diagnostic_candidate' if 0 < i < 4 else 'formal')
                for i in range(5)]
        quality = [dict(review_anchors=[0, 4], review_right_reversed=True) if 0 < i < 4 else {}
                   for i in range(5)]
        for right_end, expected in [(1, 0), (0, None)]:
            heads = [dict(head_identity_confirmed=i in (0, 4),
                          head_endpoint=0 if i == 0 else right_end if i == 4 else None)
                     for i in range(5)]
            orient_loop_reviews(rows, heads, quality, display, 1)
            self.assertEqual([h['head_endpoint'] for h in heads[1:4]], [expected] * 3)
            self.assertFalse(any(h['head_identity_confirmed'] for h in heads[1:4]))
