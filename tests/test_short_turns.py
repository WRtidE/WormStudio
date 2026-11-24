import unittest
from unittest.mock import patch

import numpy as np

from body_sequence_solver import head_results
from marked_end_recovery import recover_marked_end
from result_overlay import draw_worm
from short_turns import continue_candidate_tip, bracketed_order, recover_flanked_candidates, visible_end_order
from test_marked_end_recovery import worm_frame


class ShortTurnTests(unittest.TestCase):
    def run_turn(self, mark, mode='clear', second_mark=None):
        marks=[mark] if second_mark is None else [mark,second_mark]
        masks,gray,lines,facts=[],[],[],[]
        for i in range(16):
            mask,image=worm_frame(i)
            if mode=='appearance_conflict' and i==8:
                image=np.where(mask,195-image,210).astype('uint8')
            masks.append(mask)
            gray.append(image)
            lo,hi=(0,6) if i<7 else (9,15) if i>8 else (i,i)
            local=[m for m in marks if lo<=m<=hi]
            rooted=bool(local)
            path=np.column_stack([np.linspace(42+i,162+i,101),np.full(101,50.)])
            reverse=(not rooted and lo!=8) or (mode=='opposing_marks' and second_mark in local)
            lines.append(path[::-1] if reverse else path)
            facts.append(dict(component=[lo,hi],head_known=rooted,manual_lineage=rooted,
                anchor_frames=local,anchor_conflict=False,head_observed=True,
                tail_observed=True,length_px=120,candidate='clear_observation',
                head_source='initial_mark' if rooted else 'unresolved'))
        lines=np.array(lines)
        if mode=='missing_pose':
            facts[7]=None
            lines[7]=np.nan
        excluded={8} if mode=='excluded' else set()
        if mode=='inferred_tip':
            facts[8]['tail_observed']=False
        guards=None
        if mode=='identity_break':
            guards=[dict(mask_valid=True,identity_valid=True,identity_segment=int(i>=8)) for i in range(16)]
        audit=recover_marked_end(lines,facts,masks,gray,12,[dict(frame=m) for m in marks],excluded,guards)
        return lines,facts,audit

    def test_isolated_swaps_and_far_track_are_corrected_only_as_candidates(self):
        for mark in (2,13):
            with self.subTest(mark=mark):
                lines,facts,audit=self.run_turn(mark)
                self.assertTrue(any(r.get('candidate_only') for r in audit))
                heads=head_results([dict(body_sequence=f) for f in facts])
                for i,(path,head) in enumerate(zip(lines,heads)):
                    self.assertAlmostEqual(path[0,0],42+i)
                    rooted=facts[i]['component'][0]<=mark<=facts[i]['component'][1]
                    self.assertEqual(head['head_identity_confirmed'],rooted)
                    if not rooted:
                        self.assertEqual(head['head_status'],'unconfirmed_temporal_candidate')
                        self.assertFalse(facts[i]['anchor_frames'])

    def test_turn_cannot_bridge_exclusion_missing_pose_or_contradictory_appearance(self):
        for mode in ('excluded','missing_pose','appearance_conflict'):
            with self.subTest(mode=mode):
                _,facts,audit=self.run_turn(2,mode)
                self.assertFalse(any(r.get('candidate_only') for r in audit))
                self.assertFalse(any(f and f['head_known'] for f in facts[7:]))

    def test_short_turn_between_two_marks_tracks_both_observed_ends(self):
        lines,facts,audit=self.run_turn(2,second_mark=13)
        verified,=[e for e in audit if e['accepted']]
        self.assertEqual(verified['reason'],'two_sided_marked_turn')
        self.assertEqual(verified['forward']['trajectory_frames'],[6,7,8,9,10])
        self.assertEqual(verified['backward']['trajectory_frames'],[9,8,7,6,5])
        heads=head_results([dict(body_sequence=f) for f in facts])
        for i in (7,8):
            np.testing.assert_allclose(lines[i,[0,-1],0],[42+i,162+i])
            self.assertEqual(facts[i]['anchor_frames'],[2,13])
            self.assertEqual(heads[i]['head_status'],'recovered_from_user_point')
            self.assertTrue(heads[i]['head_identity_confirmed'])

    def test_two_marks_do_not_force_a_conflicting_or_unobserved_turn(self):
        for mode in ('opposing_marks','appearance_conflict','excluded','missing_pose','inferred_tip','identity_break'):
            with self.subTest(mode=mode):
                lines,facts,audit=self.run_turn(2,mode,second_mark=13)
                self.assertFalse(any(e['accepted'] for e in audit))
                self.assertFalse(any(f and f['head_known'] for f in facts[7:9]))
                if mode=='opposing_marks':
                    self.assertTrue(all(f['anchor_conflict'] for f in facts[7:9]))
                    self.assertEqual(lines[13,0,0],175)

    def test_indistinguishable_or_teleporting_ends_are_rejected(self):
        path=np.column_stack([np.arange(101),np.zeros(101)])
        poses=np.stack([path]*5)
        poses[2]=50
        self.assertIsNone(bracketed_order(poses,list(range(5)),0,100))
        poses[2]=path+[120,10]
        self.assertIsNone(bracketed_order(poses,list(range(5)),0,100))

    def local_turn(self, mode):
        # A pair of isolated curves arrives in opposite file orders in this fixture.
        paths=np.array([np.column_stack([np.arange(101)+i,np.full(101,40.)]) for i in range(12)])
        paths[5]=paths[5,::-1]
        facts=[]
        features=np.ones((12,2,6))
        features[5]*=-1
        for i in range(12):
            lo,hi=(0,4) if i<5 else (7,11) if i>6 else (i,i)
            known=i not in (5,6)
            facts.append(dict(component=[lo,hi],head_known=known,head_observed=True,tail_observed=True,
                              candidate='clear_observation',anchor_conflict=False,manual_lineage=False,
                              head_source='auto_taper_seed' if known else 'unresolved',length_px=100))
        guards=[dict(mask_valid=True,identity_valid=True,identity_segment=0) for _ in paths]
        if mode=='opposite_appearance':features[5]*=-1
        if mode=='opposite_flank':features[7:]*=-1
        if mode=='indistinct_tips':features[6]=0
        if mode in ('same_component','inconsistent_component'):
            facts[5]['component']=facts[6]['component']=[5,6]
            features[6]=0
            if mode=='same_component':paths[6]=paths[6,::-1]
        if mode=='hidden':facts[6]['tail_observed']=False
        if mode=='identity_break':guards[5]['identity_segment']=1
        if mode=='teleport':paths[5,:,1]+=100
        if mode=='conflicting_mark':facts[5]['anchor_conflict']=True
        excluded={5} if mode=='excluded' else set()
        audit=recover_flanked_candidates(paths,facts,12,2,excluded,guards,features.__getitem__)
        return paths,facts,audit

    def test_clear_flanked_ends_can_be_oriented_without_a_manual_point(self):
        lines,facts,audit=self.local_turn('clear')
        self.assertEqual(audit[0]['target_orders'],[1,0])
        self.assertTrue(audit[0]['accepted'] and audit[0]['candidate_only'])
        heads=head_results([dict(body_sequence=f) for f in facts])
        for i in (5,6):
            self.assertEqual(lines[i,0,0],i)
            self.assertTrue(facts[i]['head_known'])
            self.assertEqual(heads[i]['head_status'],'unconfirmed_temporal_candidate')
            self.assertFalse(heads[i]['head_identity_confirmed'])

    def test_flanked_ends_abstain_when_shape_motion_or_identity_is_unreliable(self):
        for mode in ('opposite_appearance','opposite_flank','indistinct_tips','hidden','teleport',
                     'excluded','identity_break','conflicting_mark','inconsistent_component'):
            with self.subTest(mode=mode):
                _,facts,audit=self.local_turn(mode)
                self.assertFalse(any(e['accepted'] for e in audit))
                self.assertFalse(any(f['head_known'] for f in facts[5:7]))

    def test_two_frame_track_can_share_appearance_but_needs_motion_on_each_frame(self):
        lines,facts,audit=self.local_turn('same_component')
        self.assertEqual(audit[0]['support_frames'],[5])
        self.assertEqual(audit[0]['target_orders'],[1,1])
        self.assertTrue(all(f['head_known'] and not f['manual_lineage'] for f in facts[5:7]))
        np.testing.assert_allclose(lines[5:7,0,0],[5,6])

    def long_gap(self, mode):
        lines=np.array([np.column_stack([np.arange(101)+i,np.full(101,40.)]) for i in range(19)])
        features=np.ones((19,2,6))
        facts=[]
        for i in range(19):
            component=next((a,b) for a,b in ((0,6),(7,7),(8,10),(11,12),(13,18)) if a<=i<=b)
            known=i<7 or i>=13
            if 8<=i<=10:
                lines[i]=lines[i,::-1]
                features[i]*=-1
            facts.append(dict(component=component,head_known=known,head_source='auto_taper_seed' if known else 'unresolved',
                              head_observed=True,tail_observed=True,candidate='clear_observation',
                              anchor_conflict=False,manual_lineage=False,length_px=100))
        for i in (5,6):
            facts[i].update(head_observed=False,candidate='hidden_loop')
            lines[i,:11]=[300,200]  # The inferred end has no vote.
        facts[7]['candidate']='junction_pairing'
        guards=[dict(mask_valid=True,identity_valid=True,identity_segment=1) for _ in lines]
        if mode=='conflicting_motion':
            lines[13:]=lines[13:,::-1]
        if mode=='weak_component':
            features[8:11]=0
        if mode=='single_contradiction':
            features[9]*=-1
        if mode=='conflicting_templates':
            features[13:]*=-1
        if mode=='source_break':
            guards[10]['source_frame_invalid']=True
        if mode=='identity_break':
            guards[10]['identity_segment']=2
        if mode=='missing':
            facts[9]=None
        if mode=='mark_conflict':
            facts[9]['anchor_conflict']=True
        if mode=='only_recovered_flank':
            facts[13]['head_source']='visible_end_track_candidate'
        before=lines.copy()
        audit=recover_flanked_candidates(lines,facts,12,2,set(),guards,features.__getitem__)
        return before,lines,facts,audit

    def test_visible_tail_links_multiple_components_without_confirming_a_head(self):
        before,lines,facts,audit=self.long_gap('clear')
        self.assertEqual([e['component'] for e in audit if e['accepted']],[[8,10],[11,12]])
        self.assertFalse(facts[7]['head_known'])
        np.testing.assert_array_equal(before[:8],lines[:8])
        np.testing.assert_array_equal(before[13:],lines[13:])
        for i in range(8,13):
            self.assertEqual(lines[i,0,0],i)
            self.assertEqual(facts[i]['head_source'],'visible_end_track_candidate')
            self.assertFalse(head_results([dict(body_sequence=facts[i])])[0]['head_identity_confirmed'])

    def test_visible_track_abstains_on_breaks_and_opposing_flanks(self):
        for mode in ('conflicting_motion','conflicting_templates','source_break','identity_break',
                     'missing','mark_conflict','only_recovered_flank'):
            with self.subTest(mode=mode):
                before,lines,_,audit=self.long_gap(mode)
                self.assertFalse(any(e['accepted'] for e in audit))
                np.testing.assert_array_equal(before,lines)
        for mode in ('weak_component','single_contradiction'):
            _,lines,facts,audit=self.long_gap(mode)
            self.assertEqual([e['component'] for e in audit if e['accepted']],[[11,12]])
            self.assertFalse(any(f['head_known'] for f in facts[8:11]))

    def test_single_visible_tip_needs_a_distinct_shaft_and_bounded_travel(self):
        a=np.column_stack((np.arange(101,dtype=float),np.zeros(101)))
        b=a.copy()
        b[-11:]=np.column_stack((np.full(11,1.),np.arange(10,-1,-1)))
        b[:11]+=[4,0]  # The competing tip is closer, but points across the shaft.
        self.assertEqual(visible_end_order(a,b,[True,False],[True,True],100),0)
        self.assertEqual(visible_end_order(b[::-1],a,[True,True],[True,False],100),1)
        b[-11:]=b[:11][::-1]
        self.assertIsNone(visible_end_order(a,b,[True,False],[True,True],100))
        self.assertIsNone(visible_end_order(a,b+100,[True,False],[True,True],100))

    def test_curled_turn_uses_two_way_motion_and_a_reproducible_reference(self):
        u=np.linspace(0,1,101)
        path=np.column_stack((35*u,48*np.sin(np.pi*u)))
        length=np.linalg.norm(np.diff(path,axis=0),axis=1).sum()
        for mode in ('clear','no_witness','contradiction','occluded','opposite_root'):
            with self.subTest(mode=mode):
                lines=np.stack([path+[20*np.clip(i-4,0,5),0] for i in range(15)])
                truth=lines.copy()
                features=np.ones((15,2,6))
                features[4:10,0]=0
                features[10:,1]=0
                if mode!='no_witness':features[12,1]=1
                if mode=='contradiction':features[13,1]=-1
                facts=[]
                for i in range(len(lines)):
                    known=i<4 or i>=10
                    segment=[0,3] if i<4 else [4,6] if i<7 else [7,9] if i<10 else [10,14]
                    if 4<=i<7 or (i>=10 and mode=='opposite_root'):
                        lines[i]=lines[i,::-1]
                        if i<7:features[i]*=-1
                    facts.append(dict(head_known=known,head_source='auto_taper_seed' if known else 'unresolved',
                                      component=segment,candidate='clear_observation',head_observed=True,
                                      tail_observed=True,anchor_conflict=False,length_px=length))
                if mode=='occluded':facts[6]['tail_observed']=False
                self.assertIsNone(visible_end_order(lines[6],lines[7],(True,True),(True,True),length))
                entries=recover_flanked_candidates(lines,facts,12,1,set(),None,features.__getitem__)
                if mode=='clear':
                    self.assertEqual(len(entries),2)
                    for e in entries:
                        self.assertTrue(e['accepted'] and e['candidate_only'])
                        self.assertEqual(e['trajectory_bridge']['frames'],[5,6,7,8])
                        self.assertEqual(e['supporting_reference'],12)
                        self.assertEqual(e['motion_orders'][0],e['motion_orders'][1])
                    np.testing.assert_array_equal(lines,truth)
                else:
                    self.assertFalse(any(e['accepted'] for e in entries))

    def test_candidate_continuation_compares_forward_and_reverse_orders(self):
        line=np.column_stack([np.linspace(0,30,101),np.zeros(101)])
        poses=np.stack([line+i for i in range(5)])
        facts=[dict(length_px=100) for _ in poses]
        poses[2]=poses[2,::-1]
        orders,_=continue_candidate_tip(poses,facts,2)
        self.assertEqual(orders,[1,1])
        poses[3:]=poses[3:,::-1]
        orders,_=continue_candidate_tip(poses,facts,2)
        self.assertEqual(orders,[1,0])
        poses[2]=[15,2]
        self.assertIsNone(continue_candidate_tip(poses,facts,2))

    def test_export_distinguishes_unknown_candidate_and_confirmed_ends(self):
        for labels in (['A','B'],['H?','T?'],['H','T']):
            view=dict(endpoint_labels=labels,quality={'legacy':False},identity_valid=True,posture_valid=True)
            with patch('result_overlay.present_frame',return_value=view),patch('result_overlay.cv2.circle') as circles:
                draw_worm(np.zeros((70,140,3),np.uint8),np.zeros((70,140),bool),
                          np.array([[24,35],[100,35]]),dict(worm_id=1))
            dots=[c.args for c in circles.call_args_list if c.args[3]!=(45,45,45)]
            self.assertEqual(len(dots),2)
            self.assertEqual(dots[0][3]==dots[1][3],labels[0]=='A')
            self.assertEqual([d[4] for d in dots],[-1,-1] if labels[0]=='H' else [2,2])
