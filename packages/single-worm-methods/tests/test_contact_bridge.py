import unittest
from unittest.mock import patch

import numpy as np

from wormstudio_single.contact_bridge import compare_paths, refine_contacts
from wormstudio_single.fusion_v4 import VERSION


def states(line, observed=(True, True)):
    quality = dict(length_relative_deviation=0., skeleton_coverage=1., geometric_pass=True)
    return [dict(points=p, endpoint_observed=ends, name='loop', quality=quality)
            for p, ends in [(line, list(observed)), (line[::-1], list(observed[::-1]))]]


class ContactBridgeTests(unittest.TestCase):
    def setUp(self):
        x = np.linspace(0, 100, 101)
        self.line = np.column_stack((x, 8*np.sin(x/25)))
        self.sequence = [states(self.line + [0, i]) for i in range(5)]

    def test_boundary_order_reflection_scale_and_time_reversal(self):
        for scale, mirror, reverse_time in [(1, 1, False), (2, -1, False), (.5, 1, True)]:
            sequence = [states((s[0]['points'] + [45, 8])*[mirror*scale, scale])
                        for s in self.sequence]
            if reverse_time:
                sequence.reverse()
            sequence[-1].reverse()
            checks = compare_paths(sequence, 100*scale)
            self.assertEqual(len(checks), 3)
            for q in checks:
                self.assertTrue(q['path_agreement'] and q['tip_agreement'])
                self.assertEqual(q['right_reversed'], 1)

    def test_indistinguishable_boundary_orders_abstain(self):
        self.sequence[-1][1] = self.sequence[-1][0].copy()
        self.assertEqual(compare_paths(self.sequence, 100), [])

    def run_event(self, observed, invalid=None):
        lines = np.array([s[0]['points'] for s in self.sequence])
        quality = [dict(topology='simple' if i in (0,4) else 'paired_contact',
                        centerline_valid=i in (0,4), mask_valid=True) for i in range(5)]
        qa = [dict(s[0]['quality'], solver=VERSION, endpoint_observed=list(observed))
              for s in self.sequence]
        masks = np.ones((5, 120, 130), bool)
        meta = dict(fps=12, reference_length_px=100., anchors=[0,4],
                    contact_identity_segments=[0]*5)
        if invalid == 'identity':
            meta['contact_identity_segments'][2] = 1
        if invalid == 'time':
            meta['fps'] = 2
        if invalid == 'mask':
            quality[2]['mask_valid'] = False
        if invalid == 'anchor':
            quality[4]['centerline_valid'] = False
        choices = [states(p, observed) for p in lines[1:-1]]
        with patch('wormstudio_single.contact_bridge.candidate_states', side_effect=choices):
            refine_contacts(lines, qa, masks, quality, lines.copy(), meta)
        return qa

    def test_hidden_tip_stays_inferred_and_observed_pose_can_pass(self):
        for observed in [(True, True), (False, True)]:
            qa = self.run_event(observed)
            self.assertEqual(qa[2]['contact_bridge']['measured'], all(observed))
            self.assertEqual(qa[2]['endpoint_observed'], list(observed))
            self.assertTrue(qa[2]['contact_bridge']['path_supported'])

    def test_no_bridge_across_identity_change_bad_mask_missing_anchor_or_long_gap(self):
        for bad in ['identity','mask','anchor','time']:
            with self.subTest(bad=bad):
                self.assertFalse(any('contact_bridge' in q for q in self.run_event((True,True), bad)))
