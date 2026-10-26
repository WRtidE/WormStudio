"""Behavioral gates, rollback, held-out validation and opt-out isolation."""
import unittest
from unittest.mock import Mock
import numpy as np
from wormstudio_single.feedback import FeedbackPolicy,sample_queries,plan_feedback,judge_proposal
from wormstudio_single.feedback_runner import controlled_pass

def fixture():
    truth=np.zeros((11,96,96),bool);truth[:,30:65,20:76]=True;before=truth.copy();before[3:8,:,60:]=False
    queries=sample_queries(truth[0]);f=np.repeat(queries[None],11,axis=0)
    tracks=dict(queries=queries,forward=f.copy(),backward=f.copy(),forward_visible=np.ones((11,32),bool),backward_visible=np.ones((11,32),bool))
    for d in ('forward','backward'):tracks[d+'_visibility_logits']=np.full((11,32),5.);tracks[d+'_entropy']=np.full((11,32,2),.1)
    return truth,before,tracks

class FeedbackTests(unittest.TestCase):
    def test_disabled_never_calls_models_and_returns_exact_masks(self):
        truth,before,tracks=fixture();fn=Mock(side_effect=AssertionError('model called'))
        final,result,art=controlled_pass(None,before,False,FeedbackPolicy(),fn,fn,fn)
        np.testing.assert_array_equal(final,before);self.assertEqual(result['iterations'],0);fn.assert_not_called()
    def test_prompt_queries_exclude_heldout(self):
        truth,before,tracks=fixture();plan=plan_feedback(before,tracks)
        self.assertEqual(plan['status'],'proposed');self.assertFalse(set(plan['prompt_query_ids'])&set(plan['heldout_query_ids']))
    def test_heldout_positions_do_not_change_any_prompt(self):
        truth,before,tracks=fixture();plan=plan_feedback(before,tracks)
        j=plan['heldout_query_ids'][0];tracks['queries'][j]=[2,2];tracks['forward'][:,j]=[2,2];tracks['backward'][:,j]=[2,2]
        other=plan_feedback(before,tracks);self.assertEqual(plan['points'],other['points']);self.assertEqual(plan['labels'],other['labels'])
    def test_heldout_queries_support_repair(self):
        truth,before,tracks=fixture();plan=plan_feedback(before,tracks);decision=judge_proposal(before,truth,tracks,plan)
        self.assertTrue(decision['accepted'],decision);self.assertGreater(decision['heldout_gain'],.1)
    def test_no_disagreement_means_no_proposal(self):
        truth,before,tracks=fixture();self.assertEqual(plan_feedback(truth,tracks)['status'],'no_trigger')
    def test_invisible_points_cannot_prompt(self):
        truth,before,tracks=fixture();tracks['forward_visible'][:]=False
        self.assertEqual(plan_feedback(before,tracks)['status'],'no_trigger')
    def test_uncertain_logits_cannot_prompt(self):
        truth,before,tracks=fixture();tracks['forward_visibility_logits'][:]=0
        self.assertEqual(plan_feedback(before,tracks)['status'],'no_trigger')
    def test_diffuse_coordinate_distributions_cannot_prompt(self):
        truth,before,tracks=fixture();tracks['forward_entropy'][:]=.95
        self.assertEqual(plan_feedback(before,tracks)['status'],'no_trigger')
    def test_cycle_drift_cannot_prompt(self):
        truth,before,tracks=fixture();tracks['backward'][0,:,0]+=10
        self.assertEqual(plan_feedback(before,tracks)['status'],'no_trigger')
    def test_bidirectional_conflict_cannot_prompt(self):
        truth,before,tracks=fixture();tracks['backward'][1:-1,:,0]+=10
        self.assertEqual(plan_feedback(before,tracks)['status'],'no_trigger')
    def test_nonfinite_points_cannot_prompt(self):
        truth,before,tracks=fixture();tracks['forward'][:]=np.nan
        self.assertEqual(plan_feedback(before,tracks)['status'],'no_trigger')
    def test_prompt_fitting_alone_not_enough(self):
        truth,before,tracks=fixture();plan=plan_feedback(before,tracks);proposal=before.copy();i=plan['frame']
        for p,label in zip(plan['points'],plan['labels']):
            if label:proposal[i,int(round(p[1])),int(round(p[0]))]=True
        decision=judge_proposal(before,proposal,tracks,plan)
        self.assertFalse(decision['accepted']);self.assertIn('insufficient_holdout_gain',decision['reasons'])
    def test_out_of_budget_changes_reject_entire_proposal(self):
        truth,before,tracks=fixture();plan=plan_feedback(before,tracks);proposal=truth.copy();proposal[0,0,0]=True
        self.assertIn('changed_outside_budget',judge_proposal(before,proposal,tracks,plan)['reasons'])
    def test_excess_area_rejects(self):
        truth,before,tracks=fixture();plan=plan_feedback(before,tracks);proposal=truth.copy();proposal[plan['frame']]=True
        self.assertIn('area_guard',judge_proposal(before,proposal,tracks,plan)['reasons'])
    def test_local_loss_not_hidden_by_other_improved_frames(self):
        truth,before,tracks=fixture();plan=plan_feedback(before,tracks);proposal=truth.copy()
        ids=[j for j in plan['heldout_query_ids'] if tracks['queries'][j,0]<55]
        x,y=tracks['queries'][ids[0]].round().astype(int);proposal[4,y-1:y+2,x-1:x+2]=False
        self.assertIn('frame_lost_supported_region',judge_proposal(before,proposal,tracks,plan)['reasons'])
    def test_rejection_is_atomic_and_does_not_reseed(self):
        truth,before,tracks=fixture();bad=before.copy();reseed=Mock(side_effect=AssertionError('must not reinitialize'))
        final,result,art=controlled_pass(None,before,True,FeedbackPolicy(),lambda *a:tracks,lambda *a:bad,reseed)
        np.testing.assert_array_equal(final,before);self.assertEqual(result['status'],'rejected');reseed.assert_not_called()
    def test_acceptance_calls_reseed_exactly_once(self):
        truth,before,tracks=fixture();seg=Mock(return_value=truth);reseed=Mock(return_value={'new':True})
        final,result,art=controlled_pass(None,before,True,FeedbackPolicy(),lambda *a:tracks,seg,reseed)
        np.testing.assert_array_equal(final,truth);self.assertTrue(result['accepted']);self.assertEqual(result['iterations'],1)
        self.assertEqual(seg.call_count,1);self.assertEqual(reseed.call_count,1);self.assertIn('no old point',result['new_query_identity'])
    def test_invalid_policy_rejected(self):
        for kw in [dict(radius=0),dict(radius=31),dict(query_count=100),dict(max_cycle_px=-1),dict(min_area_ratio=2)]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):FeedbackPolicy(**kw)
    def test_tiny_mask_abstains(self):
        mask=np.zeros((20,20),bool);mask[5:7,5:7]=True
        self.assertEqual(len(sample_queries(mask)),0)

if __name__=='__main__':unittest.main()
