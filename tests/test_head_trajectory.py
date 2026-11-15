import unittest
import numpy as np

from head_tail import detect_head_tail
from frame_metrics import present_frame


class TrajectoryTest(unittest.TestCase):
    def setUp(self):
        def coil(gap):
            angles = np.linspace(gap / 2, 2 * np.pi - gap / 2, 101)
            return 40 * np.column_stack((np.cos(angles), np.sin(angles)))
        self.middle = 5
        self.curves = np.stack([coil(0.02 if i == self.middle else 1.) for i in range(11)])
        # Reverse locomotion at contact and independently reverse stored A/B.
        self.curves[:, :, 0] += (5 - np.abs(np.arange(11) - 5))[:, None] * 5
        self.curves[1::2] = self.curves[1::2, ::-1]
        self.visible = np.ones((11, 2), bool)
        self.visible[self.middle] = False
        self.valid = np.ones(11, bool)

    def track(self, anchors, **changes):
        kw = dict(mode='manual_seed', fps=12, endpoint_visible=self.visible)
        kw.update(changes)
        return detect_head_tail(self.curves, self.valid, manual=anchors, **kw)

    def test_reversed_motion_and_storage_at_two_hidden_tips(self):
        anchor = {'frame': 0, 'point': self.curves[0, 0]}
        rows = self.track(anchor)
        self.assertEqual([r['head_endpoint'] for r in rows], [i % 2 for i in range(11)])
        row = rows[self.middle]
        self.assertEqual(row['head_reason'], 'trajectory_frame_orientation')
        self.assertFalse(row['head_identity_confirmed'])
        refs = row['head_trajectory_frames']
        self.assertLess(min(refs), self.middle)
        self.assertGreater(max(refs), self.middle)
        self.assertNotIn(self.middle, refs)
        self.assertEqual(present_frame(dict(row, worm_id=1, head_trajectory_frames=str(refs)))['head_trajectory_frames'], refs)

    def test_future_alone_can_resolve_one_visible_end_but_not_two_hidden_ends(self):
        self.valid[:self.middle] = False
        # Ensure the remaining visible end is the tail in this frame's native order.
        self.visible[self.middle, 0] = True
        anchor = {'frame': 10, 'point': self.curves[10, 0]}
        row = self.track(anchor)[self.middle]
        self.assertEqual(row['head_endpoint'], 1)
        self.assertGreater(min(row['head_trajectory_frames']), self.middle)
        self.visible[self.middle] = False
        self.assertIsNone(self.track(anchor)[self.middle]['head_endpoint'])

    def test_no_identity_transfer_across_invalid_pose_or_new_worm(self):
        anchor = {'frame': 10, 'point': self.curves[10, 0]}
        self.visible[self.middle, 0] = True
        ids = np.array([0] * 6 + [1] * 5)
        self.assertIsNone(self.track(anchor, segment_ids=ids)[self.middle]['head_endpoint'])
        self.valid[:self.middle] = False
        self.valid[self.middle+1] = False
        self.assertIsNone(self.track(anchor)[self.middle]['head_endpoint'])
        self.assertIsNone(self.track(None)[self.middle]['head_endpoint'])

    def test_indistinguishable_arms_or_conflicting_anchors_leave_overlap_unassigned(self):
        anchors = [{'frame': 0, 'point': self.curves[0, 0]}, {'frame': 10, 'point': self.curves[10, -1]}]
        self.assertIsNone(self.track(anchors)[self.middle]['head_endpoint'])
        p = self.curves[self.middle].copy()
        self.curves[self.middle] = (p + p[::-1]) / 2
        self.assertIsNone(self.track(anchors[0])[self.middle]['head_endpoint'])

    def test_later_seed_recovers_earlier_block_in_native_order(self):
        line = np.column_stack((np.arange(101), np.zeros(101)))
        self.curves = np.tile(line, (11, 1, 1))
        self.curves[5:, :, 1] += 31
        self.curves[1::2] = self.curves[1::2, ::-1]
        self.visible[:] = True
        anchor = {'frame': 10, 'point': self.curves[10, -1]}
        rows = self.track(anchor)
        self.assertEqual([r['head_endpoint'] for r in rows], [1-i%2 for i in range(11)])
        self.assertEqual(rows[0]['head_reason'], 'trajectory_block_orientation')
        self.assertFalse(rows[0]['head_identity_confirmed'])
        self.curves[:5, :, 1] += 200
        self.assertIsNone(self.track(anchor)[0]['head_endpoint'])
