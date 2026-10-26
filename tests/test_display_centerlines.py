import unittest
import numpy as np
from display_centerlines import build_display,inside_fraction,supported_curve
from quality_schema import VERSION

class DisplayTests(unittest.TestCase):
    def row(self,frame=0):
        return dict(worm_id=1,frame=frame,source_valid=True,mask_valid=True,identity_valid=False,
                    centerline_valid=False,measurement_mode='pixels',quality_schema_version=VERSION)
    def test_identity_review_keeps_geometry_and_never_unlocks(self):
        mask=np.zeros((30,120),bool);mask[8:23,5:115]=True
        p=np.column_stack([np.linspace(6,113,101),np.full(101,15.)]);r=self.row()
        result=build_display([r],{(1,0):p},{},lambda w,f:mask,lambda m:None)
        self.assertIn((1,0),result);self.assertEqual(r['display_source'],'geometry_review')
        self.assertFalse(r['identity_valid']);self.assertFalse(r['centerline_valid'])
        self.assertEqual(inside_fraction(result[(1,0)],mask),1.)
    def test_current_mask_refit_and_empty_source(self):
        mask=np.ones((20,100),bool);p=np.column_stack([np.linspace(2,98,101),np.full(101,10.)])
        r=self.row();r['mask_valid']=False
        result=build_display([r],{}, {},lambda w,f:mask,lambda m:(p,None,None))
        self.assertEqual(r['display_source'],'mask_refit');self.assertFalse(r['mask_valid'])
        r=self.row();r['source_valid']=False
        self.assertFalse(build_display([r],{}, {},lambda w,f:mask,lambda m:(p,None,None)))
    def test_distant_or_cross_background_curve_is_rejected(self):
        mask=np.zeros((40,100),bool);mask[5:15,:]=True
        p=np.column_stack([np.linspace(0,99,101),np.full(101,30.)])
        self.assertIsNone(supported_curve(p,mask))
        # Endpoints inside is insufficient if the connecting segment crosses a hole.
        mask[:]=True;mask[:,45:55]=False
        self.assertIsNone(supported_curve(np.array([[5.,10.],[95.,10.]]),mask))

    def test_implausible_length_and_local_fold_rejected(self):
        mask=np.ones((200,200),bool)
        self.assertIsNone(supported_curve(np.array([[10.,10.],[12.,10.]]),mask))
        self.assertIsNone(supported_curve(np.array([[10.,10.],[180.,10.]]),mask,100.))
        fold=np.array([[10.,10.],[110.,10.],[10.,11.],[110.,11.]])
        self.assertIsNone(supported_curve(fold,mask))
        straight=np.column_stack([np.linspace(10,110,101),np.full(101,50.)])
        self.assertIsNotNone(supported_curve(straight,mask,100.))

if __name__=='__main__':unittest.main()
