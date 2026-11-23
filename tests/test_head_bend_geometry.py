import unittest

import cv2
import numpy as np

from head_bend_geometry import local_angle, supported_angles, turn


class HeadBendGeometryTests(unittest.TestCase):
    def test_silhouette_recovers_kink_without_moving_an_exact_chord(self):
        x=np.arange(101)
        line=np.column_stack((220.-2*x,np.full(101,90.)))
        line[:10]=line[10]+(10-x[:10,None])*np.sqrt(2)*np.array([1.,1.])
        tangent=np.gradient(line,axis=0)
        normal=np.column_stack((-tangent[:,1],tangent[:,0]))/np.linalg.norm(tangent,axis=1)[:,None]
        width=8*np.sin(np.pi*x/100)**.56
        edges=np.vstack((line+width[:,None]*normal,(line-width[:,None]*normal)[::-1]))
        mask=np.zeros((170,260),np.uint8)
        cv2.fillPoly(mask,[np.rint(edges*16).astype(np.int32)],1,shift=4)
        for shift in (0,1.5):
            fit=line.copy()
            fit[10,1]+=shift
            angle=turn(fit[0]-fit[10],fit[10]-fit[30])
            geometry=local_angle(fit,mask)
            self.assertIsNotNone(geometry)
            values,methods=supported_angles(np.full(5,angle),np.tile(geometry,(5,1)),np.zeros(5),np.arange(5.)/12)
            if shift:
                self.assertLess(abs(values[2]-45),abs(angle-45))
                self.assertEqual(methods[2],'section_fit')
            else:
                self.assertTrue(np.allclose(values,45))
                self.assertTrue((methods=='centerline_chords').all())
        self.assertIsNone(local_angle(line,np.zeros_like(mask)))

    def test_single_frame_spike_missing_data_and_block_boundaries(self):
        raw=np.array([20.,27.,20.,np.nan,20.,20.,20.])
        geometry=np.array([[20,20,4,20],[52,51,4,51],[20,20,4,20],[40,40,4,40],
                           [30,30,4,30],[30,30,4,30],[30,30,4,30]])
        blocks=np.array([0,0,0,0,1,1,2])
        values,methods=supported_angles(raw,geometry,blocks,np.arange(7.)/12)
        np.testing.assert_allclose(values,raw,equal_nan=True)
        self.assertFalse((methods=='section_fit').any())

    def test_persistent_conflict_rejects_the_whole_interval(self):
        geometry=np.array([[20,20,4,20],[20,38,4,20],[20,38,4,20],[20,38,4,20],[20,20,4,20]])
        values,methods=supported_angles(np.full(5,20.),geometry,np.zeros(5),np.arange(5.))
        self.assertEqual(list(methods),['centerline_chords']+['review_geometry']*3+['centerline_chords'])
        self.assertTrue(np.isnan(values[1:4]).all())

    def test_landmark_chord_limits_slope_change_on_a_smooth_neck(self):
        geometry=np.tile([41.,42.,4.,35.],(3,1))
        values,_=supported_angles(np.full(3,34.),geometry,np.zeros(3),np.arange(3.))
        self.assertEqual(values[1],37.)
        geometry[:,3]=33.
        values,methods=supported_angles(np.full(3,34.),geometry,np.zeros(3),np.arange(3.))
        self.assertEqual((values[1],methods[1]),(34.,'centerline_chords'))

    def test_unobserved_cross_sections_and_curved_segments_keep_the_chord(self):
        t=np.linspace(0,np.pi,11)
        head=np.column_stack((90+12*np.cos(t),90+12*np.sin(t)))
        neck=head[-1]-np.column_stack((np.arange(1,91)*2,np.zeros(90)))
        self.assertIsNone(local_angle(np.vstack((head,neck)),np.ones((180,180),bool)))
