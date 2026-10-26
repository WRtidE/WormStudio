import unittest
import numpy as np
from wormstudio_single.contact import select_event,recover

class ContactChecks(unittest.TestCase):
    def test_future_pose_changes_the_earlier_branch(self):
        p=np.column_stack([np.linspace(0,100,101),np.zeros(101)])
        state=lambda y:dict(points=p+[0,y],unary=0.)
        for sign in [1,-1]:
            choices=[[state(0)],[state(2),state(-2)],[state(4),state(-4)],[state(sign*6)]]
            selected,_=select_event(choices,100)
            self.assertEqual(selected[1:3],[0,0] if sign==1 else [1,1])
