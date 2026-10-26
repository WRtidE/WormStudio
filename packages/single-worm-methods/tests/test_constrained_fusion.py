"""Regression tests for point drift, rejected geometry and explicit abstention."""
import unittest,json
import numpy as np,cv2
from wormstudio_single.fusion import fit,feasible_quality
from wormstudio_single.quality import INDICES
from wormstudio_single.skeleton import extract_niu

def fixture(n=3):
    mask=np.zeros((100,220),np.uint8);cv2.line(mask,(25,50),(195,50),1,17);mask=mask.astype(bool)
    p=extract_niu(mask)['ordered'];length=np.linalg.norm(np.diff(p,axis=0),axis=1).sum()
    # A completely wrong but high-visibility point observation.
    points=p[INDICES]+[0,-35];tracks=np.repeat(points[None],n,axis=0)
    d=dict(first=p,last=p,masks=np.repeat(mask[None],n,axis=0),frame_ids=np.arange(n),mask_valid=np.ones(n,bool),reference_length=length)
    z=dict(forward=tracks,backward=tracks,forward_visible=np.ones((n,21),bool),backward_visible=np.ones((n,21),bool))
    return d,z

class Constraints(unittest.TestCase):
    def test_confident_drift_cannot_replace_clear_image_path(self):
        d,z=fixture();p,q=fit(d,z)
        self.assertTrue(np.isfinite(p).all());self.assertTrue(all(x['geometric_pass'] for x in q))
        self.assertTrue(np.all(abs(p[:,:,1]-50)<2))
    def test_invalid_mask_creates_gap_not_interpolation(self):
        d,z=fixture();d['mask_valid'][1]=False;p,q=fit(d,z)
        self.assertTrue(np.isnan(p[1]).all());self.assertTrue(np.isfinite(p[[0,2]]).all())
        self.assertFalse(q[1]['geometric_pass'])
    def test_empty_masks_remain_empty(self):
        d,z=fixture();d['masks'][:]=False;p,q=fit(d,z)
        self.assertTrue(np.isnan(p).all())
    def test_one_frame(self):
        d,z=fixture(1);p,q=fit(d,z);self.assertEqual(p.shape,(1,101,2));self.assertTrue(q[0]['geometric_pass'])
    def test_collapsed_track_is_rejected_without_crashing(self):
        d,z=fixture();q=feasible_quality(np.full((101,2),50.),d['masks'][0],d['reference_length'])
        self.assertFalse(q['geometric_pass'])
if __name__=='__main__':unittest.main()
