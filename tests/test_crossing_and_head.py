import sys
from pathlib import Path
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from body_sequence import select_body_sequence
from body_sequence_solver import solve, VERSION
from centerline_topology import prepare_topology, skeleton_graph_candidates
from elastic_curve import deformable_fit, static_pillars
from skeleton_geometry import resample


def _draw_worm_mask(shape, pts, radius=7):
    mask = np.zeros(shape, dtype=np.uint8)
    pts_i = np.rint(pts).astype(np.int32)
    cv2.polylines(mask, [pts_i], False, 1, thickness=max(3, int(round(2 * radius))))
    return mask.astype(bool)


def _smooth_worm_curve(t, amp_y=18.0, shift_x=0.0, shift_y=0.0):
    x = 40.0 + 180.0 * t + shift_x
    y = 90.0 + amp_y * np.sin(2.0 * np.pi * t) + shift_y
    return np.column_stack([x, y])


class CrossingAndHeadTests(unittest.TestCase):
    def test_skeleton_graph_candidates_handles_figure_eight_with_spur(self):
        # Construct an alpha self-crossing where both free tips extend past the crossing,
        # plus a side spur so branching topology has >2 endpoints.
        ts = np.linspace(-1.05 * np.pi, 1.05 * np.pi, 240)
        pts = np.column_stack([
            130.0 + 38.0 * np.cos(ts) + 32.0 * ts,
            110.0 + 42.0 * np.sin(ts),
        ])
        pts = resample(pts, 200)
        ref_len = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
        mask = _draw_worm_mask((240, 280), pts, radius=5.5)
        cv2.line(mask.view(np.uint8), (130, 68), (130, 44), 1, thickness=7)
        topo = prepare_topology(mask.astype(bool), prune=True)
        self.assertIsNotNone(topo)
        cands = skeleton_graph_candidates(topo, ref_len)
        self.assertGreater(len(cands), 0)
        best = cands[0]['points']
        length = float(np.linalg.norm(np.diff(best, axis=0), axis=1).sum())
        self.assertGreater(length, 0.72 * ref_len)
        self.assertLess(length, 1.18 * ref_len)

    def test_deformable_fit_respects_pillar_and_mask_cpu_and_mps(self):
        import torch

        ts = np.linspace(-0.35 * np.pi, 1.35 * np.pi, 160)
        arc = np.column_stack([120.0 + 38.0 * np.cos(ts), 110.0 + 38.0 * np.sin(ts)])
        ref_len = float(np.linalg.norm(np.diff(arc, axis=0), axis=1).sum())
        mask = _draw_worm_mask((220, 240), arc, radius=7)
        # Solid coiled contact blob around the bend so skeleton alone loses clear path.
        cv2.circle(mask.view(np.uint8), (120, 78), 18, 1, -1)
        pillar = np.array([[120.0, 110.0, 22.0]], dtype=np.float32)

        # Slightly perturbed initialization
        init_pts = resample(arc, 101) + np.array([4.0, -5.0])
        devices = ['cpu']
        if torch.backends.mps.is_available():
            devices.append('mps')
        for dev in devices:
            fitted = deformable_fit(mask, init_pts, ref_len, 7.0, pillars=pillar, device=dev, steps=60)
            self.assertIsNotNone(fitted, msg=f'deformable_fit failed on {dev}')
            self.assertEqual(fitted.shape, (101, 2))
            self.assertTrue(np.isfinite(fitted).all())
            fit_len = float(np.linalg.norm(np.diff(fitted, axis=0), axis=1).sum())
            self.assertAlmostEqual(fit_len / ref_len, 1.0, delta=0.18)
            pillar_dist = np.linalg.norm(fitted - pillar[0, :2], axis=1).min()
            self.assertGreater(pillar_dist, 17.5)

    def test_solve_bridges_coiled_crossing_and_preserves_head_continuity(self):
        # 9-frame smooth coiling sequence: frames 0-2 and 6-8 are clear tapered worms,
        # frames 3-5 bend smoothly through a solid coiled contact blob without teleporting.
        rng = np.random.default_rng(7)
        base_tex = rng.integers(140, 210, size=(200, 260), dtype=np.uint8)
        masks, gray = [], []
        radii_profile = np.linspace(9.5, 4.5, 101)  # clear anterior-posterior taper at start (end 0)
        s = np.linspace(0.0, 1.0, 101)
        curvatures = [0.25, 0.55, 0.95, 1.35, 1.50, 1.35, 0.95, 0.55, 0.25]
        arc_len = 165.0
        for f, k in enumerate(curvatures):
            theta = (s - 0.5) * (k * np.pi)
            ds = arc_len / 100.0
            dx = ds * np.cos(theta)
            dy = ds * np.sin(theta)
            xy = np.zeros((101, 2), dtype=float)
            xy[1:, 0] = np.cumsum(dx[:-1])
            xy[1:, 1] = np.cumsum(dy[:-1])
            xy -= xy.mean(axis=0)
            xy += np.array([130.0 + 1.5 * f, 105.0])
            mask = np.zeros((200, 260), dtype=np.uint8)
            for p, rad in zip(xy, radii_profile):
                cv2.circle(mask, (int(round(p[0])), int(round(p[1]))), int(round(rad)), 1, -1)
            if f in (3, 4, 5):
                # Touching/solid coil interior so naive framewise pose sees contact/branching.
                cv2.circle(mask, (int(round(xy[:, 0].mean())), int(round(xy[[0, -1], 1].mean()))), 15, 1, -1)
            masks.append(mask.astype(bool))
            img = base_tex.copy()
            img[mask > 0] = np.clip(img[mask > 0].astype(int) - 55, 20, 230).astype(np.uint8)
            gray.append(img)

        lines, facts, meta = solve(masks, gray, fps=12.0, auto_seed=True)
        self.assertEqual(meta['version'], VERSION)
        valid_count = sum(1 for f in facts if f is not None)
        self.assertEqual(valid_count, 9)
        components = {tuple(f['component']) for f in facts}
        self.assertEqual(components, {(0, 8)})
        self.assertTrue(all(f['head_known'] for f in facts))
        # Anterior (head at index 0) stays on the left throughout smooth bending.
        self.assertLess(lines[0, 0, 0], lines[0, -1, 0])
        self.assertLess(lines[8, 0, 0], lines[8, -1, 0])

    def test_select_body_sequence_handles_fast_tip_swing_without_dropping_flow(self):
        # Construct 3 frames where middle-to-last frame has a fast head swing across uniform background
        # so sparse LK at tip is unreliable, but body overlap stays consistent.
        shape = (180, 260)
        bg = np.full(shape, 180, dtype=np.uint8)
        ts = np.linspace(0.0, 1.0, 101)
        pts0 = _smooth_worm_curve(ts, amp_y=10.0)
        pts1 = _smooth_worm_curve(ts, amp_y=15.0)
        pts2 = _smooth_worm_curve(ts, amp_y=-14.0)
        pts2[:20, 1] -= np.linspace(18.0, 0.0, 20)
        frames = [
            [dict(points=resample(pts, 101), name='clear_observation', unary=0.1, endpoint_observed=[True, True])]
            for pts in (pts0, pts1, pts2)
        ]
        masks = [_draw_worm_mask(shape, pts, radius=6) for pts in (pts0, pts1, pts2)]
        gray = []
        for m in masks:
            im = bg.copy()
            im[m] = 90
            gray.append(im)
        selected, evidence = select_body_sequence(frames, gray, masks, radius=7.0, anchor=(0, pts0[0]))
        self.assertEqual(len(selected), 3)
        self.assertLess(np.linalg.norm(selected[2]['points'][-1] - pts2[-1]), 5.0)

    def test_static_pillars_ignores_uniform_frames(self):
        gray = [np.full((120, 120), 180, dtype=np.uint8) for _ in range(4)]
        self.assertEqual(len(static_pillars(gray, 8.0)), 0)

    def test_long_track_taper_consensus_tolerates_rare_outlier_while_short_stays_strict(self):
        from unittest.mock import patch
        from body_sequence_solver import reseed_clear_components

        path = np.column_stack((np.arange(101) + 10.0, np.full(101, 20.0)))
        mask = np.zeros((40, 125), dtype=bool)
        mask[15:25, 5:116] = True

        # 8-frame component with 7:1 consensus should reseed and flip endpoints cleanly.
        paths_long = np.stack([path] * 8)
        facts_long = [
            dict(head_known=False, anchor_conflict=False, candidate='clear_observation',
                 head_observed=True, tail_observed=True)
            for _ in paths_long
        ]
        with patch('body_sequence_solver.taper_side', side_effect=[1, 1, 1, 0, 1, 1, 1, 1]):
            reseed_clear_components(paths_long, facts_long, [mask] * 8, [(0, 7)], None)
        self.assertTrue(all(f['head_known'] for f in facts_long))
        np.testing.assert_array_equal(paths_long[0], path[::-1])

        # Short 4-frame component with 3:1 conflict must still be rejected.
        paths_short = np.stack([path] * 4)
        facts_short = [
            dict(head_known=False, anchor_conflict=False, candidate='clear_observation',
                 head_observed=True, tail_observed=True)
            for _ in paths_short
        ]
        with patch('body_sequence_solver.taper_side', side_effect=[1, 0, 1, 1]):
            reseed_clear_components(paths_short, facts_short, [mask] * 4, [(0, 3)], None)
        self.assertFalse(any(f['head_known'] for f in facts_short))

    def test_appearance_components_respects_excluded_frames_and_orients_valid_track(self):
        from body_sequence_solver import _recover_appearance_components

        n = 10
        path = np.column_stack((np.linspace(30.0, 210.0, 101), np.full(101, 60.0)))
        radii_profile = np.linspace(9.5, 4.5, 101)
        mask = np.zeros((120, 240), dtype=np.uint8)
        for p, rad in zip(path, radii_profile):
            cv2.circle(mask, (int(round(p[0])), int(round(p[1]))), int(round(rad)), 1, -1)
        masks = [mask.astype(bool) for _ in range(n)]
        bg = np.full((120, 240), 190, dtype=np.uint8)
        # Asymmetric dark anterior bulb on left (head) so appearance fingerprint is decisive.
        bg[masks[0]] = 110
        cv2.circle(bg, (38, 60), 7, 45, -1)
        gray = [bg.copy() for _ in range(n)]

        # Comp (0, 3) is known; (4, 6) is reversed & reachable; (8, 9) blocked by excluded frame 7.
        output = np.stack([path.copy() for _ in range(n)])
        for i in range(4, 7):
            output[i] = path[::-1].copy()
        facts = [
            dict(head_known=(i < 4), head_source=('initial_mark' if i < 4 else 'unresolved'),
                 anchor_conflict=False, candidate='clear_observation',
                 head_observed=True, tail_observed=True, component=([0, 3] if i < 4 else [4, 6] if i < 7 else [8, 9]))
            for i in range(n)
        ]
        facts[7] = None
        marks = [dict(frame=0, point=path[0].tolist(), human=True)]
        _recover_appearance_components(output, facts, masks, gray, marks, [(4, 6), (8, 9)], {7}, None)
        self.assertTrue(all(facts[i]['head_known'] for i in range(4, 7)))
        np.testing.assert_allclose(output[5, 0], path[0])
        self.assertFalse(facts[8]['head_known'])

    def test_biophysical_multi_cue_consensus_corrects_inverted_auto_seed_but_respects_manual(self):
        from body_sequence_solver import _verify_auto_seed_orientation

        # Construct a 22-frame forward-crawling worm whose true head (thicker end, leading motion,
        # stronger lateral swing) is stored at index -1 (inverted relative to frame-0 auto_taper_seed).
        n = 22
        radii_true = np.linspace(4.2, 9.2, 101)  # index -1 is blunt head
        lines, masks = [], []
        for f in range(n):
            xs = np.linspace(40.0 + 4.0 * f, 160.0 + 4.0 * f, 101)  # moving right (+x, led by index -1)
            ys = np.full(101, 70.0)
            # Anterior lateral foraging swing at index -1
            ys[-18:] += 6.0 * np.sin(1.2 * f) * np.linspace(0.0, 1.0, 18)
            pts = np.column_stack([xs, ys])
            mask = np.zeros((140, 300), dtype=np.uint8)
            for p, rad in zip(pts, radii_true):
                cv2.circle(mask, (int(round(p[0])), int(round(p[1]))), int(round(rad)), 1, -1)
            lines.append(pts)
            masks.append(mask.astype(bool))
        lines_auto = np.stack(lines)
        facts_auto = [
            dict(head_known=True, manual_lineage=False, anchor_conflict=False,
                 head_source='auto_taper_seed', candidate='clear_observation',
                 head_observed=True, tail_observed=True, component=[0, n - 1])
            for _ in range(n)
        ]
        audit = _verify_auto_seed_orientation(lines_auto, facts_auto, masks, None)
        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]['flipped'])
        self.assertEqual(facts_auto[0]['head_source'], 'biophysical_multi_cue_consensus')
        self.assertGreater(lines_auto[0, 0, 0], lines_auto[0, -1, 0])  # head (right end) is now index 0

        # Human manual_lineage tracks must never be auto-flipped.
        lines_manual = np.stack(lines)
        facts_manual = [
            dict(head_known=True, manual_lineage=True, anchor_conflict=False,
                 head_source='initial_mark', candidate='clear_observation',
                 head_observed=True, tail_observed=True, component=[0, n - 1])
            for _ in range(n)
        ]
        audit_m = _verify_auto_seed_orientation(lines_manual, facts_manual, masks, None)
        self.assertEqual(audit_m, [])
        self.assertLess(lines_manual[0, 0, 0], lines_manual[0, -1, 0])


if __name__ == '__main__':
    unittest.main()
