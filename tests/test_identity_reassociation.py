import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import copy,tempfile,unittest
import cv2,numpy as np
from identity_reassociation import reassociate,validate_reviews,ImageEvidence


def inputs():
    rows=[dict(frame=f,worm_id=1,mask_valid=f!=5,identity_valid=f<5,identity_segment=int(f>=5),
        identity_status='provisional_continuous' if f<5 else 'post_contact_or_jump_unverified',
        source_frame_invalid=False,identity_evidence='test',reasons=[]) for f in range(15)]
    return {1:rows},dict(frames=15,fps=12,video='not-opened-by-test',crop_xyxy=[0,0,200,150])


class Evidence:
    def __init__(self,accept=True):self.accept=accept;self.calls=[]
    def compare(self,w,a,b):
        self.calls.append((w,a,b));return dict(accepted=self.accept,checks={'synthetic_test':self.accept})


def review(decision='same',start=6,end=14):
    return dict(id='test-only-review',worm_id=1,start_frame=start,end_frame=end,decision=decision,
        reference_start_frame=0,reference_end_frame=4,
        reviewer='synthetic-test',evidence='synthetic test fixture only',confirmed_at='2026-10-21T00:00:00+00:00')


class AssociationTests(unittest.TestCase):
    def test_success_restores_identity_but_does_not_modify_input_or_jump(self):
        g,s=inputs();old=copy.deepcopy(g);p=Evidence();q,r=reassociate(g,s,None,evidence_provider=p)
        self.assertEqual(g,old);self.assertTrue(q[1][6]['identity_valid'])
        self.assertEqual(q[1][6]['identity_status'],'auto_reassociated')
        self.assertFalse(q[1][6]['identity_human_confirmed']);self.assertFalse(q[1][5]['mask_valid'])
        self.assertNotEqual(q[1][4]['identity_segment'],q[1][6]['identity_segment'])
        self.assertFalse(r['scores_are_probabilities'])

    def test_ambiguous_evidence_remains_unlinked(self):
        g,s=inputs();q,r=reassociate(g,s,None,evidence_provider=Evidence(False))
        self.assertFalse(q[1][6]['identity_valid']);self.assertEqual(r['edges'][1]['decision'],'ambiguous')

    def test_human_same_overrides_ambiguous_matching(self):
        g,s=inputs();q,r=reassociate(g,s,None,reviews=[review()],evidence_provider=Evidence(False))
        self.assertTrue(q[1][6]['identity_human_confirmed']);self.assertTrue(q[1][6]['identity_valid'])

    def test_human_different_and_unknown_override_automatic(self):
        for decision in ('different','unknown'):
            g,s=inputs();q,r=reassociate(g,s,None,reviews=[review(decision)],evidence_provider=Evidence(True))
            self.assertFalse(q[1][6]['identity_valid']);self.assertEqual(r['edges'][1]['decision'],'human_'+decision)

    def test_removing_review_restores_algorithm(self):
        g,s=inputs();q,_=reassociate(g,s,None,reviews=[review('different')],evidence_provider=Evidence())
        restored,_=reassociate(g,s,None,reviews=[],evidence_provider=Evidence())
        self.assertFalse(q[1][6]['identity_valid']);self.assertTrue(restored[1][6]['identity_valid'])

    def test_short_fragment_stays_candidate(self):
        g,s=inputs();g[1]=g[1][:9];s['frames']=9
        q,r=reassociate(g,s,None,evidence_provider=Evidence());self.assertEqual(r['edges'][1]['reason'],'short_fragment')

    def test_source_invalid_and_fit_rejection_split(self):
        g,s=inputs();g[1][9]['source_frame_invalid']=True
        fit={1:[dict(mask_valid=f!=11) for f in range(15)]}
        q,r=reassociate(g,s,None,fit,evidence_provider=Evidence())
        self.assertEqual([(e['start_frame'],e['end_frame']) for e in r['edges']],[(0,4),(6,8),(10,10),(12,14)])

    def test_stale_review_bounds_rejected(self):
        g,s=inputs()
        with self.assertRaises(ValueError):reassociate(g,s,None,reviews=[review(start=7)],evidence_provider=Evidence())

    def test_multiobject_automatic_disabled(self):
        g,s=inputs();g[2]=copy.deepcopy(g[1])
        for row in g[2]:row['worm_id']=2
        q,r=reassociate(g,s,None,evidence_provider=Evidence());self.assertFalse(q[1][6]['identity_valid'])

    def test_review_requires_evidence_unique_bounds_and_timestamp(self):
        for change in ({'evidence':''},{'confirmed_at':'2026-10-21'},{'end_frame':100},{'decision':'yes'}):
            item=review();item.update(change)
            with self.assertRaises(ValueError):validate_reviews(dict(identity_reviews=[item]),[1],15)
        with self.assertRaises(ValueError):validate_reviews(dict(identity_reviews=[review(),review()]),[1],15)

    def test_rejected_parent_does_not_imply_accepted_child(self):
        g,s=inputs();g[1][10]['mask_valid']=False
        q,r=reassociate(g,s,None,reviews=[review('different',6,9)],evidence_provider=Evidence())
        self.assertFalse(q[1][11]['identity_valid']);self.assertEqual(r['edges'][2]['reason'],'previous_human_split_or_unknown')

    def test_long_gap_abstains(self):
        g,s=inputs();s['fps']=1
        q,r=reassociate(g,s,None,evidence_provider=Evidence());self.assertEqual(r['edges'][1]['reason'],'gap_too_long')

    def test_changing_upstream_reference_invalidates_later_human_link(self):
        g,s=inputs();g[1][10]['mask_valid']=False
        earlier=review('different',6,9)
        later=review('same',11,14);later.update(id='later-review',reference_start_frame=6,reference_end_frame=9)
        q,r=reassociate(g,s,None,reviews=[earlier,later],evidence_provider=Evidence())
        self.assertEqual(r['edges'][2]['decision'],'stale_review')
        self.assertFalse(q[1][11]['identity_valid']);self.assertFalse(q[1][11]['identity_human_confirmed'])

    def test_head_confirmation_does_not_cross_adjacent_identity_segments(self):
        from head_tail import detect_head_tail
        p=np.stack([np.column_stack([np.linspace(10,110,101),np.full(101,40.)]) for _ in range(10)])
        rows=detect_head_tail(p,[True]*10,manual=dict(frame=0,point=p[0,0].tolist()),mode='manual_only',segment_ids=[0]*5+[1]*5)
        self.assertTrue(all(r['head_identity_confirmed'] for r in rows[:5]))
        self.assertTrue(all(not r['head_identity_confirmed'] for r in rows[5:]))
        self.assertNotEqual(rows[4]['head_segment'],rows[5]['head_segment'])

    def test_association_does_not_move_pose_length_rejection_threshold(self):
        from analyze_phenotypes import pose_length_reference
        rows=[dict(length_um=100.,centerline_valid=True,identity_original_valid=True,identity_valid=True) for _ in range(4)]
        rows += [dict(length_um=160.,centerline_valid=True,identity_original_valid=False,identity_valid=True) for _ in range(20)]
        self.assertEqual(pose_length_reference(rows),100.)
        for row in rows[4:]:row['identity_valid']=False
        self.assertEqual(pose_length_reference(rows),100.)


