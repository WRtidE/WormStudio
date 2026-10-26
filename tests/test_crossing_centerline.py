"""Topology safety: reject ambiguous/hidden paths, recover observable crossing."""
import unittest
import cv2
import numpy as np
from crossing_centerline import resolve_sequence, sequence_choice, ordered_error
from centerline_topology import geometry_quality,resample


class CrossingCenterlineTests(unittest.TestCase):
    def test_distinct_paths_with_same_crossing_count_have_zero_margin_when_tied(self):
        t=np.linspace(-.3*np.pi,1.3*np.pi,101)
        a=np.column_stack([170+90*np.sin(t),130+65*np.sin(2*t)])
        b=a+[0,12]
        self.assertEqual(geometry_quality(a)['projection_crossings'],geometry_quality(b)['projection_crossings'])
        candidates=[dict(points=a,unary=0.),dict(points=b,unary=0.)]
        result=sequence_choice([candidates],500)[0]
        self.assertEqual(result['branch_margin'],0.)
        duplicate=sequence_choice([[dict(points=a,unary=0.),dict(points=a[::-1],unary=0.)]],500)[0]
        self.assertIsNone(duplicate['branch_margin'])

    def test_closed_ring_and_hidden_tip_never_enter_measurements(self):
        ring=np.zeros((240,300),np.uint8);cv2.circle(ring,(110,120),65,1,13)
        hidden=ring.copy();cv2.line(hidden,(175,120),(265,160),1,13)
        for mask in [ring,hidden]:
            points,quality,diagnostic=resolve_sequence([mask>0]*3)
            self.assertTrue(np.isnan(points).all())
            self.assertTrue(all(not q['centerline_valid'] for q in quality))

    def test_crossing_without_reliable_anchors_is_not_accepted(self):
        t=np.linspace(-.3*np.pi,1.3*np.pi,801)
        p=np.column_stack([170+90*np.sin(t),130+65*np.sin(2*t)])
        mask=np.zeros((260,340),np.uint8);cv2.polylines(mask,[np.rint(p).astype(np.int32)],False,1,11)
        points,quality,_=resolve_sequence([mask>0]*5)
        self.assertTrue(np.isnan(points).all())
        self.assertTrue(all('crossing_without_two_reliable_anchors' in q['quality_reasons'] for q in quality))

    def test_observable_crossing_sequence_keeps_ordered_ground_truth(self):
        masks=[];truth=[];t=np.linspace(-.3*np.pi,1.3*np.pi,801)
        for a in np.sin(np.linspace(0,np.pi,41))**2:
            p=np.column_stack([(1-a)*np.linspace(-120,120,len(t))+a*105*np.sin(t),80*np.sin(2*t)])
            p-=p.mean(0);p*=520/np.linalg.norm(np.diff(p,axis=0),axis=1).sum();p+=[210,210]
            mask=np.zeros((420,420),np.uint8)
            radius=np.maximum(1,6*np.sin(np.linspace(0,np.pi,len(t)))**.4)
            for xy,r in zip(p,radius):cv2.circle(mask,tuple(np.rint(xy).astype(int)),int(round(r)),1,-1)
            masks.append(mask>0);truth.append(resample(p))
        points,quality,_=resolve_sequence(masks)
        crossing=[]
        for f,q in enumerate(quality):
            if not q['centerline_valid']:
                self.assertTrue(np.isnan(points[f]).all());continue
            self.assertLess(ordered_error(points[f],truth[f]),5.)
            if q['topology']=='paired_contact':crossing.append(f)
        self.assertGreater(len(crossing),5)
        # A missing mask must break usable temporal evidence.
        validity=np.ones(41,bool);validity[5]=False;validity[35]=False
        points,quality,_=resolve_sequence(masks,validity)
        for f in crossing:
            self.assertFalse(quality[f]['centerline_valid'])
            self.assertTrue(np.isnan(points[f]).all())


if __name__=='__main__':unittest.main()
