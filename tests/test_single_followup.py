import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
from head_tail import detect_head_tail
from head_evidence import normalize_anchors
from test_head_tail import poses
from cache_integrity import snapshot,matches
from quality_audit import audit
from quality_schema import VERSION


class FollowupTests(unittest.TestCase):
    def test_multi_anchor_conflict_and_removal_preserve_geometry(self):
        p=poses();p[1::2]=p[1::2,::-1];before=p.copy()
        anchors=[dict(id='a',frame=0,point=p[0,0].tolist()),dict(id='b',frame=7,point=p[7,-1].tolist())]
        rows=detect_head_tail(p,[True]*len(p),anchors,'manual_only')
        self.assertTrue(all(r['head_identity_confirmed'] for r in rows))
        self.assertEqual([r['head_endpoint'] for r in rows],[i%2 for i in range(len(p))])
        anchors.append(dict(id='c',frame=9,point=p[9,0].tolist()))
        rows=detect_head_tail(p,[True]*len(p),anchors)
        self.assertTrue(all(r['head_anchor_conflict'] and not r['head_identity_confirmed'] for r in rows))
        self.assertTrue(all(r['head_endpoint'] is None for r in rows))
        rows=detect_head_tail(p,[True]*len(p),anchors[:-1],'manual_only')
        self.assertTrue(all(r['head_identity_confirmed'] for r in rows))
        np.testing.assert_array_equal(p,before)

    def test_multi_anchor_gap_and_invalid_anchor_are_isolated(self):
        p=poses();valid=np.ones(len(p),bool);valid[12]=False
        anchors=[dict(id='a',frame=3,point=p[3,0].tolist()),dict(id='b',frame=20,point=p[20,-1].tolist())]
        rows=detect_head_tail(p,valid,anchors,'manual_only')
        self.assertTrue(all(r['head_endpoint']==0 for r in rows[:12]))
        self.assertIsNone(rows[12]['head_endpoint'])
        self.assertTrue(all(r['head_endpoint']==1 for r in rows[13:]))
        anchors.append(dict(id='c',frame=22,point=[50,0]))
        rows=detect_head_tail(p,valid,anchors)
        self.assertTrue(all(r['head_identity_confirmed'] for r in rows[:12]))
        self.assertTrue(all(r['head_endpoint'] is None for r in rows[13:]))

    def test_legacy_anchor_and_duplicate_id(self):
        point=dict(frame=0,point=[1,2]);self.assertEqual(normalize_anchors(point)[0]['id'],'legacy-0-f0')
        self.assertNotIn('id',point)
        with self.assertRaises(ValueError):normalize_anchors([dict(point,id='same'),dict(point,id='same')])
        with self.assertRaises(ValueError):normalize_anchors(dict(frame=True,point=[1,2]))

    def test_cache_detects_finite_geometry_corruption_and_relocation(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);a=base/'a';b=base/'b';a.mkdir();b.mkdir()
            p=poses();np.save(a/'pose.npy',p);np.save(b/'pose.npy',p)
            stage={'outputs':[str(a/'pose.npy')]};record={'output_sha256':snapshot(stage,a)}
            self.assertTrue(matches({'outputs':[str(b/'pose.npy')]},b,record))
            p[3,30,0]+=2;np.save(a/'pose.npy',p) # shape and finite checks alone cannot detect this.
            self.assertFalse(matches(stage,a,record))
            (a/'pose.npy').unlink();self.assertFalse(matches(stage,a,record))

    def test_audit_separates_pose_identity_and_missing_source(self):
        rows=[]
        for i in range(6):
            rows.append(dict(frame=i,worm_id=1,quality_schema_version=VERSION,measurement_mode='pixels',
                source_valid=i!=2,mask_valid=True,pose_valid=i!=1,identity_valid=i<3,
                identity_status='jump_review' if i==3 else 'post_contact_or_jump_unverified' if i>3 else 'provisional_continuous',
                identity_evidence=json.dumps(dict(event_frame=3)) if i>=3 else '{}'))
        a=audit(rows)['worms']['1']
        self.assertEqual(a['geometric_pose_valid'],4);self.assertEqual(a['formal_posture_valid'],1)
        self.assertEqual(a['identity_blocked_geometry'],3);self.assertEqual(a['identity_blocked_ranges'],[[3,5]])
        self.assertEqual([t['frame'] for t in a['triggers']],[3])

    def test_audit_keeps_interleaved_worms_and_legacy_records_separate(self):
        common = dict(quality_schema_version=VERSION, measurement_mode='pixels', source_valid=True,
            mask_valid=True, pose_valid=True, identity_valid=False, identity_status='jump_review',
            identity_evidence={'event_frame': 1}, tracklet_pose_valid=True, tracklet_motion_valid=True)
        rows = [dict(common, worm_id='7', frame='3'),
                dict(worm_id='2', frame='0', mask_valid='True', centerline_valid='True'),
                dict(common, worm_id=7, frame=1),
                dict(common, worm_id=7, frame=2, identity_valid=True, identity_status='auto_reassociated',
                     identity_link_source='auto_reassociated', identity_human_confirmed=True,
                     head_identity_confirmed=True, head_anchor_conflict=True)]
        before = json.dumps(rows)
        result = audit(rows)['worms']
        self.assertEqual(list(result), ['2', '7'])
        legacy = result['2']
        self.assertEqual((legacy['source_valid'], legacy['mask_valid'], legacy['formal_posture_valid']), (0, 1, 1))
        current = result['7']
        self.assertEqual((current['frames'], current['geometric_pose_valid'], current['formal_posture_valid']), (3, 3, 1))
        self.assertEqual(current['identity_blocked_geometry'], 2)
        self.assertEqual(current['identity_blocked_ranges'], [[1, 1], [3, 3]])
        self.assertEqual(current['formal_posture_rejected_ranges'], [[1, 1], [3, 3]])
        self.assertEqual(current['tracklet_pose_valid'], 3)
        self.assertEqual(current['tracklet_additional_pose'], 2)
        self.assertEqual(current['tracklet_motion_valid'], 3)
        self.assertEqual(current['identity_auto_reassociated'], 1)
        self.assertEqual(current['identity_human_confirmed'], 1)
        self.assertEqual((current['head_confirmed'], current['head_conflict_frames']), (1, 1))
        self.assertEqual(current['identity_status_counts'], {'jump_review': 2, 'auto_reassociated': 1})
        self.assertEqual(current['triggers'], [dict(frame=1, status='jump_review', evidence={'event_frame': 1})])
        self.assertEqual(json.dumps(rows), before)


if __name__=='__main__':unittest.main()
