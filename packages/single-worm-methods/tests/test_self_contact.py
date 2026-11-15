import unittest

import cv2
import numpy as np
from wormstudio_single.self_contact import (
    choose_sequence,
    common_background,
    continued_order_stop,
    lifted_routes,
    loop_review_states,
    observe,
    reconstruct,
    review_interval,
    sweep,
)


def state(p):
    tangent = p[5:] - p[:-5]
    tangent /= np.linalg.norm(tangent, axis=1, keepdims=True)
    return {"p": p, "tangent": tangent, "profile": None, "cost": 0.0}


def open_pose():
    img = np.zeros((150, 150), np.uint8)
    cv2.line(img, (20, 74), (130, 74), 1, 7)
    p = np.column_stack([np.linspace(20, 130, 101), np.full(101, 74)])
    return observe(img), p


def ring(scale=1, reflect=False):
    img = np.zeros((140 * scale, 140 * scale), np.uint8)
    cv2.circle(img, (80 * scale, 70 * scale), 22 * scale, 1, 6 * scale)
    cv2.line(img, (58 * scale, 70 * scale), (18 * scale, 103 * scale), 1, 6 * scale)
    return observe(np.fliplr(img) if reflect else img)


class SelfContactTests(unittest.TestCase):
    def test_hidden_end_and_loop_do_not_depend_on_size_or_handedness(self):
        for scale, reflect in [(1, False), (2, True)]:
            f = ring(scale, reflect)
            centers = common_background([f, f, f], 7 * scale)
            self.assertEqual(len(centers), 1)
            for sign in [1, -1]:
                curves = lifted_routes(f, centers[0], sign * 2 * np.pi, 7 * scale)
                self.assertTrue(
                    any(
                        sum(visible) == 1 and sign * sweep(p, centers[0]) > 1.7 * np.pi
                        for p, visible, _ in curves
                    )
                )

    def test_display_route_follows_the_ring_with_a_hidden_tip(self):
        f = ring()
        center = common_background([f, f, f], 7)[0]
        curve = min(loop_review_states(f, center, 172, 7), key=lambda s: s["cost"])["p"]
        self.assertIsNotNone(curve)
        self.assertGreater(abs(sweep(curve, center)), 1.7 * np.pi)
        xy = np.rint(curve - f.offset).astype(int)
        self.assertTrue(f.mask[xy[:, 1], xy[:, 0]].all())

    def test_review_tracks_the_same_end_through_a_reversed_boundary(self):
        f = ring()
        center = common_background([f, f, f], 7)[0]
        candidates = loop_review_states(f, center, 172, 7)
        boundary = min(candidates, key=lambda state: state["cost"])["p"]
        curves = np.stack([boundary, np.full_like(boundary, np.nan), boundary[::-1]])
        path, reversed_right = review_interval([f] * 3, curves, 0, 2, center, 172, 7, None)
        self.assertTrue(reversed_right)
        np.testing.assert_allclose(path[1]["p"], boundary)

    def test_winding_preserved_across_angle_cut(self):
        # The final tangent of the moving arc crosses the polar coordinate cut.
        center = np.zeros(2)

        def arc(offset):
            a = np.linspace(-2.4, 3.13, 101) + offset
            return 25 * np.column_stack([np.cos(a), np.sin(a)])

        a, b, c = [arc(o) for o in [0.0, 0.025, 0.05]]
        selected, _, _ = choose_sequence(
            [[state(a)], [state(b[::-1]), state(b)], [state(c)]], 140, center
        )
        self.assertEqual(selected, [0, 1, 0])
        self.assertIsNone(choose_sequence([[state(a)], [state(b[::-1])]], 140, center))

    def test_temporal_tie_is_reported(self):
        _, p = open_pose()
        _, margins, _ = choose_sequence(
            [[state(p)], [state(p + [0, 13]), state(p - [0, 13])], [state(p)]],
            110,
            None,
        )
        self.assertAlmostEqual(margins[1], 0)

    def test_missing_centerline_without_an_enclosed_loop(self):
        f, p = open_pose()
        frames = [f] * 5
        curves = np.stack([p] * 5)
        curves[2] = np.nan
        result, _, status, events, _ = reconstruct(frames, curves, np.ones((5, 2)))
        self.assertEqual(events[0]["method"], "temporal")
        self.assertEqual(status[2], "inferred")
        self.assertTrue(np.isfinite(result[2]).all())
        self.assertLess(np.linalg.norm(result[2] - p, axis=1).mean(), 5)

    def test_single_candidate_cannot_force_a_body_reversal(self):
        _, p = open_pose()
        self.assertIsNone(choose_sequence([[state(p)], [state(p[::-1])]], 110, None))

    def test_protect_manual_point_and_skip_invalid_mask(self):
        f, p = open_pose()
        frames = [f, f, ring(), f, f, observe(np.zeros((140, 140))), ring(), f]
        _, visible, status, events, _ = reconstruct(
            frames,
            np.stack([p] * len(frames)),
            np.ones((len(frames), 2)),
            protected=[2],
        )
        self.assertEqual(events[0]["reason"], "manual_anchor_in_event")
        self.assertEqual(events[1]["reason"], "missing_boundary")
        self.assertEqual(status[5], "invalid_mask")
        self.assertFalse(visible[[2, 5, 6]].any())

    def test_two_separate_loops_require_review(self):
        img = np.zeros((150, 150), np.uint8)
        for center in [(50, 75), (95, 75)]:
            cv2.circle(img, center, 22, 1, 7)
        f, p = open_pose()
        frames = [f, f, observe(img), f, f]
        *_, events, _ = reconstruct(frames, np.stack([p] * 5), np.ones((5, 2)))
        self.assertEqual(events[0]["reason"], "multiple_persistent_loops")

    def test_order_propagation_stops_at_identity_change_or_unexplained_motion(self):
        _, p = open_pose()
        poses = np.stack([p] * 7)
        valid = np.array([True, True, False, True, True, True, True])
        segments = np.array([0, 0, 0, 0, 0, 1, 1])
        self.assertEqual(continued_order_stop(poses, valid, segments, 0, 110), 5)
        poses[3] = p[::-1]
        self.assertEqual(continued_order_stop(poses, valid, segments, 0, 110), 2)


if __name__ == "__main__":
    unittest.main()
