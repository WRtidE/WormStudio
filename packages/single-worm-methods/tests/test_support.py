"""Auxiliary queries must not change primary indexing or cache dimensions."""
import unittest
import numpy as np
from wormstudio_single.support import support_points
from wormstudio_single.inference import track


class FakeTensor:
    def __init__(self, value): self.value=np.asarray(value)
    def __getitem__(self, key): return FakeTensor(self.value[key])
    def detach(self): return self
    def cpu(self): return self
    def float(self): return self
    def numpy(self): return self.value


class FakeTracker:
    MODEL_SIZE=256
    def __init__(self): self._model=self; self.hook=None; self.calls=[]
    def register_forward_hook(self, hook): self.hook=hook; return self
    def remove(self): self.hook=None
    def track_frame(self, frame, points, state, autocast=True):
        self.calls.append((points,state))
        if state is None: self.points=np.asarray(points)
        step=0 if state is None else state
        n=len(self.points)
        if self.hook:
            logits=np.zeros((1,1,n,512));logits[0,0,:,10]=np.arange(n)
            self.hook(self,None,(None,FakeTensor(logits),FakeTensor(np.arange(n).reshape(1,1,n,1))))
        return self.points+step,np.ones(n,bool),step+1


class SupportTests(unittest.TestCase):
    def test_local_grid_radius_and_determinism(self):
        points=np.array([[100.,60.],[300.,180.]])
        mask=np.ones((256,512),bool)
        support,parents=support_points(points,mask,8)
        self.assertEqual(support.shape,(16,2))
        self.assertTrue(np.all(abs((support-points[parents])/[2.,1.])<=32.001))
        repeated,_=support_points(points,mask,8)
        np.testing.assert_array_equal(support,repeated)

    def test_body_mode_only_foreground_and_no_primary_duplicates(self):
        mask=np.zeros((128,256),bool);mask[60:69,20:230]=True
        points=np.column_stack([np.arange(30,221,20),np.full(10,64.)])
        support,parents=support_points(points,mask,16,'body')
        self.assertGreater(len(support),0)
        self.assertLessEqual(len(support),160)
        xy=np.rint(support).astype(int)
        self.assertTrue(mask[xy[:,1],xy[:,0]].all())
        all_points=np.concatenate([points,support])/[1.,.5]
        distances=np.sum((all_points[:,None]-all_points[None,:])**2,axis=2)
        np.fill_diagonal(distances,np.inf)
        self.assertGreaterEqual(distances.min(),.75**2-1e-4)
        self.assertTrue(np.all(abs((support-points[parents])/[1.,.5])<=32.001))

    def test_empty_mask_and_zero_queries(self):
        for count,mode in [(0,'local'),(16,'body')]:
            support,parents=support_points(np.array([[0.,0.]]),np.zeros((10,10)),count,mode)
            self.assertEqual(support.shape,(0,2));self.assertEqual(parents.shape,(0,))
        for count in (-1,65,1.5,True):
            with self.assertRaises(ValueError):support_points([[1,1]],np.ones((10,10)),count)

    def test_support_remains_in_recurrence_but_not_primary_outputs(self):
        model=FakeTracker();frames=np.zeros((3,40,60,3),np.uint8)
        primary=np.array([[10.,12.],[20.,22.]])
        support=np.array([[30.,32.],[35.,33.],[36.,34.]])
        diagnostic={}
        p,v,heads=track(model,frames,primary,True,support,diagnostic)
        self.assertEqual(len(model.calls[0][0]),5)
        self.assertTrue(all(points is None and state is not None for points,state in model.calls[1:]))
        np.testing.assert_array_equal(p,primary[None]+np.arange(3)[:,None,None])
        self.assertEqual(heads['coordinate_probabilities'].shape,(3,2,2,256))
        np.testing.assert_array_equal(heads['visibility_logits'][0],[0,1])
        self.assertEqual(diagnostic['support_tracks'].shape,(3,3,2))
        self.assertIsNone(model.hook)
        legacy=track(FakeTracker(),frames,primary,True)
        np.testing.assert_array_equal(p,legacy[0])
        for key in heads:np.testing.assert_array_equal(heads[key],legacy[2][key])


if __name__=='__main__':unittest.main()
