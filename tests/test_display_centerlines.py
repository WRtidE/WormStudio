import unittest
import numpy as np
from display_centerlines import build_display,fill_display_gaps,inside_fraction,supported_curve,track_visible_tips
from quality_schema import VERSION

class DisplayTests(unittest.TestCase):
    def row(self,frame=0):
        return dict(worm_id=1,frame=frame,source_valid=True,mask_valid=True,identity_valid=False,
                    centerline_valid=False,measurement_mode='pixels',quality_schema_version=VERSION)
    def test_identity_review_keeps_geometry_and_never_unlocks(self):
        mask=np.zeros((30,120),bool);mask[8:23,5:115]=True
        p=np.column_stack([np.linspace(6,113,101),np.full(101,15.)]);r=self.row()
        result=build_display([r],{(1,0):p},{},lambda w,f:mask,lambda m:None)
        self.assertIn((1,0),result);self.assertEqual(r['display_source'],'geometry_review')
        self.assertFalse(r['identity_valid']);self.assertFalse(r['centerline_valid'])
        self.assertEqual(inside_fraction(result[(1,0)],mask),1.)

    def test_short_display_gap_uses_the_current_mask_and_preserves_measurement_status(self):
        rows = [dict(self.row(i),identity_valid=True,display_reason='no_supported_curve') for i in range(3)]
        mask = np.zeros((40,120),bool)
        mask[12:23,5:115] = True
        p = np.column_stack([np.linspace(10,110,101),np.full(101,15.)])
        output = {(1,0):p, (1,2):p[::-1]+[0,4]}
        fill_display_gaps(rows,output,lambda w,f:mask)
        self.assertEqual(rows[1]['display_source'],'temporal_mask_review')
        self.assertEqual(inside_fraction(output[1,1],mask),1.)
        self.assertFalse(rows[1]['centerline_valid'])
        del output[1,1]
        rows[1].update(display_reason='no_supported_curve',identity_valid=False)
        fill_display_gaps(rows,output,lambda w,f:mask)
        self.assertNotIn((1,1),output)
    def test_current_mask_refit_and_empty_source(self):
        mask=np.ones((20,100),bool);p=np.column_stack([np.linspace(2,98,101),np.full(101,10.)])
        r=self.row();r['mask_valid']=False
        result=build_display([r],{}, {},lambda w,f:mask,lambda m:(p,None,None))
        self.assertEqual(r['display_source'],'mask_refit');self.assertFalse(r['mask_valid'])
        r=self.row();r['source_valid']=False
        self.assertFalse(build_display([r],{}, {},lambda w,f:mask,lambda m:(p,None,None)))
    def test_distant_or_cross_background_curve_is_rejected(self):
        mask=np.zeros((40,100),bool);mask[5:15,:]=True
        p=np.column_stack([np.linspace(0,99,101),np.full(101,30.)])
        self.assertIsNone(supported_curve(p,mask))
        # Endpoints inside is insufficient if the connecting segment crosses a hole.
        mask[:]=True;mask[:,45:55]=False
        self.assertIsNone(supported_curve(np.array([[5.,10.],[95.,10.]]),mask))

    def test_implausible_length_and_local_fold_rejected(self):
        mask=np.ones((200,200),bool)
        self.assertIsNone(supported_curve(np.array([[10.,10.],[12.,10.]]),mask))
        self.assertIsNone(supported_curve(np.array([[10.,10.],[180.,10.]]),mask,100.))
        fold=np.array([[10.,10.],[110.,10.],[10.,11.],[110.,11.]])
        self.assertIsNone(supported_curve(fold,mask))
        straight=np.column_stack([np.linspace(10,110,101),np.full(101,50.)])
        self.assertIsNotNone(supported_curve(straight,mask,100.))

    def test_independent_tip_can_survive_without_a_display_line(self):
        from frame_metrics import present_frame
        from result_overlay import draw_worm
        from unittest.mock import patch
        mask = np.zeros((80,160), bool)
        mask[40,30:101] = True
        mask[40,129:132] = True  # Disconnected speck at the hidden tip.
        p = np.array([[30.,40.],[130.,40.]])
        for reverse, side in ((False,'head'), (True,'tail')):
            with self.subTest(side=side):
                rows = [dict(self.row(i), identity_valid=True, identity_segment=1, display_source='formal',
                             body_sequence_head_known=True) for i in range(3)]
                rows[1].update(display_source='unavailable', body_sequence_head_known=False,
                               head_reason='no_supported_body_path')
                line = p[::-1] if reverse else p
                track_visible_tips(rows, {(1,0):line, (1,2):line}, lambda w,i:mask, 12)
                frame = present_frame(rows[1])
                self.assertEqual(frame['display_tips'], {side:[30.,40.]})
                self.assertFalse(frame['head_confirmed'])
                self.assertFalse(frame['posture_valid'])
                with patch('result_overlay.cv2.putText') as put:
                    draw_worm(np.zeros((*mask.shape,3), np.uint8), mask, None, rows[1])
                self.assertEqual([call.args[1] for call in put.call_args_list if call.args[1] in
                                  ('H?', 'T?', 'A', 'B')], ['H?' if side == 'head' else 'T?'])

    def test_ambiguous_or_interrupted_tip_tracks_are_not_assigned(self):
        from frame_metrics import present_frame
        mask = np.zeros((80,160), bool)
        mask[40,30:131] = True
        p = np.array([[30.,40.],[130.,40.]])
        for change in ('swap','identity','conflict','time','hidden'):
            with self.subTest(change=change):
                rows = [dict(self.row(i), identity_valid=True, identity_segment=1, display_source='formal',
                             body_sequence_head_known=True) for i in range(3)]
                rows[1].update(display_source='unavailable', body_sequence_head_known=False,
                               head_reason='no_supported_body_path')
                if change == 'identity': rows[1]['identity_segment'] = 2
                if change == 'conflict': rows[2]['head_anchor_conflict'] = True
                if change == 'hidden': rows[0]['head_display_estimated_end'] = 0
                track_visible_tips(rows, {(1,0):p, (1,2):p[::-1] if change=='swap' else p},
                                   lambda w,i:mask, 5 if change=='time' else 12)
                self.assertEqual(present_frame(rows[1]).get('display_tips', {}),
                                 {'tail':[130.,40.]} if change=='hidden' else {})
                if change == 'hidden':
                    self.assertEqual(present_frame(rows[0])['display_tips'], {'tail':[130.,40.]})

    def test_two_missing_poses_keep_only_the_tips_visible_in_each_frame(self):
        from frame_metrics import present_frame
        rows = [dict(self.row(i), identity_valid=True, identity_segment=1,
                     body_sequence_head_known=i in (0,3), display_source='formal' if i in (0,3) else 'unavailable',
                     head_reason='initial_mark' if i in (0,3) else 'no_supported_body_path') for i in range(4)]
        first = np.zeros((80,160), bool)
        first[40,30:116] = True
        second = np.zeros_like(first)
        second[40,42:131] = True
        p = np.array([[30.,40.],[130.,40.]])
        track_visible_tips(rows, {(1,0):p, (1,3):p}, lambda w,i:first if i==1 else second, 12)
        self.assertEqual(present_frame(rows[1])['display_tips'], {'head':[30.,40.]})
        self.assertEqual(present_frame(rows[2])['display_tips'], {'tail':[130.,40.]})

if __name__=='__main__':unittest.main()
