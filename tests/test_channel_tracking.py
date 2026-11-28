import unittest
from unittest.mock import patch

import cv2
import numpy as np

from wormtracker.config import WormTrackerConfig
from wormtracker.core.grid import get_interpolated_grid
from wormtracker.core.grid_reference import ChannelGridReference
from wormtracker.core.tracker import ChannelTracker
from wormtracker.engine.mog2 import MOG2Engine


class ChannelTracking(unittest.TestCase):
    bounds = [(4, 44), (50, 90)]

    def test_grid_follows_measured_edges_for_different_heights_and_lane_counts(self):
        for lanes, height in [(1, 240), (2, 1080), (4, 480), (32, 720), (40, 360), (64, 240), (100, 240)]:
            with self.subTest(lanes=lanes, height=height):
                width = 280 + lanes * 42
                frame = np.full((height, width, 3), 170, np.uint8)
                expected = []
                for i in range(lanes):
                    # Vary positions and widths so equal subdivision cannot pass.
                    left = 120 + i * 42 + 2 * (i % 3 - 1)
                    right = left + 28 + i % 2
                    frame[:, left-1:left+2] = 78
                    frame[:, right-1:right+2] = 60
                    expected.append((left, right))
                # Strong chip-external structures on both sides of the search area.
                frame[:, :32:3] = 0
                frame[:, -32::3] = 0
                grid = get_interpolated_grid(frame, lanes, (40/width, 40/width), wall_ratio=.12)
                self.assertEqual(grid['grid_method'], 'peaks')
                np.testing.assert_allclose(grid['channel_bounds'], expected, atol=1)

    def test_grid_uses_the_requested_rows_and_falls_back_when_a_wall_is_missing(self):
        frame = np.full((480, 600, 3), 175, np.uint8)
        for left, right in [(80, 155), (190, 260), (295, 370), (409, 482)]:
            frame[48:96, left-1:left+2] = 55
            frame[48:96, right-1:right+2] = 55
        options = dict(crop_ratio=(.1, .2), wall_ratio=.12)
        grid = get_interpolated_grid(frame, 4, **options)
        np.testing.assert_allclose(grid['channel_bounds'], [(80,155),(190,260),(295,370),(409,482)], atol=1)
        frame[:, 250:305] = 175
        self.assertEqual(get_interpolated_grid(frame, 4, **options),
                         get_interpolated_grid(frame, 4, refine_walls=False, **options))

    def test_dense_lanes_use_the_channel_facing_edge_of_each_wall(self):
        for lanes, width, pitch in [(32, 640, 18), (100, 1920, 17)]:
            with self.subTest(lanes=lanes):
                frame = np.full((360, width, 3), 175, np.uint8)
                expected = []
                for i in range(lanes):
                    left = (width - lanes * pitch) // 2 + i * pitch
                    right = left + round(.65 * pitch) + i % 2
                    frame[:, left-1:left+2] = frame[:, right-1:right+2] = 60
                    expected.append((left, right))
                grid = get_interpolated_grid(frame, lanes, (0, 0), wall_ratio=.12)
                self.assertEqual(grid['grid_method'], 'peaks')
                np.testing.assert_allclose(grid['channel_bounds'], expected, atol=1)

    def events(self, observations):
        config = WormTrackerConfig(init_frame_index=0, tripwire_ratio=0.5,
                                   max_dist_y=60, cross_debounce=3)
        engine = MOG2Engine(config)
        frame = np.zeros((200, 100, 3), np.uint8)
        counts, cooldown, events = {1: 0, 2: 0}, {}, []
        with patch.object(engine, '_detect') as detect:
            for i, points in enumerate(observations, start=1):
                detect.return_value = (points, None, 0, None)
                result = engine.process_frame(frame, i, {'channel_bounds': self.bounds}, counts, cooldown)
                if not points:
                    self.assertFalse(result.active_worms)
                events.extend((i, e['channel'], e['track']) for e in result.crossing_events)
        self.assertEqual(sum(counts.values()), len(events))
        return events

    def test_short_gap_and_long_gap(self):
        prefix = [[(25, 135)], [(25, 120)]]
        self.assertEqual(self.events(prefix + [[], [], [(25, 77)]]), [(5, 2, 0)])
        self.assertFalse(self.events(prefix + [[], [], [], [(25, 77)]]))
        self.assertFalse(self.events([[(25, 135)], [], [(25, 77)]]))

    def test_lane_boundaries_and_line_jitter(self):
        self.assertFalse(self.events([[(42, 115)], [(51, 90)]]))
        self.assertFalse(self.events([[(48, 115)], [(48, 85)]]))
        ys = [115, 101, 99, 100, 99, 101, 99, 101, 98, 115, 96]
        self.assertEqual([f for f, _, _ in self.events([[(20, y)] for y in ys])], [3, 11])

    def test_two_following_worms_keep_ids_at_a_missed_detection(self):
        # The leader disappears exactly when its follower reaches its old position.
        points = [[(20, 125), (20, 153)], [(20, 111), (20, 139)],
                  [(20, 125)], [(20, 83), (20, 111)], [(20, 97)]]
        events = self.events(points)
        self.assertEqual(events, [(4, 2, 0), (5, 2, 1)])
        self.assertEqual(events, self.events([list(reversed(frame)) for frame in points]))

    def test_single_track_splitting_on_the_line_counts_both_departures(self):
        engine = MOG2Engine(WormTrackerConfig(init_frame_index=0, tripwire_ratio=.5, max_dist_y=60))
        frame = np.zeros((200, 100, 3), np.uint8)
        counts, cooldown, events = {1: 0, 2: 0}, {}, []
        observations = [
            [(145, 2700)], [(136, 2800)], [(125, 3200)], [(114, 2700)],
            [(120, 1350), (89, 1700)], [(114, 1400), (74, 1780)],
            [(104, 1550), (59, 1800)], [(92, 1800)],
        ]
        with patch.object(engine, '_detect') as detect:
            for i, bodies in enumerate(observations, 1):
                engine._areas = {(20, y): area for y, area in bodies}
                detect.return_value = (list(engine._areas), None, 0, None)
                r = engine.process_frame(frame, i, {'channel_bounds': self.bounds}, counts, cooldown)
                events.extend((i, e['track']) for e in r.crossing_events)
        self.assertEqual(events, [(5, 0), (8, 1)])

    def test_rejoined_fragments_continue_the_approaching_track(self):
        tracker = ChannelTracker(WormTrackerConfig())
        for frame, sizes in enumerate([
            {(23, 228): 818}, {(27, 200): 632}, {(31, 156): 736},
            {(30, 127): 390, (42, 76): 357}, {(33, 24): 1022},
        ], 1):
            tracks = tracker.update(list(sizes), self.bounds, frame, sizes)
        self.assertEqual(list(tracks), [0])
        self.assertEqual(list(tracks[0])[-2:], [(30, 127), (33, 24)])

    def test_fast_isolated_pair_needs_area_and_horizontal_agreement(self):
        cfg = WormTrackerConfig(max_dist_y=60, max_dist_x=20)
        for x, area, linked in [(24, 120, True), (38, 120, False), (24, 12, False)]:
            tracker = ChannelTracker(cfg)
            tracker.update([(20, 135)], self.bounds, 1, {(20, 135): 190})
            tracks = tracker.update([(x, 70)], self.bounds, 2, {(x, 70): area})
            self.assertEqual(0 in tracks, linked)

    def test_temporal_evidence_requires_observed_body_on_both_sides(self):
        cfg = WormTrackerConfig(init_frame_index=0, tripwire_ratio=.5, mask_top_ratio=0,
                                mask_bottom_ratio=1, min_area=70, max_dist_y=90)

        def replay(centers, primary):
            engine = MOG2Engine(cfg)
            counts, cooldown, events = {1: 0, 2: 0}, {}, []
            for i, (center, detected) in enumerate(zip(centers, primary), 1):
                frame = np.full((240, 100), 170, np.uint8)
                if center is not None:
                    cv2.ellipse(frame, (24, center), (5, 18), 0, 0, 360, 125, -1)
                engine._evidence.observe(frame, self.bounds, 0, 120, 0)
                points = [(24, center)] if detected else []
                engine._areas = {pt: 255 for pt in points}
                with patch.object(engine, '_detect', return_value=(points, None, 0, None)):
                    result = engine.process_frame(frame, i, {'channel_bounds': self.bounds}, counts, cooldown)
                events.extend(result.crossing_events)
            return [(e['channel'], e.get('marker')) for e in events]

        for observations, expected in [
            ([False, True, True], [(2, 'temporal_centroid')]),
            ([True, False, True], [(2, 'temporal_centroid')]),
            ([True, True, True], [(2, None)]),
        ]:
            self.assertEqual(replay([None]*6 + [150, 95, 65], [False]*6 + observations), expected)
        self.assertEqual(replay([None]*6 + [117, 60, None], [False]*6 + [True, True, False]),
                         [(2, 'lower_body')])
        for centers in ([80, 60], [123, 118], [None, 65]):
            self.assertEqual(replay([None]*6 + centers, [False]*6 + [False, True]), [])

    def test_detected_body_between_approach_and_dark_lobe_owns_the_motion(self):
        from wormtracker.core.crossing_evidence import Body, CrossingEvidence

        cfg = WormTrackerConfig()
        tracker = ChannelTracker(cfg)
        tracker.update([(24, 116)], self.bounds, 1, {(24, 116): 2200})
        evidence = CrossingEvidence(cfg)
        points = [(24, 102)]
        with patch.object(evidence, 'bodies', return_value=[Body(24, 34, 66, 800)]):
            added = evidence.upper_observations(tracker, points, self.bounds, 2, 100, {})
        self.assertFalse(added)
        self.assertEqual(points, [(24, 102)])

    def test_registration_follows_the_walls_with_a_moving_object(self):
        frame = np.full((160, 240, 3), 180, np.uint8)
        bounds = [(35, 60), (75, 100), (125, 150), (165, 190)]
        for left, right in bounds:
            frame[:, left-2:left+1] = 65
            frame[:, right:right+3] = 50
        grid = dict(roi_left=32, roi_right=193, channel_width=25,
                    wall_bounds=[(32, 35)], channel_bounds=bounds)
        ref = ChannelGridReference(frame, grid, .5)
        moved = np.roll(frame, 9, axis=1)
        cv2.ellipse(moved, (55, 85), (6, 16), 0, 0, 360, (30, 30, 30), -1)
        tracked = ref.locate(moved)
        self.assertEqual(tracked['channel_bounds'], [(l+9, r+9) for l, r in bounds])
        with self.assertRaises(RuntimeError):
            ref.locate(np.full_like(frame, 180))

    def test_fragment_geometry(self):
        mask = np.zeros((200, 100), np.uint8)
        for x, y, w, h in [(15, 70, 10, 26), (29, 73, 4, 18), (15, 106, 10, 25), (56, 73, 10, 25)]:
            cv2.rectangle(mask, (x, y), (x+w, y+h), 255, -1)
        engine = MOG2Engine(WormTrackerConfig(min_area=60))
        centers = sorted(engine._extract_centroids(mask, self.bounds))
        self.assertEqual(len(centers), 3)
        self.assertEqual(sorted(y for _, y in centers), [82, 85, 118])
