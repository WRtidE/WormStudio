import unittest
import numpy as np
from head_point_tracks import make_queries,associate,reconcile


class PointGroupsTest(unittest.TestCase):
    def setUp(self):
        self.mask=np.zeros((130,130),bool);self.mask[54:67,10:111]=True
        self.curve=np.column_stack([np.linspace(10,110,101),np.full(101,60.)])
        self.p,self.g=make_queries(self.curve,self.mask)

    def assign(self,p,curve,mask,logits=None):
        return associate(p,self.g,np.full(len(p),9.) if logits is None else logits,
                         np.zeros((len(p),2)),curve,mask)

    def test_rotation_and_reversed_skeleton_keep_query_identity(self):
        for angle in (0,np.pi/2,np.pi):
            r=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
            p=(self.p-60)@r.T+60;c=(self.curve-60)@r.T+60
            mask=np.ones_like(self.mask)
            self.assertEqual(self.assign(p,c,mask)['endpoint'],0)
            self.assertEqual(self.assign(p,c[::-1],mask)['endpoint'],1)

    def test_visibility_and_background_abstain(self):
        logits=np.full(len(self.p),9.);logits[self.g==1]=-9
        self.assertIsNone(self.assign(self.p,self.curve,self.mask,logits)['endpoint'])
        self.assertIsNone(self.assign(self.p+[0,30],self.curve,self.mask)['endpoint'])

    def test_merged_groups_do_not_force_identity(self):
        p=self.p.copy();p[self.g==1]=p[self.g==1]-[85,0]
        self.assertIsNone(self.assign(p,self.curve,self.mask)['endpoint'])

    def test_reverse_alignment_disagreement_and_gate(self):
        left=[dict(endpoint=0)]*7;right=[dict(endpoint=1)]*5+[dict(endpoint=0),dict(endpoint=1)]
        a,rows=reconcile(left,right,[True]*6+[False])
        self.assertTrue(a['accepted']);self.assertEqual(a['parity'],1)
        self.assertEqual(rows[0]['endpoint'],0);self.assertIsNone(rows[5]['endpoint'])
        self.assertIsNone(rows[6]['endpoint']);self.assertFalse(any(r['head_identity_confirmed'] for r in rows))
        a,rows=reconcile(left[:4],right[:4],[True]*4)
        self.assertFalse(a['accepted']);self.assertTrue(all(r['endpoint'] is None for r in rows))


if __name__=='__main__':unittest.main()
