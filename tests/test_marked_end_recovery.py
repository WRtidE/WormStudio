import unittest
from unittest.mock import patch
import numpy as np

from body_sequence_solver import head_results, solve
from marked_end_recovery import motion_match
from terminal_overlap import match_shaft


def worm_frame(shift, partial=None, mirror=False):
    x = 42+shift
    yy,xx = np.indices((100,240))
    u = (xx-x)/120.
    width = 2 + 7*np.sin(np.pi*np.clip(u,0,1))**.6 + 4*(1-u)
    mask = (u >= 0) & (u <= 1) & (abs(yy-50) < width)
    if partial == 'head':
        mask &= u < .30
    if partial == 'tail':
        mask &= u > .70
    gray = np.where(mask,55+85*u,210).clip(0,255).astype('uint8')
    if mirror:
        mask,gray = mask[:,::-1].copy(),gray[:,::-1].copy()
    return mask,gray


class MarkedEndRecoveryTests(unittest.TestCase):
    def test_adjacent_components_rejoin_by_the_same_tip_in_both_directions(self):
        from marked_end_recovery import recover_marked_end
        lines=np.array([np.column_stack([np.arange(101)+i,np.zeros(101)]) for i in range(9)])
        lines[7:]=lines[7:,::-1]
        facts=[dict(component=[0,6] if i<7 else [7,8],manual_lineage=i<7,
                    anchor_frames=[2] if i<7 else [],anchor_conflict=False,
                    head_observed=True,tail_observed=True,length_px=100,candidate='clear_observation')
               for i in range(9)]
        with patch('marked_end_recovery.fingerprint',return_value=np.zeros((2,6))), \
             patch('marked_end_recovery.appearance_vote',return_value=0):
            audit=recover_marked_end(lines,facts,[None]*9,[None]*9,12,[dict(frame=2)],set(),None)
        self.assertEqual(audit[0]['reason'],'reciprocal_marked_end')
        self.assertTrue(audit[0]['reciprocal_ends'])
        self.assertEqual(lines[7,0,0],7)
        self.assertEqual(facts[8]['anchor_frames'],[2])

    def test_stationary_shaft_recovers_after_tip_slides_then_stops(self):
        for end in ('head','tail'):
            for mark in (2,13):
                with self.subTest(end=end,mark=mark):
                    masks,images=[],[]
                    vertical = mark == 13
                    for i in range(16):
                        shift = 0 if i<7 else 12 if i==7 else 24
                        mask,image = worm_frame(shift)
                        if i==7:
                            if end=='head':
                                mask[:,125:] = False
                            else:
                                mask[:,:104] = False
                            image = np.where(mask,image,210).astype('uint8')
                        masks.append(mask.T.copy() if vertical else mask)
                        images.append(image.T.copy() if vertical else image)
                    x = 66 if mark==13 else 42
                    lines,facts,audit=solve(masks,images,15,manual=dict(frame=mark,point=[50,x] if vertical else [x,50]))
                    link, = audit['head_reacquisition']
                    self.assertTrue(link['accepted'])
                    self.assertEqual(link['reason'],'marked_end_shaft_and_appearance')
                    self.assertEqual(set(link['tracked_ends']),{end})
                    shaft=link['tracked_ends'][end]['shaft_overlap']
                    self.assertEqual(set(shaft['frames']),{6,7,8})
                    self.assertEqual(shaft['observed_gap_tips'][0]['frame'],7)
                    self.assertTrue(np.isnan(lines[7]).all())
                    for i,head in enumerate(head_results([dict(body_sequence=f) for f in facts])):
                        self.assertEqual(head['head_identity_confirmed'],i!=7)
                        if i!=7:
                            self.assertLess(abs(lines[i,0,int(vertical)]-(42 if i<7 else 66)),10)

    def test_overlap_requires_the_same_unique_run_in_every_gap_frame(self):
        path = np.column_stack([np.arange(101),np.zeros(101)])
        lines = np.stack([path]*6)
        arm = path[:33]
        crossing = np.column_stack([np.full(33,15),np.arange(-16,17)])
        self.assertEqual(match_shaft(lines,1,4,0,{2:[arm],3:[arm]},100)[0],0)
        for competing in ([arm,arm[::-1]+[0,1]],[crossing],[]):
            with self.subTest(branch_count=len(competing)):
                branches={2:[arm],3:competing}
                self.assertIsNone(match_shaft(lines,1,4,0,branches,100))
        # Each gap contains a local match, but on disjoint lengths of the shaft.
        self.assertIsNone(match_shaft(lines,1,4,0,{2:[arm[:12]],3:[arm[19:]]},100))

    def test_conflicting_tip_and_shaft_matches_veto_recovery(self):
        path = np.column_stack([np.arange(101),np.zeros(101)])
        lines=np.stack([path]*4)
        facts=[dict(head_observed=True,tail_observed=True,length_px=100) for _ in lines]
        with patch('marked_end_recovery.match_tip',return_value=(1,dict(observed_gap_tips=[]))):
            flip,evidence=motion_match(lines,facts,[],1,2,0)
        self.assertIsNone(flip)
        self.assertEqual(evidence['reason'],'motion_and_shaft_disagree')
        self.assertEqual(evidence['shaft_side'],0)

    def test_visible_head_or_tail_reorients_next_track_without_measuring_gap(self):
        for end in ('head','tail'):
            for mark in (2,13):
                with self.subTest(end=end,mark=mark):
                    mirror = mark == 13
                    samples = [worm_frame(i,end if i in (7,8) else None,mirror) for i in range(16)]
                    masks,images = zip(*samples)
                    def head_x(i):
                        return 239-(42+i) if mirror else 42+i
                    lines,facts,audit = solve(masks,images,15,manual=dict(frame=mark,point=[head_x(mark),50]))
                    self.assertTrue(np.isnan(lines[7:9]).all())
                    links = [a for a in audit['head_reacquisition'] if a['accepted']]
                    self.assertEqual(len(links),1)
                    self.assertEqual(set(links[0]['tracked_ends']),{end})
                    self.assertEqual({p['frame'] for p in links[0]['tracked_ends'][end]['observed_gap_tips']},{7,8})
                    heads = head_results([dict(body_sequence=f) for f in facts])
                    for i,h in enumerate(heads):
                        self.assertEqual(h['head_identity_confirmed'],i not in (7,8))
                        if i in (7,8):
                            continue
                        self.assertLess(abs(lines[i,0,0]-head_x(i)),10)
                        self.assertEqual(facts[i]['anchor_frames'],[mark])
                    self.assertEqual(heads[12 if mark==2 else 2]['head_status'],'recovered_from_user_point')

    def test_appearance_alone_or_motion_alone_cannot_release_measurements(self):
        samples = [worm_frame(i,'head' if i==7 else None) for i in range(15)]
        for mode in ('missing_tip','opposite_appearance','source_excluded','identity_break','invalid_identity','no_mark'):
            with self.subTest(mode=mode):
                masks,images = map(list,zip(*samples))
                marks = None if mode=='no_mark' else dict(frame=2,point=[44,50])
                guards = [dict(mask_valid=True,identity_valid=True,identity_segment=int(mode=='identity_break' and i>=8))
                          for i in range(15)]
                if mode=='invalid_identity':
                    guards[7]['identity_valid']=False
                if mode=='missing_tip':
                    masks[7]=np.zeros_like(masks[7])
                if mode=='opposite_appearance':
                    for i in range(8,15):
                        # Reverse optical contrast while preserving motion and silhouette.
                        images[i]=np.where(masks[i],195-images[i],210).astype('uint8')
                _,facts,audit = solve(masks,images,15,manual=marks,guards=guards,
                    source_invalid=[7] if mode=='source_excluded' else [])
                self.assertFalse(any(a['accepted'] for a in audit['head_reacquisition']))
                heads = head_results([dict(body_sequence=f) for f in facts])
                self.assertFalse(any(h['head_identity_confirmed'] for h in heads[8:]))

    def test_singleton_conflicting_marks_block_orientation(self):
        mask,image = worm_frame(0)
        _,facts,_=solve([mask],[image],15,manual=[dict(frame=0,point=[42,50]),dict(frame=0,point=[162,50])])
        self.assertTrue(facts[0]['anchor_conflict'])
        self.assertFalse(facts[0]['manual_lineage'])

    def test_opposing_human_tracks_cannot_release_the_middle_component(self):
        samples = [worm_frame(i,'head' if i in (7,15) else None) for i in range(23)]
        masks,images = zip(*samples)
        marks = [dict(frame=2,point=[44,50]),dict(frame=19,point=[181,50])]
        _,facts,audit = solve(masks,images,15,manual=marks)
        self.assertTrue(all(f['anchor_conflict'] and not f['head_known'] for f in facts[8:15]))
        self.assertEqual({r['reason'] for r in audit['head_reacquisition']},{'opposing_marked_tracks'})

    def test_two_frame_passage_needs_support_on_its_far_side(self):
        samples = [worm_frame(i,'tail' if i==7 else None) for i in range(10)]
        masks,images = zip(*samples)
        _,facts,audit = solve(masks,images,15,manual=dict(frame=2,point=[44,50]))
        self.assertEqual(audit['head_reacquisition'][0]['reason'],'awaiting_more_clear_frames')
        self.assertTrue(all(not f['manual_lineage'] for f in facts[8:]))