class PixelEvidenceTests(unittest.TestCase):
    def describe(self,extra=False,static=False):
        with tempfile.TemporaryDirectory() as folder:
            image=np.full((200,300,3),200,np.uint8);mask=np.zeros((200,300),np.uint8)
            cv2.line(mask,(80,70),(150,70),1,10);image[mask>0]=50
            if extra:cv2.line(image,(80,100),(150,100),(50,50,50),10)
            path=str(Path(folder)/'source.avi');writer=cv2.VideoWriter(path,cv2.VideoWriter_fourcc(*'MJPG'),12,(300,200));writer.write(image);writer.release()
            provider=ImageEvidence(dict(video=path,crop_xyxy=[0,0,300,200],frames=1),lambda w,f:mask)
            provider.background=provider.frame(0).copy() if static else np.full((100,150),200,np.uint8)
            result=provider.describe(1,0);provider.close();return result

    def test_independent_source_pixels_find_target(self):
        result=self.describe();self.assertEqual(result['candidate_count'],1)
        self.assertGreater(result['candidates'][0]['overlap'],.5)

    def test_competing_source_target_is_ambiguous(self):
        self.assertEqual(self.describe(extra=True)['candidate_count'],2)

    def test_static_background_object_does_not_pass_redetection(self):
        result=self.describe(static=True);self.assertEqual(result['candidate_count'],0)
        self.assertEqual(result['foreground_support'],0)

if __name__=='__main__':unittest.main()
