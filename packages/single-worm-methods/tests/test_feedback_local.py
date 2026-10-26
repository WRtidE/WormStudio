import unittest
from dataclasses import replace
from unittest.mock import patch
import numpy as np
from test_feedback import fixture
from wormstudio_single.feedback import FeedbackPolicy
from wormstudio_single.feedback_local import LocalPolicy,windows,concentrated_tracks,local_plan,local_judge,select_attempts,track_window

def evidence():
    truth,before,tr=fixture()
    for d in ('forward','backward'):
        p=np.full((11,32,2,256),.7/256)
        for i in range(11):
            for j in range(32):
                for a in range(2):p[i,j,a,min(255,int(tr[d][i,j,a]*256/96))]+=.3
        tr[d+'_coordinate_probabilities']=p
        tr[d+'_entropy'][:]=.85
    return truth,before,tr

class LocalFeedbackTests(unittest.TestCase):
    def test_windows_cover_tail_and_have_bounded_length(self):
        for n in [3,17,18,101,141,201]:
            w=windows(n);covered=set()
            for s,e in w:covered.update(range(s,e));self.assertLessEqual(e-s,17)
            self.assertEqual(covered,set(range(n)));self.assertEqual(len(w),len(set(w)))
    def test_invalid_local_policy(self):
        for kw in [dict(window=4),dict(window=100),dict(stride=17),dict(persistence=16),dict(min_local_mass=0),dict(min_mass_enrichment=1),dict(mass_radius_px=float('nan'))]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):LocalPolicy(**kw)
    def test_concentrated_peak_with_entropy_tail_can_trigger(self):
        truth,before,tr=evidence();p=local_plan(before,tr);self.assertEqual(p['status'],'proposed');self.assertEqual(p['persistence_frames'],2)
        self.assertTrue(local_judge(before,truth,tr,p)['accepted'])
    def test_uniform_head_cannot_trigger_even_with_perfect_cycle(self):
        truth,before,tr=evidence()
        for d in ('forward','backward'):tr[d+'_coordinate_probabilities'][:]=1/256
        self.assertEqual(local_plan(before,tr)['status'],'no_trigger')
    def test_distant_peak_cannot_support_decoded_point(self):
        truth,before,tr=evidence()
        for d in ('forward','backward'):
            tr[d+'_coordinate_probabilities'][:]=0;tr[d+'_coordinate_probabilities'][...,0]=1
        self.assertEqual(local_plan(before,tr)['status'],'no_trigger')
    def test_single_frame_disagreement_is_suppressed(self):
        truth,before,tr=evidence();single=truth.copy();single[5]=before[5]
        self.assertEqual(local_plan(single,tr)['status'],'no_trigger')
    def test_cycle_and_visibility_still_required(self):
        for fail in ('cycle','visible'):
            truth,before,tr=evidence()
            if fail=='cycle':tr['backward'][0]+=10
            else:tr['forward_visible'][:]=False
            self.assertEqual(local_plan(before,tr)['status'],'no_trigger')
    def test_heldout_does_not_choose_prompts(self):
        truth,before,tr=evidence();p=local_plan(before,tr)
        for d in ('forward','backward'):tr[d+'_coordinate_probabilities'][:,::3]=1/256
        other=local_plan(before,tr);self.assertEqual(p['points'],other['points']);self.assertEqual(p['frame'],other['frame'])
        self.assertFalse(local_judge(before,truth,tr,other)['accepted'])
    def test_prompt_only_fit_rejected(self):
        truth,before,tr=evidence();p=local_plan(before,tr);candidate=before.copy()
        for (x,y),label in zip(p['points'],p['labels']):
            if label:candidate[p['frame'],round(y),round(x)]=True
        self.assertFalse(local_judge(before,candidate,tr,p)['accepted'])
    def test_attempts_bounded_disjoint_and_deterministic(self):
        def r(s,missing):return dict(start=s,stop=s+17,plan=dict(status='proposed',frames=[dict(missing_queries=missing)]))
        records=[r(0,4),r(8,5),r(24,3),r(48,3),r(80,3)]
        chosen=select_attempts(records);self.assertEqual([r['start'] for r in chosen],[8,48,80]);self.assertEqual(chosen,select_attempts(records[::-1]))
    def test_final_mask_membership_does_not_block_reverse_cycle(self):
        truth,before,tr=evidence();frames=np.zeros((11,96,96,3),np.uint8);masks=truth.copy();masks[-1]=False
        calls=[]
        def fake(model,frames,points,with_evidence):
            calls.append(len(points));n=len(frames);q=len(points);p=np.repeat(points[None],n,axis=0)
            return p,np.ones((n,q),bool),{'entropy':np.zeros((n,q,2)),'visibility_logits':np.ones((n,q))*5}
        with patch('wormstudio_single.inference.track',side_effect=fake):out=track_window(None,frames,masks)
        self.assertEqual(calls,[32,32]);self.assertTrue(np.isfinite(out['backward']).all())

if __name__=='__main__':unittest.main()
