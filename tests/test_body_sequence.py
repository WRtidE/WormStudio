import unittest
from unittest.mock import patch

import numpy as np
import cv2

from body_sequence import select_body_sequence,loop_transition,persistent_loop_centers
from body_sequence_solver import recover_short_observations, flow_supports_order, reseed_clear_components
from head_recovery import tracking_indices


class BodySequenceTests(unittest.TestCase):
    def test_local_reseed_needs_consistent_visible_ends_and_one_identity(self):
        path=np.column_stack((np.arange(101)+10,np.full(101,20)))
        mask=np.zeros((40,125),bool);mask[15:25,5:116]=True
        for votes,identity,accepted in [([1,1,None,1],True,True),([0,1,None,1],True,False),
                                        ([1,None,None,1],True,False),([1,1,None,1],False,False)]:
            with self.subTest(votes=votes,identity=identity):
                paths=np.stack([path]*4)
                facts=[dict(head_known=False,anchor_conflict=False,candidate='clear_observation',
                            head_observed=True,tail_observed=True) for _ in paths]
                guards=[dict(identity_valid=True,mask_valid=True,identity_segment=int(not identity and i>1))
                        for i in range(4)]
                with patch('body_sequence_solver.taper_side',side_effect=votes):
                    reseed_clear_components(paths,facts,[mask]*4,[(0,3)],guards)
                self.assertEqual([r['head_known'] for r in facts],[accepted]*4)
                np.testing.assert_array_equal(paths[0],path[::-1] if accepted else path)
                self.assertFalse(any(f.get('manual_lineage') for f in facts))

    def test_short_visible_pose_requires_matching_tips_between_clear_frames(self):
        def line(x, right):
            return np.column_stack((np.linspace(x, right, 101), np.full(101, 45.)))
        anchor = line(25, 125)
        short = line(31, 119)
        mask = np.zeros((90, 155), np.uint8)
        cv2.line(mask, (31, 45), (119, 45), 1, 9)
        masks = [mask.astype(bool)] * 3
        clear = dict(name='clear_observation', length_px=100.)
        for case, point, right, fps, accepted in [
                ('reverse storage', short[::-1], anchor[::-1], 12, True),
                ('missing source', None, anchor, 12, False),
                ('unrelated shape', short+[0, 16], anchor, 12, False),
                ('missing tip', line(47, 119), anchor, 12, False),
                ('long interval', short, anchor, 5, False)]:
            with self.subTest(case=case):
                frames = [[clear], [], [clear]]
                recover_short_observations(frames, [anchor, point, right], masks, 120., fps)
                self.assertEqual(bool(frames[1]), accepted)
                if accepted:
                    self.assertEqual(frames[1][0]['name'], 'short_observation')
                    np.testing.assert_array_equal(frames[1][0]['points'], point)

    def test_single_geometry_vote_needs_image_tracks_at_both_ends(self):
        indices = tracking_indices()
        line = np.column_stack((np.linspace(25, 125, 101), np.full(101, 45.)))
        masks = np.ones((3, 90, 155), bool)
        gray = masks.astype(np.uint8)
        for case, reliable, last, wanted in [
                ('both quarters', (indices <= 15) | (indices >= 85), line, True),
                ('one quarter', indices >= 77, line, False),
                ('order disagreement', np.ones(len(indices), bool), line[::-1], False)]:
            with self.subTest(case=case), patch('motion.lk_step',
                    side_effect=lambda a,b,p:(p.copy(), reliable.copy(), np.zeros(len(p)))):
                self.assertEqual(flow_supports_order(gray, masks, 0, 2, line, last, indices), wanted)
        masks[1] = False
        with patch('motion.lk_step', side_effect=lambda a,b,p:(p, np.ones(len(p), bool), np.zeros(len(p)))):
            self.assertFalse(flow_supports_order(gray, masks, 0, 2, line, line, indices))

    def test_a_mark_inside_the_sequence_orients_both_earlier_and_later_frames(self):
        base=np.column_stack([np.linspace(20,120,101),np.full(101,70.)])
        frames=[[dict(points=base[::-1],unary=0.)],[dict(points=base[::-1],unary=0.)],
                [dict(points=base,unary=0.)]]
        masks=[np.ones((150,170),bool)]*3
        def missing(a,b,p):
            return p,np.zeros(len(p),bool),np.full(len(p),100.)
        with patch('body_sequence.lk_step',side_effect=missing):
            selected,_=select_body_sequence(frames,[np.zeros((150,170),np.uint8)]*3,masks,5,
                                            anchor=(1,base[0]))
        for state in selected:np.testing.assert_allclose(state['points'],base)

    def test_same_endpoints_cannot_switch_winding_around_a_persistent_loop(self):
        center=np.array([70.,70.])
        def arc(stop,rotation=0):
            a=np.linspace(.4,stop,101)+rotation
            return center+np.column_stack([25*np.cos(a),25*np.sin(a)])
        first=arc(6.2)
        wrong=arc(6.2-4*np.pi)
        np.testing.assert_allclose(first[[0,-1]],wrong[[0,-1]])
        blocked,_,_=loop_transition([dict(points=first)],
            [dict(points=wrong),dict(points=arc(6.2,.1))],center)
        self.assertEqual(blocked.tolist(),[[True,False]])

    def test_holes_must_persist_in_both_frames_to_constrain_a_loop(self):
        yy,xx=np.mgrid[:140,:140]
        ring=((xx-70)**2+(yy-70)**2<35**2)&((xx-70)**2+(yy-70)**2>25**2)
        self.assertEqual(len(persistent_loop_centers(ring,ring,5)),1)
        opened=ring.copy();opened[:71,68:73]=False
        self.assertEqual(persistent_loop_centers(ring,opened,5),[])

    def test_future_geometry_and_image_motion_keep_a_marked_end_when_storage_reverses(self):
        base=np.column_stack([np.linspace(20,120,101),70+5*np.sin(np.linspace(0,3,101))])
        shifts=[0,3,6,5,3,0,-2]
        frames=[]
        for i,x in enumerate(shifts):
            p=base+[x,0]
            if i in (2,3,5):p=p[::-1]
            cs=[dict(points=p,unary=0.)]
            # A wrong body position fits this deliberately broad synthetic mask.
            if i in (2,3,4):cs.insert(0,dict(points=p+[0,25],unary=-.1))
            frames.append(cs)
        images=[np.full((150,170),i,np.uint8) for i in range(len(shifts))]
        masks=[np.ones((150,170),bool) for _ in shifts]
        def flow(a,b,p):
            delta=shifts[int(b[0,0])]-shifts[int(a[0,0])]
            return p+[delta,0],np.ones(len(p),bool),np.zeros(len(p))
        with patch('body_sequence.lk_step',side_effect=flow):
            selected,_=select_body_sequence(frames,images,masks,5)
        for i,s in enumerate(selected):
            np.testing.assert_allclose(s['points'],base+[shifts[i],0])

    def test_unknown_flow_leaves_choice_to_sequence_geometry(self):
        base=np.column_stack([np.linspace(20,120,101),np.full(101,70.)])
        frames=[[dict(points=base,unary=0.)],[dict(points=base[::-1],unary=0.)],
                [dict(points=base,unary=0.)]]
        masks=[np.ones((150,170),bool)]*3
        def missing(a,b,p):
            return p,np.zeros(len(p),bool),np.full(len(p),100.)
        with patch('body_sequence.lk_step',side_effect=missing):
            selected,_=select_body_sequence(frames,[np.zeros((150,170),np.uint8)]*3,masks,5)
        for state in selected:np.testing.assert_allclose(state['points'],base)
