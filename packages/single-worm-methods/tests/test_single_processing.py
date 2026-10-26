"""Boundary/quality regressions for full-video temporal processing."""
import unittest
import numpy as np
from wormstudio_single.metrics import choose_anchors,make_windows,mask_speeds,pose_supported

class ProcessingChecks(unittest.TestCase):
    def test_all_frame_coverage_with_leading_trailing_uncertainty(self):
        valid=np.zeros(301,bool);valid[13:170]=True;valid[260:276]=True
        anchors=choose_anchors(valid);windows=make_windows(len(valid),anchors)
        observed=set(np.concatenate([ids for ids,_ in windows]).tolist())
        self.assertEqual(observed,set(range(301)))
        for ids,end in windows:
            self.assertTrue(valid[ids[0]])
            if end:self.assertTrue(valid[ids[-1]])
    def test_no_anchor_returns_no_invented_track(self):
        self.assertEqual(choose_anchors(np.zeros(20,bool)),[])
        self.assertEqual(make_windows(20,[]),[])
    def test_single_anchor_propagates_both_sides(self):
        windows=make_windows(9,[4])
        self.assertEqual(set(np.concatenate([x for x,_ in windows])),set(range(9)))
        self.assertTrue(all(not end for _,end in windows))
    def test_short_video(self):
        self.assertEqual(make_windows(1,[0])[0][0].tolist(),[0])
    def test_sustained_contact_has_immediate_boundary_anchors(self):
        valid=np.ones(150,bool);valid[70:80]=False
        anchors=choose_anchors(valid)
        self.assertIn(69,anchors);self.assertIn(80,anchors)
    def test_speed_does_not_bridge_invalid_mask(self):
        p=np.column_stack([np.arange(5)*2,np.zeros(5)])
        speed=mask_speeds(p,[True,True,False,True,True],30)
        np.testing.assert_allclose(speed[[0,1,3,4]],60)
        self.assertTrue(np.isnan(speed[2]))
    def test_geometry_alone_cannot_accept_self_contact(self):
        bq=dict(centerline_valid=True,mask_valid=True,topology='paired_contact')
        self.assertFalse(pose_supported(bq,dict(geometric_pass=True),0,520))
        bq['topology']='simple'
        self.assertTrue(pose_supported(bq,dict(geometric_pass=True),0,520))
        self.assertFalse(pose_supported(bq,dict(geometric_pass=True),30,520))
        bq['mask_valid']=False
        self.assertFalse(pose_supported(bq,dict(geometric_pass=True),0,520))

if __name__=='__main__':unittest.main()
