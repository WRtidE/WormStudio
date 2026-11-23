import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import unittest
import cv2
import numpy as np
from multiworm_quality import frame_geometry,sequence_guards


def worm(a,b):
    m=np.zeros((120,160),np.uint8)
    cv2.line(m,a,b,1,7)
    return m.astype(bool)


class MultiwormQualityTests(unittest.TestCase):
    def test_separate_worms_are_not_contacts(self):
        q=frame_geometry({1:worm((20,25),(90,25)),2:worm((20,65),(90,65))})
        self.assertEqual(q[1]['contact_ids'],[])
        self.assertEqual(q[2]['contact_ids'],[])

    def test_crossing_is_flagged_symmetrically(self):
        q=frame_geometry({1:worm((20,50),(110,50)),2:worm((70,20),(70,90))})
        self.assertEqual(q[1]['contact_ids'],[2])
        self.assertEqual(q[2]['contact_ids'],[1])
        self.assertGreater(q[1]['overlap_pixels'],0)

    def test_identity_is_not_silently_confirmed_after_contact(self):
        a=worm((20,50),(110,50));b=worm((70,20),(70,90));c=worm((20,100),(110,100))
        q=sequence_guards({1:np.array([a,a,a]),2:np.array([c,b,c])})
        self.assertTrue(q[1][0]['mask_valid'])
        self.assertFalse(q[1][1]['mask_valid'])
        self.assertTrue(q[1][2]['mask_valid'])
        self.assertTrue(q[1][2]['identity_review'])

    def test_self_contact_does_not_invent_other_object(self):
        m=worm((25,25),(90,25))
        cv2.circle(m.view(np.uint8),(70,55),25,1,7)
        q=frame_geometry({1:m})
        self.assertEqual(q[1]['contact_ids'],[])

    def test_border_and_fragmentation(self):
        m=worm((0,20),(50,20))|worm((80,70),(140,70))
        q=frame_geometry({1:m})[1]
        self.assertTrue(q['fragmented'])
        self.assertTrue(q['boundary_truncated'])

    def test_existing_bad_mask_is_not_reenabled(self):
        m=worm((20,30),(90,30))
        q=sequence_guards({1:np.array([m,m])},{(0,1):True})
        self.assertFalse(q[1][0]['mask_valid'])
        self.assertTrue(q[1][1]['mask_valid'])

    def test_corrupt_frame_does_not_poison_later_identity(self):
        normal=worm((20,30),(80,30));false=worm((80,90),(145,90))
        q=sequence_guards({1:np.array([normal,false,normal])},source_invalid=[1])
        self.assertFalse(q[1][1]['mask_valid'])
        self.assertFalse(q[1][2]['identity_review'])
        self.assertEqual(q[1][2]['centroid_step_px'],0.)

    def test_false_overlap_in_corrupt_frame_is_not_a_contact_event(self):
        a=worm((20,30),(100,30));b=worm((20,80),(100,80))
        q=sequence_guards({1:np.array([a,a,a]),2:np.array([b,a,b])},source_invalid=[1])
        self.assertEqual(q[1][1]['contact_ids'],[])
        self.assertFalse(q[1][2]['identity_review'])
        self.assertFalse(q[2][2]['identity_review'])

    def test_same_speed_is_valid_at_both_frame_rates(self):
        # 360 px/s: 12 px at 30 fps, 30 px at 12 fps.
        first=worm((20,30),(80,30))
        for fps,step in [(30,12),(12,30)]:
            moved=worm((20+step,30),(80+step,30))
            q=sequence_guards({1:np.array([first,moved])},fps=fps)
            self.assertFalse(q[1][1]['identity_review'])
            self.assertAlmostEqual(q[1][1]['centroid_step_speed_px_s'],360.)

    def test_large_jump_is_still_flagged_at_low_frame_rate(self):
        first=worm((15,25),(75,25));moved=worm((65,90),(125,90))
        for fps in [12,30]:
            q=sequence_guards({1:np.array([first,moved])},fps=fps)
            self.assertTrue(q[1][1]['identity_review'])


if __name__=='__main__':unittest.main()
