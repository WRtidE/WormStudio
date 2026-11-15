import unittest

import numpy as np

from measurement_units import pixel_fields
from phenotype_statistics import behavior_statistics, measure_posture, trajectory_statistics
from result_metrics import present_report


class PostureStatisticsTests(unittest.TestCase):
    def measure(self, points, scale=1., **overrides):
        row = dict(mask_valid=True, centerline_valid=True)
        row.update(overrides)
        measure_posture(row, points, scale)
        return row

    def test_straight_and_curved_bodies_in_both_directions(self):
        points = np.column_stack((np.arange(101.), np.zeros(101)))
        straight = self.measure(points)
        self.assertEqual(straight['body_extension'], 1.)
        self.assertEqual(straight['midbody_abs_curvature_per_um'], 0.)
        t = np.linspace(0, np.pi / 2, 101)
        arc = 10 * np.column_stack((np.cos(t), np.sin(t)))
        for line in (arc, arc[::-1]):
            result = self.measure(line, scale=2.)
            self.assertAlmostEqual(result['body_extension'], np.sqrt(2) / (np.pi / 2), places=4)
            self.assertAlmostEqual(result['midbody_abs_curvature_per_um'], 1 / 20, places=5)

    def test_exclusion_uses_identity_geometry_and_uncertain_regions(self):
        points = np.column_stack((np.arange(101.), np.zeros(101)))
        for reason in (dict(identity_valid=False), dict(contact_bridge=True),
                       dict(uncertain_point_ranges='[[40,50]]')):
            row = self.measure(points, **reason)
            self.assertTrue(np.isnan(row['body_extension']))
            self.assertTrue(np.isnan(row['midbody_abs_curvature_per_um']))
        row = self.measure(points, endpoint_a_inferred=True, uncertain_point_ranges='[[0,5]]')
        self.assertTrue(np.isnan(row['body_extension']))
        self.assertEqual(row['midbody_abs_curvature_per_um'], 0.)

    def test_segment_weighted_straightness_and_time(self):
        points = np.array([[0,0],[2,0],[0,0],[200,0],[208,0],[999,0]])
        result = trajectory_statistics(points, np.array([0.,.1,.2,.5,.9,1.]),
            [np.array([0,1,2]), np.array([3,4]), np.array([5])])
        self.assertEqual(result['segment_net_displacement_sum_um'], 8.)
        self.assertEqual([s['net_displacement'] for s in result['trajectory_segments']], [0., 8.])
        self.assertEqual(result['path_valid_frames'], 5)
        self.assertAlmostEqual(result['path_valid_duration_s'], .6)
        self.assertEqual(result['path_length_um'], 12.)
        self.assertAlmostEqual(result['path_straightness'], 8 / 12)
        still = trajectory_statistics(points[[0,0]], np.array([0.,.1]), [np.arange(2)])
        self.assertEqual(still['path_length_um'], 0.)
        self.assertIsNone(still['path_straightness'])
        self.assertIsNone(trajectory_statistics(points, np.arange(6), [])['path_length_um'])

    def test_pixel_units_and_saved_coverage(self):
        row = pixel_fields(dict(worm_id=1, frames=6, median_midbody_abs_curvature_per_um=.001,
                                curvature_valid_frames=4, path_length_um=12., path_valid_frames=5,
                                path_valid_duration_s=.6))
        metrics = {m['key']: m for m in present_report({}, [row])['worms'][0]['metrics']}
        curvature = metrics['median_midbody_abs_curvature_per_px']
        self.assertEqual((curvature['value'], curvature['unit'], curvature['valid_frames']), (.001,'px⁻¹',4))
        self.assertEqual(metrics['path_length_px']['valid_duration_s'], .6)
        self.assertIsNone(metrics['median_body_extension']['value'])


class BehaviorStatisticsTests(unittest.TestCase):
    def row(self, state, segment=0, confirmed=True):
        return dict(movement_state=state, head_segment=segment, head_metrics_valid=confirmed,
                    head_identity_confirmed=confirmed)

    def test_durations_follow_timestamps_and_events_need_visible_onsets(self):
        rows = [self.row(state) for state in ('stationary','forward','stationary','forward','backward','backward')]
        times = np.array([0., .1, .3, .5, .7, 1.])
        stats = behavior_statistics(rows, times, [np.arange(6)])
        self.assertAlmostEqual(stats['activity_observed_s'], 1.)
        self.assertAlmostEqual(stats['stationary_duration_s'], .25)
        self.assertAlmostEqual(stats['moving_duration_s'], .75)
        self.assertAlmostEqual(stats['forward_duration_s'], .35)
        self.assertAlmostEqual(stats['backward_duration_s'], .4)
        self.assertEqual((stats['pause_onset_count'], stats['reversal_count']), (1, 1))
        pauses = [e for e in stats['behavior_events'] if e['event'] == 'pause']
        self.assertEqual(len(pauses), 2)
        self.assertTrue(pauses[0]['left_censored'])
        self.assertAlmostEqual(pauses[1]['duration_s'], .2)
        self.assertEqual((pauses[1]['start_frame'], pauses[1]['end_frame']), (2, 2))
        self.assertTrue(stats['behavior_events'][-1]['right_censored'])

    def test_boundaries_break_observations_and_direction_candidates_are_excluded(self):
        rows = [self.row('forward'), self.row('backward',1), self.row('unknown'),
                self.row('forward_candidate',confirmed=False), self.row('backward',2)]
        stats = behavior_statistics(rows, np.arange(5.), [np.arange(3),np.arange(3,5)])
        self.assertEqual(stats['activity_observed_s'], 2.)
        self.assertEqual(stats['stationary_duration_s'], 0.)
        self.assertEqual(stats['direction_observed_s'], 0.)
        self.assertIsNone(stats['reversal_count'])
        self.assertIsNone(stats['forward_duration_s'])
        self.assertEqual(stats['behavior_events'], [])
        rows = [self.row('forward'), self.row('forward'), self.row('backward'), self.row('backward')]
        stats = behavior_statistics(rows, np.arange(4.), [np.arange(2),np.arange(2,4)])
        self.assertEqual(stats['reversal_count'], 0)
        self.assertEqual(stats['backward_duration_s'], 1.)
