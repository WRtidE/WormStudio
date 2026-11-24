"""Single-worm recovery must reduce missing paths without fabricating confirmation."""
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from body_sequence_solver import solve, head_results, candidates_for, recover_native_observations
from framewise_pose import observed_pose
from review_intervals import build_intervals
from quality_schema import VERSION


def straight(shift=0):
    mask=np.zeros((180,500),np.uint8)
    cv2.line(mask,(40+shift,90),(160+shift,90),1,13)
    return mask>0, np.where(mask,60,220).astype('uint8')


class SingleRecoveryTests(unittest.TestCase):
    def test_first_link_break_keeps_the_following_continuous_segment_together(self):
        samples=[straight(x) for x in (0,200,200)]
        _,facts,audit=solve([s[0] for s in samples],[s[1] for s in samples],30,
                            manual=dict(frame=0,point=[35,90]))
        self.assertEqual(facts[0]['component'],[0,0])
        self.assertEqual(facts[1]['component'],[1,2])
        self.assertEqual(facts[2]['component'],[1,2])
        self.assertEqual([w['rejected_at'] for w in audit['windows'] if 'rejected_at' in w],[1])

    def test_large_unsupported_jump_cannot_carry_manual_confirmation(self):
        samples=[straight(x) for x in (0,0,200,200)]
        lines,facts,audit=solve([s[0] for s in samples],[s[1] for s in samples],30,
                              manual=dict(frame=0,point=[35,90]))
        self.assertTrue(np.isfinite(lines).all())
        self.assertNotEqual(facts[1]['component'],facts[2]['component'])
        heads=head_results([dict(body_sequence=f) for f in facts])
        self.assertTrue(heads[0]['head_identity_confirmed'])
        self.assertTrue(all(not h['head_identity_confirmed'] for h in heads[2:]))
        self.assertTrue(any(w.get('reason')=='body_correspondence_break' for w in audit['windows']))

    def test_conflicting_marks_do_not_flip_a_stationary_worm(self):
        mask,gray=straight()
        lines,facts,_=solve([mask]*6,[gray]*6,30,manual=[dict(frame=0,point=[35,90]),dict(frame=5,point=[165,90])])
        self.assertTrue(all(f['anchor_conflict'] for f in facts))
        self.assertTrue(all(not h['head_identity_confirmed'] for h in head_results([dict(body_sequence=f) for f in facts])))
        np.testing.assert_allclose(lines,np.repeat(lines[:1],6,axis=0))

    def test_native_retry_needs_two_sided_images_and_cannot_cross_source_exclusion(self):
        mask,gray=straight();p,_=observed_pose(mask)
        reference=np.linalg.norm(np.diff(p,axis=0),axis=1).sum()
        for case,images,excluded,accepted in [
                ('supported',[gray]*3,set(),True),
                ('blank left',[np.zeros_like(gray),gray,gray],set(),False),
                ('blank right',[gray,gray,np.zeros_like(gray)],set(),False),
                ('excluded',[gray]*3,{1},False)]:
            with self.subTest(case=case):
                frames=[candidates_for(mask,reference,p)[0],[],candidates_for(mask,reference,p)[0]]
                observed=[p,None,p]
                attempts=recover_native_observations(frames,observed,[mask]*3,images,reference,12,excluded,13)
                self.assertEqual(bool(frames[1]),accepted)
                self.assertEqual(sum(a['accepted'] for a in attempts),int(accepted))
                if accepted:
                    self.assertEqual(frames[1][0]['recovery_anchors'],[0,2])
                    np.testing.assert_allclose(observed[1],observed_pose(mask,max_size=None)[0])

    def test_existing_candidates_are_never_replaced_by_native_retry(self):
        mask,gray=straight();p,_=observed_pose(mask)
        reference=np.linalg.norm(np.diff(p,axis=0),axis=1).sum()
        choices=candidates_for(mask,reference,p)[0]
        frames=[choices,choices,choices]
        with patch('body_sequence_solver.observed_pose',side_effect=AssertionError('unneeded retry')):
            attempts=recover_native_observations(frames,[p]*3,[mask]*3,[gray]*3,reference,12,set(),13)
        self.assertEqual(attempts,[])


def review_row(frame,**updates):
    row=dict(frame=frame,worm_id=1,quality_schema_version=VERSION,source_valid=True,mask_valid=True,
             pose_valid=True,identity_valid=True,scale_valid=False,measurement_mode='pixels',
             head_valid=False,head_identity_confirmed=False,centerline_topology='simple',
             body_extension=.4,centerline_inside_fraction=1.,identity_segment=0,body_sequence_segment=0)
    row.update(updates)
    return row


class ReviewIntervalTests(unittest.TestCase):
    def test_tracked_candidates_only_request_direction_review_on_conflict(self):
        rows=[review_row(i,body_sequence_version='current',body_sequence_head_known=True,
                         head_reason=reason) for i,reason in enumerate(
                         ('multi_frame_appearance_candidate','bidirectional_tip_candidate','candidate_motion_conflict'))]
        task,=build_intervals(rows)['intervals']
        self.assertEqual((task['kind'],task['target']),('head_motion_conflict',2))
        self.assertFalse(any(r['head_identity_confirmed'] for r in rows))

    def test_selects_clear_keyframe_and_separates_evidence_boundaries(self):
        rows=[review_row(i,body_extension=.8 if i==2 else .4,body_sequence_segment=0 if i<4 else 4) for i in range(7)]
        report=build_intervals(rows)
        self.assertEqual([(t['start'],t['end'],t['target']) for t in report['intervals']],[(0,3,2),(4,6,4)])
        self.assertEqual(report['head_confirmation_intervals'],2)
        self.assertEqual(report['intervals'][0]['after'],4)
        self.assertFalse(any(r['head_identity_confirmed'] for r in rows))

    def test_excluded_contact_and_conflict_frames_are_not_head_confirmation_targets(self):
        rows=[review_row(0),review_row(1,source_valid=False),
              review_row(2,centerline_topology='paired_contact',endpoint_a_inferred=True),
              review_row(3,head_anchor_conflict=True),review_row(4)]
        report=build_intervals(rows)
        tasks=report['intervals']
        self.assertEqual([t['action'] for t in tasks],['confirm_head','inspect','inspect','inspect_anchors','confirm_head'])
        self.assertEqual([t['target'] for t in tasks if t['action']=='confirm_head'],[0,4])

    def test_completed_confirmation_removes_task_and_other_worms_stay_separate(self):
        rows=[review_row(0,head_valid=True,head_identity_confirmed=True),review_row(0,worm_id=2),
              review_row(1,head_valid=True,head_identity_confirmed=True),review_row(1,worm_id=2)]
        report=build_intervals(rows)
        self.assertEqual([(t['worm_id'],t['start'],t['end']) for t in report['intervals']],[(2,0,1)])

    def test_head_geometry_conflict_groups_frames_without_requesting_a_new_head_mark(self):
        methods=['centerline_chords','review_geometry','review_geometry','review_geometry','section_fit']
        rows=[review_row(i,head_angle_method=method,head_angle_block=int(i>0),
                         head_valid=True,head_identity_confirmed=True) for i,method in enumerate(methods)]
        task,=build_intervals(rows)['intervals']
        self.assertEqual((task['kind'],task['action'],task['start'],task['target'],task['end']),
                         ('head_geometry','inspect',1,2,3))
        self.assertIsNone(task['before'])
        self.assertEqual(task['after'],4)
