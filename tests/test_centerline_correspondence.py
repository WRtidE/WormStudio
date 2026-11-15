import unittest

import numpy as np

from centerline_correspondence import constrain_trajectory_proposals, curve_links, overlap_link
from head_tail import detect_head_tail


def sliding_turn():
    radius = 8.
    poses = []
    for i, start in enumerate((-121, -103, -84, -65, -47, -29, -12, 5, 23)):
        s = np.arange(101) + start
        angle = np.clip(s / radius, 0, np.pi)
        x = radius * np.sin(angle) + np.minimum(s, 0) - np.maximum(s - np.pi*radius, 0)
        y = radius * (1 - np.cos(angle))
        curve = np.column_stack((x, y))
        poses.append(curve[::-1] if i % 2 else curve)
    return np.stack(poses)


class CenterlineCorrespondenceTest(unittest.TestCase):
    def test_sliding_hairpin_tracks_anatomy_under_arbitrary_storage_order(self):
        p = sliding_turn()
        for poses in (p, 4*p[:, :, ::-1] + [97, 21]):
            for i in range(len(poses)-1):
                self.assertEqual(overlap_link(poses[i], poses[i+1])['flip'], 1)
            rows = detect_head_tail(poses, np.ones(len(poses), bool), manual={'frame': 0, 'point': poses[0, 0]},
                                    mode='manual_seed', fps=12, endpoint_visible=np.ones((len(poses), 2), bool))
            self.assertEqual([r['head_endpoint'] for r in rows], [i%2 for i in range(len(poses))])

    def test_verified_pose_gap_recovers_next_frame_without_labelling_the_gap(self):
        p = sliding_turn()
        valid = np.ones(len(p), bool)
        valid[4] = False
        p[4] = np.nan
        allowed = ~valid
        seen = np.tile(valid[:, None], (1, 2))
        ids = np.zeros(len(p), int)
        links = curve_links(p, valid, seen, ids, 12, allowed, set())
        self.assertEqual(links[3, 5]['flip'], 0)
        self.assertGreaterEqual(len(links[3, 5]['frames']), 3)
        options = dict(mode='manual_seed', manual={'frame': 0, 'point': p[0, 0]}, fps=12,
                       endpoint_visible=seen, segment_ids=ids)
        rows = detect_head_tail(p, valid, pose_gap_allowed=allowed, **options)
        self.assertIsNone(rows[4]['head_endpoint'])
        self.assertEqual(rows[5]['head_endpoint'], 1)
        allowed[:] = False
        rows = detect_head_tail(p, valid, pose_gap_allowed=allowed, **options)
        self.assertIsNone(rows[5]['head_endpoint'])
        self.assertNotIn((3, 5), curve_links(p, valid, seen, ids, 12, ~valid, {4}))
        ids[4] = 1
        self.assertNotIn((3, 5), curve_links(p, valid, seen, ids, 12, ~valid, set()))

    def test_unrelated_or_indistinguishable_curves_leave_order_open(self):
        p = sliding_turn()[3]
        self.assertIsNone(overlap_link(p, p+[0, 100]))
        retraced = (p+p[::-1]) / 2
        self.assertIsNone(overlap_link(retraced, retraced.copy()))

    def test_linked_clips_use_a_single_anatomical_seed(self):
        # Translation breaks nearest-end tracking; shared body geometry connects the
        # clips. The taper reverses in the second clip and storage order varies too.
        line = np.column_stack((np.arange(101), np.zeros(101)))
        poses = np.tile(line, (16, 1, 1))
        poses[8:, :, 0] += 35
        flip = np.arange(16) % 2 == 1
        poses[flip] = poses[flip, ::-1]
        radius = np.full((16, 101), 5.)
        radius[:8, -13:] = 1
        radius[8:, :13] = 1
        radius[flip] = radius[flip, ::-1]
        options = dict(mode='taper_seed', fps=12, radius_profiles=radius,
                       endpoint_visible=np.ones((16, 2), bool))
        early = {'frame': 0, 'point': [0, 0]}
        late = {'frame': 15, 'point': [135, 0]}
        for anchor, head in ((early, 0), (None, 0), (late, 1)):
            rows = detect_head_tail(poses, np.ones(16, bool), manual=anchor, **options)
            self.assertEqual([r['head_endpoint'] for r in rows], (head ^ flip.astype(int)).tolist())
            self.assertEqual(rows[0]['head_temporal_block'], rows[-1]['head_temporal_block'])
            self.assertFalse(rows[-1 if anchor is early else 0]['head_identity_confirmed'])

        rows = detect_head_tail(poses, np.ones(16, bool), manual=[early, late], **options)
        self.assertTrue(all(r['head_anchor_conflict'] and r['head_endpoint'] is None for r in rows))
        rows = detect_head_tail(poses, np.ones(16, bool), manual=early, segment_ids=np.arange(16)//8, **options)
        self.assertEqual(rows[8]['head_endpoint'], 1)
        # A fitted but hidden tip cannot cast a taper vote for the initial head.
        options['endpoint_visible'][:8, 0] = False
        rows = detect_head_tail(poses, np.ones(16, bool), **options)
        self.assertEqual(rows[8]['head_endpoint'], 1)
        options['endpoint_visible'][:] = True
        # With no usable early taper, a later reliable seed can orient both clips.
        radius[:8] = 5
        rows = detect_head_tail(poses, np.ones(16, bool), **options)
        self.assertEqual([r['head_endpoint'] for r in rows], (1 ^ flip.astype(int)).tolist())
        self.assertEqual(rows[0]['head_temporal_seed_frame'], 8)

    def test_final_frame_predictions_obey_measured_correspondences(self):
        # Raw order reverses at 1. Frame 2 has a spurious independent prediction.
        links = {(0, 1): dict(flip=1, frames=[0, 1]), (1, 2): dict(flip=0, frames=[1, 2])}
        def predictions():
            return [dict(head_endpoint=h, head_reason='trajectory_frame_orientation',
                         head_identity_confirmed=False, head_evidence_score=.4) for h in (0, 1, 0)]
        rows = predictions()
        constrain_trajectory_proposals(rows, links, [True, False, False])
        self.assertEqual([r['head_endpoint'] for r in rows], [0, 1, 1])
        self.assertEqual(rows[2]['head_trajectory_frames'], [1, 2])
        self.assertFalse(rows[2]['head_identity_confirmed'])
        rows = predictions()
        constrain_trajectory_proposals(rows, links, [False]*3)
        self.assertTrue(all(r['head_endpoint'] is None and r['head_temporal_conflict'] for r in rows))
