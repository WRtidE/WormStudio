import unittest
import numpy as np
from head_tail import detect_head_tail, endpoint_labels


def poses(n=25, moving_end=0):
    x=np.linspace(0,100,101)
    p=np.tile(np.column_stack([x,np.zeros(101)]),(n,1,1))
    for f in range(n):
        p[f,:,1]=12*np.sin(f*.65)*np.exp(-x/12)
    return p if moving_end==0 else p[:,::-1].copy()


class HeadTailTests(unittest.TestCase):
    def test_relative_motion_is_invariant_to_rigid_rotation_and_translation(self):
        p=poses();q=p.copy()
        for i in range(len(p)):
            a=.025*i;rotation=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
            q[i]=p[i]@rotation.T+[i*.3,i*.1]
        a=detect_head_tail(p,[True]*len(p));b=detect_head_tail(q,[True]*len(q))
        self.assertEqual([r['head_endpoint'] for r in a],[r['head_endpoint'] for r in b])
        np.testing.assert_allclose([r['head_evidence_score'] for r in a],[r['head_evidence_score'] for r in b],atol=1e-10)
        q[:]=p[0]
        for i in range(len(p)):
            a=.025*i;q[i]=q[i]@np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]]).T
        self.assertTrue(all(r['head_endpoint'] is None for r in detect_head_tail(q,[True]*len(q))))

    def test_appearance_recovers_short_segment_without_confirming_or_crossing_identity(self):
        p=poses(30);p[26:]=p[0];valid=np.ones(30,bool);valid[25]=False
        profile=np.linspace(-1,1,101)**3;profile=(profile-profile.mean())/profile.std()
        profiles=np.tile(profile,(30,1))
        r=detect_head_tail(p,valid,intensity_profiles=profiles)
        self.assertTrue(all(x['head_endpoint']==0 for x in r[26:]))
        self.assertEqual(r[26]['head_reason'],'appearance_temporal_reference')
        self.assertFalse(r[26]['head_identity_confirmed'])
        identities=np.zeros(30,int);identities[26:]=1
        r=detect_head_tail(p,valid,intensity_profiles=profiles,segment_ids=identities)
        self.assertTrue(all(x['head_endpoint'] is None for x in r[26:]))

    def test_appearance_conflict_abstains_and_manual_stays_authoritative(self):
        p=poses(51);valid=np.ones(51,bool);valid[25]=False
        profile=np.linspace(-1,1,101);profile/=profile.std()
        profiles=np.tile(profile,(51,1));profiles[26:]=profiles[26:,::-1]
        r=detect_head_tail(p,valid,intensity_profiles=profiles)
        self.assertEqual(r[0]['head_reason'],'appearance_motion_conflict')
        self.assertIsNone(r[0]['head_endpoint'])
        r=detect_head_tail(p,valid,intensity_profiles=profiles,manual=dict(frame=5,point=p[5,0].tolist()))
        self.assertTrue(r[0]['head_identity_confirmed'])
        profiles[:]=np.nan
        r=detect_head_tail(p,valid,intensity_profiles=profiles)
        self.assertEqual(r[0]['head_reason'],'relative_endpoint_bending')

    def test_mobile_end_with_input_order_flips(self):
        p=poses();p[1::2]=p[1::2,::-1]
        result=detect_head_tail(p,np.ones(len(p),bool))
        self.assertEqual([r['head_endpoint'] for r in result],[i%2 for i in range(len(p))])
        self.assertTrue(all(r['head_status']=='unconfirmed_relative_motion_candidate' for r in result))
        self.assertFalse(any(r['head_identity_confirmed'] for r in result))
        self.assertEqual(endpoint_labels(result[0]),('H?','T?'))

    def test_still_and_rigid_translation_do_not_guess(self):
        p=poses();p[:]=p[0]
        for offset in (np.zeros(len(p)),np.arange(len(p))):
            q=p.copy();q[:,:,0]+=offset[:,None]
            result=detect_head_tail(q,np.ones(len(p),bool))
            self.assertTrue(all(r['head_endpoint'] is None for r in result))

    def test_manual_anchor_at_later_frame_overrides_motion_both_directions(self):
        p=poses()
        result=detect_head_tail(p,np.ones(len(p),bool),manual=dict(frame=12,point=p[12,-1].tolist()))
        self.assertTrue(all(r['head_endpoint']==1 and r['head_identity_confirmed'] for r in result))
        self.assertEqual(endpoint_labels(result[0]),('H','T'))

    def test_gap_and_near_contact_break_manual_identity(self):
        p=poses();valid=np.ones(len(p),bool);valid[12]=False
        result=detect_head_tail(p,valid,manual=dict(frame=4,point=p[4,0].tolist()),mode='manual_only')
        self.assertTrue(all(r['head_identity_confirmed'] for r in result[:12]))
        self.assertTrue(all(r['head_endpoint'] is None for r in result[12:]))
        p[12,-1]=p[12,0]+1
        result=detect_head_tail(p,np.ones(len(p),bool))
        self.assertEqual(result[12]['head_reason'],'endpoints_too_close')
        self.assertIsNone(result[12]['head_endpoint'])
        self.assertNotEqual(result[11]['head_segment'],result[13]['head_segment'])

    def test_manual_unreliable_or_wrong_point_is_not_silently_confirmed(self):
        p=poses();valid=np.ones(len(p),bool);valid[8]=False
        result=detect_head_tail(p,valid,manual=dict(frame=8,point=[0,0]))
        self.assertFalse(any(r['head_identity_confirmed'] for r in result))
        result=detect_head_tail(p,np.ones(len(p),bool),manual=dict(frame=8,point=[50,0]))
        self.assertTrue(all(r['head_endpoint'] is None for r in result))
        self.assertEqual(result[0]['head_reason'],'manual_point_not_near_endpoint')

    def test_no_forward_motion_flip_and_no_geometry_edit(self):
        p=poses();p[:,:,0]+=np.arange(len(p))[:,None]*.1
        original=p.copy()
        result=detect_head_tail(p,np.ones(len(p),bool))
        self.assertTrue(all(r['head_endpoint']==0 for r in result))  # moving away from head is legitimate
        np.testing.assert_array_equal(p,original)

    def test_short_or_missing_segment_and_jumps(self):
        p=poses(4)
        self.assertTrue(all(r['head_endpoint'] is None for r in detect_head_tail(p,[True]*4)))
        p[:]=np.nan
        self.assertTrue(all(r['head_endpoint'] is None for r in detect_head_tail(p,[True]*4)))
        p=poses();p[12:,:,0]+=100
        result=detect_head_tail(p,[True]*len(p),manual=dict(frame=4,point=p[4,0].tolist()),mode='manual_only')
        self.assertTrue(result[11]['head_identity_confirmed'])
        self.assertIsNone(result[12]['head_endpoint'])

    def test_taper_still_worm_order_changes_and_manual_override(self):
        p=poses();p[:]=p[0]
        # Rounder left tip, sharp right tip; body radii are identical.
        r=np.ones((len(p),101))*8;r[:,:13]=5;r[:,-13:]=1
        p[1::2]=p[1::2,::-1];r[1::2]=r[1::2,::-1]
        result=detect_head_tail(p,[True]*len(p),radius_profiles=r)
        self.assertEqual([v['head_endpoint'] for v in result],[i%2 for i in range(len(p))])
        self.assertTrue(all(v['head_status']=='unconfirmed_taper_candidate' for v in result))
        self.assertFalse(any(v['head_identity_confirmed'] for v in result))
        self.assertEqual(endpoint_labels(result[0]),('H?','T?'))
        reviewed=detect_head_tail(p,[True]*len(p),radius_profiles=r,manual=dict(frame=4,point=p[4,-1].tolist()))
        self.assertTrue(all(v['head_identity_confirmed'] for v in reviewed))
        self.assertEqual(reviewed[0]['head_endpoint'],1)

    def test_shape_motion_conflict_and_shape_support(self):
        p=poses();r=np.ones((len(p),101))*8;r[:,:13]=1;r[:,-13:]=5
        result=detect_head_tail(p,[True]*len(p),radius_profiles=r)
        self.assertTrue(all(v['head_endpoint'] is None and v['head_reason']=='shape_motion_conflict' for v in result))
        r[2:]=np.nan
        result=detect_head_tail(p,[True]*len(p),radius_profiles=r)
        self.assertTrue(all(v['head_endpoint']==0 and v['head_reason']=='relative_endpoint_bending' for v in result))

    def test_symmetric_tips_do_not_invent_shape_head_or_cross_gaps(self):
        p=poses();p[:]=p[0];r=np.ones((len(p),101))*8
        self.assertTrue(all(v['head_endpoint'] is None for v in detect_head_tail(p,[True]*len(p),radius_profiles=r)))
        r[:,:13]=5;r[:,-13:]=1
        valid=np.ones(len(p),bool);valid[12]=False
        result=detect_head_tail(p,valid,radius_profiles=r,manual=dict(frame=4,point=p[4,-1].tolist()))
        self.assertTrue(result[0]['head_identity_confirmed'])
        self.assertFalse(result[13]['head_identity_confirmed'])
        self.assertEqual(result[13]['head_endpoint'],0)

    def test_present_candidate_does_not_unlock_head_metrics(self):
        from quality_schema import finish_dimensions,VERSION
        from frame_metrics import present_frame
        row=dict(worm_id=1,quality_schema_version=VERSION,source_valid=True,mask_valid=True,
                 centerline_valid=True,identity_valid=True,measurement_mode='pixels',
                 head_status='unconfirmed_taper_candidate',head_identity_confirmed=False,
                 head_metrics_valid=False,head_candidate_angle_smoothed_deg=20,
                 mask_bbox_crop_xyxy='[10,20,110,50]')
        finish_dimensions(row);view=present_frame(row)
        self.assertTrue(view['head_candidate']);self.assertFalse(view['head_confirmed'])
        self.assertIsNone(view['head_angle_deg'])
        self.assertEqual(view['quality']['dimensions']['head']['status'],'candidate')
        self.assertEqual(view['mask_bbox_crop_xyxy'],[10,20,110,50])
        row['mask_valid']=False
        self.assertIsNone(present_frame(row)['mask_bbox_crop_xyxy'])


if __name__=='__main__':unittest.main()
