import unittest
import numpy as np
from test_feedback_local import evidence
from wormstudio_single.feedback_image import ImagePolicy,image_field,point_evidence,image_plan,image_judge

def scene():
    truth,before,tr=evidence();frames=np.full(truth.shape+(3,),150,np.uint8);frames[truth]=70
    return frames,truth,before,tr

class ImageFeedbackTests(unittest.TestCase):
    def test_budget_excludes_frames_without_prompt_evidence(self):
        frames,truth,before,tr=scene();p=image_plan(frames,before,tr)
        self.assertEqual((p['start'],p['stop']),(3,8))
        self.assertLess(p['stop']-p['start'],p['propagation_budget_before_image'][1]-p['propagation_budget_before_image'][0])
    def test_true_undersegmentation_keeps_proposal(self):
        frames,truth,before,tr=scene();p=image_plan(frames,before,tr);self.assertEqual(p['status'],'proposed')
        self.assertTrue(image_judge(frames,before,truth,tr,p)['accepted'])
    def test_background_drift_stops_before_sam(self):
        frames,truth,before,tr=scene();frames[:]=150;frames[before]=70
        self.assertEqual(image_plan(frames,before,tr)['status'],'no_trigger')
    def test_light_foreground_also_supported(self):
        frames,truth,before,tr=scene();frames=255-frames
        self.assertEqual(image_plan(frames,before,tr)['status'],'proposed')
    def test_low_contrast_abstains(self):
        frames,truth,before,tr=scene();frames[:]=150;frames[truth]=148
        self.assertEqual(image_plan(frames,before,tr)['status'],'no_trigger')
    def test_unknown_is_not_called_background(self):
        image=np.full((96,96,3),150,np.uint8);mask=np.zeros((96,96),bool);mask[30:60,30:60]=True
        f=image_field(image,mask);supported,rows=point_evidence(f,mask,np.array([[65,45]]))
        self.assertFalse(supported[0]);self.assertEqual(rows[0]['label'],'unknown_contrast')
    def test_dark_other_object_across_background_not_same_body(self):
        image=np.full((96,96,3),150,np.uint8);mask=np.zeros((96,96),bool);mask[25:65,20:40]=True;image[mask]=70;image[30:60,48:60]=70
        support,rows=point_evidence(image_field(image,mask),mask,np.array([[53.,45.]]))
        self.assertFalse(support[0]);self.assertEqual(rows[0]['label'],'edge_crossing_or_other_object')
    def test_heldout_never_changes_image_prompts(self):
        frames,truth,before,tr=scene();p=image_plan(frames,before,tr)
        tr['queries'][::3]=[2,2];tr['forward'][:,::3]=[2,2];tr['backward'][:,::3]=[2,2]
        other=image_plan(frames,before,tr);self.assertEqual(p['points'],other['points']);self.assertEqual(p['frame'],other['frame'])
    def test_added_background_vetoes_candidate(self):
        frames,truth,before,tr=scene();p=image_plan(frames,before,tr);candidate=truth.copy();candidate[4,20:30,20:76]=True
        d=image_judge(frames,before,candidate,tr,p);self.assertFalse(d['accepted']);self.assertIn('added_region_background',d['reasons'])
    def test_no_original_gate_is_relaxed(self):
        frames,truth,before,tr=scene();p=image_plan(frames,before,tr);d=image_judge(frames,before,before,tr,p)
        self.assertIn('insufficient_holdout_gain',d['reasons']);self.assertFalse(d['accepted'])
    def test_nonfinite_out_of_image_points_abstain(self):
        frames,truth,before,tr=scene();f=image_field(frames[0],before[0]);support,rows=point_evidence(f,before[0],np.array([[np.nan,0],[-1,0],[1000,0]]));self.assertFalse(support.any());self.assertTrue(all(r['label']=='invalid' for r in rows))
    def test_invalid_image_policies_rejected(self):
        for kw in [dict(min_contrast=0),dict(foreground_score=.1),dict(max_added_background_fraction=2),dict(min_path_score=float('nan'))]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):ImagePolicy(**kw)

if __name__=='__main__':unittest.main()
