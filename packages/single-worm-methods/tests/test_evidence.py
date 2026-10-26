"""Uncertainty, axis transforms, conflicting tracks and event-level inference."""
import itertools
import unittest
import numpy as np

from wormstudio_single.evidence import decode_heads, prepare, candidate_cost
from wormstudio_single.fusion_v4 import fit, select_with_margins
from wormstudio_single.contact import _transition
from wormstudio_single.quality import INDICES
from test_constrained_fusion import fixture


def rich(d,z):
    z={k:v.copy() for k,v in z.items()}
    h,w=d['masks'].shape[1:]
    for direction in ('forward','backward'):
        rows=[]
        for points in z[direction]:
            centers=points[:,::-1]/[h,w]*256-.5
            logits=-((np.arange(256)[None,None]-centers[...,None])/1.2)**2
            rows.append(decode_heads(logits.reshape(21,512),np.full(21,8.),w,h))
        for key in rows[0]:z[f'{direction}_{key}']=np.array([r[key] for r in rows])
    return z


class EvidenceTests(unittest.TestCase):
    def test_xy_scaling_and_full_distribution_retains_remote_mode(self):
        logits=np.full((1,512),-100.)
        logits[0,30]=logits[0,210]=20  # first head is displayed Y
        logits[0,256+50]=20
        e=decode_heads(logits,np.array([2.]),512,128)
        self.assertEqual(e['coordinate_probabilities'][0,0].argmax(),50)
        self.assertAlmostEqual(e['position_std_px'][0,1],45.,places=4)
        self.assertGreater(e['entropy'][0,1],e['entropy'][0,0])

    def test_conflict_reduces_both_votes_instead_of_taking_best_direction(self):
        d,z=fixture();d['masks'][:]=True
        base=np.repeat(d['first'][None,INDICES],3,axis=0)
        z['forward']=base.copy();z['backward']=base.copy()
        consistent=prepare(d,rich(d,z))
        z['backward']+=np.array([0,20])
        conflict=prepare(d,rich(d,z))
        for a,b in zip(consistent['frames'][1]['directions'],conflict['frames'][1]['directions']):
            self.assertLess(b['weight'].sum(),a['weight'].sum()/3)
        self.assertEqual(conflict['frames'][1]['diagnostic']['tap_conflict_fraction'],1.)

    def test_low_visibility_has_less_influence_and_no_weight_renormalization(self):
        d,z=fixture();d['masks'][:]=True
        z['forward']=z['backward']=np.repeat(d['first'][None,INDICES],3,axis=0)
        z=rich(d,z);high=prepare(d,z)['frames'][1]
        for direction in ('forward','backward'):z[f'{direction}_visibility_logits'][:]=-8
        low=prepare(d,z)['frames'][1]
        wrong=d['first']+[0,10]
        self.assertLess(candidate_cost(wrong,low),candidate_cost(wrong,high)/100)

    def test_one_sided_tracking_remains_finite(self):
        d,z=fixture();z=rich(d,z);d['last'][:]=np.nan
        z['backward'][:]=np.nan;z['backward_visible'][:]=False
        for key in list(z):
            if key.startswith('backward_') and key!='backward_visible':
                z[key][:]=0 if key.endswith('coordinate_probabilities') else np.nan
        p,q=fit(d,z)
        self.assertTrue(np.isfinite(p).all())
        self.assertTrue(all(x['tap_bidirectional_points']==0 for x in q))

    def test_bimodal_information_survives_low_coordinate_precision(self):
        d,z=fixture();d['masks'][:]=True
        z['forward']=z['backward']=np.repeat(d['first'][None,INDICES],3,axis=0)
        z=rich(d,z)
        for direction in ('forward','backward'):
            z[f'{direction}_position_std_px'][:]=1e6
        e=prepare(d,z)['frames'][1]
        self.assertLess(e['directions'][0]['weight'].sum(),1e-6)
        self.assertGreater(candidate_cost(d['first']+[0,10],e),candidate_cost(d['first'],e)+.01)

    def test_reversed_backward_body_labels_are_aligned_once_per_window(self):
        d,z=fixture();z['forward']=np.repeat(d['first'][None,INDICES],3,axis=0)
        z['backward']=z['forward'][:,::-1].copy()
        e=prepare(d,rich(d,z))
        self.assertTrue(e['backward_reversed'])
        self.assertAlmostEqual(e['frames'][1]['diagnostic']['tap_bidirectional_error_px'],0.)

    def test_rich_evidence_cannot_override_clear_shape_or_invalid_mask(self):
        d,z=fixture();z=rich(d,z)
        p,q=fit(d,z)
        self.assertTrue(np.all(abs(p[:,:,1]-50)<2))
        self.assertTrue(all(x['geometric_pass'] for x in q))
        d['mask_valid'][1]=False;p,q=fit(d,z)
        self.assertTrue(np.isnan(p[1]).all())
        self.assertFalse(q[1]['geometric_pass'])

    def test_v4_refuses_to_invent_missing_model_evidence(self):
        d,z=fixture()
        with self.assertRaisesRegex(ValueError,'retrack'):fit(d,z)

    def test_partial_evidence_is_not_silently_treated_as_a_legacy_cache(self):
        from wormstudio_single.pipeline import run_fusion
        d,z=fixture();z=rich(d,z);del z['forward_entropy']
        with self.assertRaisesRegex(ValueError,'Incomplete'):run_fusion(d,z,'auto')

    def test_motion_evidence_breaks_an_otherwise_symmetric_branch_tie(self):
        d,z=fixture(4);d['masks'][:]=True
        p=d['first'];target=np.array([p,p+[0,2],p+[0,4],p])
        z['forward']=z['backward']=target[:,INDICES]
        e=prepare(d,rich(d,z))
        choices=[[dict(points=p,unary=0.)],
                 [dict(points=p+[0,-2],unary=0.),dict(points=p+[0,2],unary=0.)],
                 [dict(points=p+[0,-4],unary=0.),dict(points=p+[0,4],unary=0.)],
                 [dict(points=p,unary=0.)]]
        for frame,states in zip(e['frames'],choices):
            for state in states:state['unary']=2*candidate_cost(state['points'],frame)
        selected,gaps,_=select_with_margins(choices,d['reference_length'])
        self.assertEqual(selected,[0,1,1,0]);self.assertGreater(gaps[1][0],0)

    def test_event_min_marginals_equal_exhaustive_sequence_search(self):
        rng=np.random.default_rng(8);p=np.column_stack([np.arange(101),np.zeros(101)])
        states=[[dict(points=p+rng.normal(size=2)*3,unary=float(rng.random())) for _ in range(2)] for _ in range(5)]
        selected,margins,best=select_with_margins(states,100)
        costs=[];paths=list(itertools.product(range(2),repeat=5))
        for path in paths:
            points=[s[k]['points'] for s,k in zip(states,path)]
            cost=sum(s[k]['unary'] for s,k in zip(states,path))
            cost+=sum(float(_transition(a,b,8.)) for a,b in zip(points,points[1:]))
            cost+=sum(.6*float(_transition(np.zeros_like(c),c-2*b+a,8.)) for a,b,c in zip(points,points[1:],points[2:]))
            costs.append(cost)
        self.assertEqual(tuple(selected),paths[int(np.argmin(costs))])
        self.assertAlmostEqual(best,min(costs))
        for i in range(5):
            for k in range(2):self.assertAlmostEqual(margins[i][k],min(c for path,c in zip(paths,costs) if path[i]==k)-best)


if __name__=='__main__':unittest.main()
