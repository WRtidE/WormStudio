import unittest
import numpy as np
from head_tail.head_tail import detect_head_tail


class InitializationTest(unittest.TestCase):
    def fixture(self,n=20):
        p=np.tile(np.column_stack([np.linspace(0,100,101),np.zeros(101)]),(n,1,1))
        radius=np.full((n,101),10.);radius[:,:13]=7;radius[:,-13:]=2
        return p,np.ones(n,bool),radius

    def detect(self,p,valid,radius,**kw):
        return detect_head_tail(p,valid,radius_profiles=radius,fps=12.,mode=kw.pop('mode','taper_seed'),**kw)

    def test_initial_shape_is_frozen_despite_later_shape_reversal(self):
        p,v,r=self.fixture();r[7:]=r[7:,::-1]
        rows=self.detect(p,v,r)
        self.assertTrue(all(x['head_endpoint']==0 for x in rows))
        self.assertTrue(all(not x['head_identity_confirmed'] for x in rows))

    def test_skeleton_reversal_preserves_physical_head(self):
        p,v,r=self.fixture();p[1::2]=p[1::2,::-1];r[1::2]=r[1::2,::-1]
        rows=self.detect(p,v,r)
        self.assertEqual([x['head_endpoint'] for x in rows],[i%2 for i in range(len(p))])

    def test_user_overrides_shape_and_short_gap_is_candidate_only(self):
        p,v,r=self.fixture();v[8]=False
        rows=self.detect(p,v,r,manual={'frame':0,'point':[100,0]})
        self.assertEqual(rows[0]['head_endpoint'],1);self.assertTrue(rows[0]['head_identity_confirmed'])
        self.assertIsNone(rows[8]['head_endpoint'])
        self.assertEqual(rows[9]['head_endpoint'],1);self.assertFalse(rows[9]['head_identity_confirmed'])
        self.assertEqual(rows[9]['head_reason'],'manual_seed_propagated')

    def test_manual_only_long_gap_identity_and_conflicts(self):
        p,v,r=self.fixture();v[5:10]=False
        rows=self.detect(p,v,r,mode='manual_seed',manual={'frame':0,'point':[0,0]})
        self.assertTrue(all(x['head_endpoint'] is None for x in rows[10:]))
        ids=np.r_[np.zeros(10,int),np.ones(10,int)]
        rows=self.detect(p,np.ones(20,bool),r,mode='manual_seed',manual={'frame':0,'point':[0,0]},segment_ids=ids)
        self.assertTrue(all(x['head_endpoint'] is None for x in rows[10:]))
        rows=self.detect(p,np.ones(20,bool),r,manual=[{'frame':0,'point':[0,0]},{'frame':2,'point':[100,0]}])
        self.assertTrue(all(x['head_endpoint'] is None for x in rows))

    def test_insufficient_shape_and_invalid_manual_do_not_fall_back(self):
        p,v,r=self.fixture();r[:]=10
        rows=self.detect(p,v,r);self.assertTrue(all(x['head_endpoint'] is None for x in rows))
        p,v,r=self.fixture();v[0]=False
        rows=self.detect(p,v,r,manual={'frame':0,'point':[0,0]})
        self.assertTrue(all(x['head_endpoint'] is None for x in rows))

    def test_visible_tip_carries_both_labels_through_contact(self):
        poses, valid, radius = self.fixture(4)
        # One terminal portion folds into the body, then emerges. Reverse the
        # stored vertex order on both sides of the contact boundary.
        poses[2, 60:, 1] = poses[2, 60:, 0] - 60
        poses[2, 60:, 0] = 60
        poses[1:3] = poses[1:3, ::-1]
        visible = np.ones((4, 2), bool)
        visible[2, 0] = False
        for head in (0, 1):
            options = dict(mode='manual_seed', manual={'frame': 0, 'point': [100 * head, 0]})
            rows = self.detect(poses, valid, radius, endpoint_visible=visible, **options)
            self.assertEqual([r['head_endpoint'] for r in rows], [head, 1-head, 1-head, head])
            self.assertEqual(rows[2]['head_temporal_link_source'], 'visible_tip')
            self.assertFalse(rows[2]['head_identity_confirmed'])
            baseline = self.detect(poses, valid, radius, **options)
            self.assertIsNone(baseline[2]['head_endpoint'])

        for change in ('hidden', 'different_tip', 'identity_break', 'gap', 'no_seed'):
            with self.subTest(change=change):
                v, flags, ids = valid.copy(), visible.copy(), np.zeros(4, int)
                anchor = {'frame': 0, 'point': [0, 0]}
                if change == 'hidden': flags[2] = False
                if change == 'different_tip': flags[1, 1] = False
                if change == 'identity_break': ids[2:] = 1
                if change == 'gap': v[1] = False
                if change == 'no_seed': anchor = None
                rows = self.detect(poses, v, radius, mode='manual_seed', manual=anchor,
                                   endpoint_visible=flags, segment_ids=ids)
                self.assertIsNone(rows[2]['head_endpoint'])

    def test_bad_anchor_elsewhere_does_not_cancel_valid_user_input(self):
        p,v,r=self.fixture();v[5:10]=False
        rows=self.detect(p,v,r,manual=[{'frame':0,'point':[0,0]},
                                      {'frame':7,'point':[0,0]},{'frame':10,'point':[0,0]}])
        self.assertTrue(rows[0]['head_identity_confirmed'])
        self.assertTrue(rows[10]['head_identity_confirmed'])
        self.assertIsNone(rows[7]['head_endpoint'])
        rows=self.detect(p,np.ones(20,bool),r,manual={'frame':0,'point':[50,0]})
        self.assertTrue(all(x['head_endpoint'] is None for x in rows))


if __name__=='__main__':unittest.main()
