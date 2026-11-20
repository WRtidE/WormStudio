import unittest
from unittest.mock import patch

import numpy as np

from pose.body_sequence import select_body_sequence,loop_transition,persistent_loop_centers


class BodySequenceTests(unittest.TestCase):
    def test_a_mark_inside_the_sequence_orients_both_earlier_and_later_frames(self):
        base=np.column_stack([np.linspace(20,120,101),np.full(101,70.)])
        frames=[[dict(points=base[::-1],unary=0.)],[dict(points=base[::-1],unary=0.)],
                [dict(points=base,unary=0.)]]
        masks=[np.ones((150,170),bool)]*3
        def missing(a,b,p):
            return p,np.zeros(len(p),bool),np.full(len(p),100.)
        with patch('pose.body_sequence.lk_step',side_effect=missing):
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
        with patch('pose.body_sequence.lk_step',side_effect=flow):
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
        with patch('pose.body_sequence.lk_step',side_effect=missing):
            selected,_=select_body_sequence(frames,[np.zeros((150,170),np.uint8)]*3,masks,5)
        for state in selected:np.testing.assert_allclose(state['points'],base)
