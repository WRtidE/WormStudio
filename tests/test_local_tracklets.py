import copy
import unittest
from local_tracklets import measure
from quality_schema import exclude_unusable, VERSION
from frame_metrics import present_frame


def sequence(n=12):
    return [dict(frame=i,worm_id=1,time_s=i/12,time_valid=True,source_valid=True,mask_valid=True,
        pose_valid=True,identity_valid=i<4,identity_segment=0 if i<4 else 1,
        measurement_mode='pixels',quality_schema_version=VERSION,
        centroid_x_source_px=float(i if i<4 else 100+i),centroid_y_source_px=1.,
        length_px=100.,mean_width_px=10.,valid_for_summary=i<4) for i in range(n)]


class LocalTrackletTests(unittest.TestCase):
    def test_jump_does_not_create_speed_or_confirm_identity(self):
        rows=sequence();rows[4]['mask_valid']=False
        original=copy.deepcopy(rows);report=measure(rows,12)
        self.assertEqual(rows[5]['tracklet_status'],'stable_anonymous')
        self.assertIsNone(rows[4]['tracklet_speed_px_s'])
        self.assertIsNone(rows[5]['tracklet_speed_px_s'])
        self.assertEqual(rows[6]['tracklet_speed_px_s'],12.)
        self.assertNotEqual(rows[3]['tracklet_id'],rows[5]['tracklet_id'])
        self.assertEqual(report['tracklets'][1]['additional_pose_frames'],7)
        for old,new in zip(original,rows):
            self.assertEqual({k:new[k] for k in old},old)
            self.assertFalse(new['tracklet_identity_confirmed'])

    def test_source_missing_contact_and_boundary_break_runs(self):
        for field in ('source_valid','mask_valid','time_valid'):
            rows=sequence(18);rows[8][field]=False
            measure(rows,12)
            self.assertNotEqual(rows[7]['tracklet_id'],rows[9]['tracklet_id'])
            self.assertIsNone(rows[9]['tracklet_speed_px_s'])
            self.assertFalse(rows[8]['tracklet_pose_valid'])

    def test_missing_frame_breaks_even_if_adjacent_in_array(self):
        rows=sequence(18);del rows[8];measure(rows,12)
        self.assertNotEqual(rows[7]['tracklet_id'],rows[8]['tracklet_id'])
        self.assertIsNone(rows[8]['tracklet_speed_px_s'])

    def test_pose_failures_not_recovered_by_stability(self):
        rows=sequence();rows[8]['pose_valid']=False;measure(rows,12)
        self.assertFalse(rows[8]['tracklet_pose_valid'])
        self.assertIsNone(rows[8]['tracklet_length_px'])
        self.assertTrue(rows[8]['tracklet_motion_valid'])

    def test_short_chunks_fail_closed(self):
        rows=sequence(3);measure(rows,12)
        self.assertTrue(all(r['tracklet_status']=='short_candidate' for r in rows))
        self.assertTrue(all(r['tracklet_length_px'] is None for r in rows))

    def test_duration_threshold_scales_with_fps(self):
        for fps,minimum in [(12,4),(30,9)]:
            rows=sequence(12)
            for r in rows:r['identity_segment']=0;r['identity_valid']=False
            report=measure(rows[:minimum-1],fps)
            self.assertEqual(report['minimum_samples'],minimum)
            self.assertFalse(rows[0]['tracklet_pose_valid'])
            measure(rows[:minimum],fps)
            self.assertTrue(rows[0]['tracklet_pose_valid'])

    def test_global_gate_still_blanks_individual_fields(self):
        rows=sequence();measure(rows,12);r=rows[6];exclude_unusable(r)
        import math
        self.assertTrue(math.isnan(r['length_px']))
        self.assertEqual(r['tracklet_length_px'],100.)
        shown=present_frame(r)
        self.assertIsNone(shown['length_px'])
        self.assertEqual(shown['tracklet_length_px'],100.)
        self.assertFalse(shown['identity_valid'])

    def test_static_false_object_is_not_identity_evidence(self):
        rows=sequence()
        for r in rows:r['centroid_x_source_px']=20.
        measure(rows,12)
        self.assertEqual(rows[6]['tracklet_speed_px_s'],0.)
        self.assertFalse(rows[6]['identity_valid'])
        self.assertFalse(rows[6]['tracklet_identity_confirmed'])

    def test_objects_never_merged(self):
        rows=sequence();other=copy.deepcopy(rows)
        for r in other:r['worm_id']=2
        report=measure(rows+other,12)
        self.assertEqual(len(report['tracklets']),4)
        self.assertNotEqual(rows[6]['tracklet_id'],other[6]['tracklet_id'])

    def test_physical_inputs_export_explicit_pixels(self):
        rows=sequence()
        for r in rows:
            r['measurement_mode']='physical';r['length_um']=r.pop('length_px')*.5
            r['mean_width_um']=r.pop('mean_width_px')*.5
        measure(rows,12,.5)
        self.assertEqual(rows[6]['tracklet_length_px'],100.)

    def test_nonfinite_position_breaks(self):
        rows=sequence(18);rows[8]['centroid_x_source_px']=float('nan');measure(rows,12)
        self.assertEqual(rows[8]['tracklet_status'],'rejected')
        self.assertNotEqual(rows[7]['tracklet_id'],rows[9]['tracklet_id'])

if __name__=='__main__':unittest.main()
