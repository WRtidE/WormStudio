import unittest

import numpy as np

from track_order_evidence import reacquired_order


class ReacquiredOrderTests(unittest.TestCase):
    def setUp(self):
        def line(half):
            return np.column_stack((np.linspace(-half, half, 101), np.zeros(101)))
        self.history = {0: line(65), 1: line(15)}
        self.candidate, self.next_curve = line(60), line(62)
        def group(curve):
            return ('window',0,'query',np.arange(21),curve[::5].copy(),np.ones(21,bool))
        self.tracks = [{'forward':[group(p)]} for p in (self.history[0],self.history[1],
                       self.candidate[::-1],self.next_curve[::-1])]

    def result(self):
        return reacquired_order(2,self.candidate,self.next_curve,self.history,self.tracks,130)

    def test_consistent_query_ids_restore_order_after_fast_turn(self):
        result = self.result()
        self.assertEqual(result['current']['visible_matched'],21)
        self.assertEqual(result['following']['visible_matched'],21)
        self.assertEqual(result['anchor'],0)

    def test_velocity_alone_does_not_overrule_a_genuine_reversal(self):
        self.tracks[2]['forward'][0][5][:] = False
        self.assertIsNone(self.result())

    def test_confirmation_frame_must_support_the_same_order(self):
        self.tracks[3]['forward'][0][4][:] = self.next_curve[::5].copy()
        self.assertIsNone(self.result())
