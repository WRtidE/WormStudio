import unittest
from unittest.mock import patch

import cv2
import numpy as np

from blurred_tips import ExposureTraces, display_exposure_tip, exposure_traces
from frame_metrics import present_frame
from result_overlay import draw_pose_endpoint


def tip_exposure(angle=0, separation=44, single=False, first_share=.5):
    """Two exposures of a tapered arm sweeping linearly about a stationary shaft."""
    length=380.
    center=np.array([127.,123.])
    radians=np.radians(angle)
    rot=np.array([[np.cos(radians),-np.sin(radians)],[np.sin(radians),np.cos(radians)]])
    def xy(u,v):
        return center+rot@np.array([u,v])
    def pixels(point):
        return tuple(np.rint(point).astype(int))
    ends=np.array([xy(44,-separation/2),xy(44,separation/2)])
    fork=xy(0,0)
    bg=np.full((250,250),190,np.uint8)
    cv2.circle(bg,pixels(xy(52,-separation/2-11)),14,135,2)
    poses=[]
    for tip in ends:
        image=bg.copy()
        cv2.line(image,pixels(xy(-160,0)),pixels(fork),65,12)
        direction=tip-fork
        normal=np.array([-direction[1],direction[0]])/np.linalg.norm(direction)
        vertices=np.rint([fork+6*normal,tip+.8*normal,tip-.8*normal,fork-6*normal]).astype('int32')
        cv2.fillConvexPoly(image,vertices,65,cv2.LINE_AA)
        poses.append(image)
    image=poses[0] if single else cv2.addWeighted(poses[0],first_share,poses[1],1-first_share,0)
    distance=np.linalg.norm(ends[0]-fork)
    steps=np.arange(101)*length/100
    path=np.array([ends[0]+min(s,distance)*(fork-ends[0])/distance+max(0,s-distance)*(xy(-1,0)-fork) for s in steps])
    mid=ends.mean(axis=0)
    return image,bg,path,length,mid-(ends[1]-ends[0]),mid+(ends[1]-ends[0]),mid


class BlurredTipTests(unittest.TestCase):
    def test_estimate_is_closer_to_mid_exposure_for_rotated_sweeps(self):
        for angle in (0,39,95,170):
            for separation in (36,44,54):
                with self.subTest(angle=angle,separation=separation):
                    image,bg,*geometry,truth=tip_exposure(angle,separation)
                    found=display_exposure_tip(image,[bg]*4,*geometry,'tail')
                    self.assertIsNotNone(found)
                    mid,method,traces=found
                    self.assertEqual(method,'midpoint')
                    np.testing.assert_allclose(traces.tips.mean(axis=0),mid)
                    self.assertLess(np.linalg.norm(mid-truth),6)
                    self.assertLess(np.linalg.norm(mid-truth),np.linalg.norm(geometry[0][0]-truth))

    def test_tail_picks_either_darker_tip_but_head_retains_midpoint(self):
        for angle in (0,95,170):
            for share in (.3,.7):
                with self.subTest(angle=angle,share=share):
                    image,bg,*geometry,middle=tip_exposure(angle,first_share=share)
                    xy,method,traces=display_exposure_tip(image,[bg]*4,*geometry,'tail')
                    first=geometry[0][0]
                    target=first if share>.5 else 2*middle-first
                    self.assertEqual(method,'darker_tip')
                    self.assertLess(np.linalg.norm(xy-target),7.)
                    for side,refs in [('head',[bg]*4),('tail',[bg]*3)]:
                        mid,method,_=display_exposure_tip(image,refs,*geometry,side)
                        self.assertEqual(method,'midpoint')
                        np.testing.assert_allclose(mid,traces.tips.mean(axis=0))

    def test_leaveout_compares_physical_tip_and_abstains_if_selection_changes(self):
        image,bg,*geometry,_=tip_exposure()
        tips=np.array([[32.,55.],[32.,75.]])
        levels=np.array([[40.,20.],[39.,21.]])
        base=ExposureTraces(tips,levels,0)
        reordered=ExposureTraces(tips[::-1],levels[:,::-1],1)
        conflict=ExposureTraces(tips,levels[:,::-1],1)
        for alternate,method in [(reordered,'darker_tip'),(conflict,'midpoint'),(None,'midpoint')]:
            with patch('blurred_tips.exposure_traces',side_effect=[base,reordered,alternate,reordered,reordered]):
                xy,actual,_=display_exposure_tip(image,[bg]*4,*geometry,'tail')
            self.assertEqual(actual,method)
            np.testing.assert_allclose(xy,tips[0] if method=='darker_tip' else tips.mean(axis=0))

    def test_single_trace_static_or_crosswise_motion_has_no_midpoint(self):
        *args,truth=tip_exposure(single=True)
        self.assertIsNone(exposure_traces(*args))
        *args,truth=tip_exposure()
        for before,after in ((truth,truth),(truth-[45,0],truth+[45,0]),(truth+[0,80],truth+[0,160])):
            self.assertIsNone(exposure_traces(*args[:4],before,after))

    def test_two_sharp_dark_edges_do_not_become_an_exposure_estimate(self):
        image,bg,path,length,before,after,_=tip_exposure()
        # Keep the original fork geometry, but render both arms at full contrast.
        dark=bg.astype(float)-image
        mask=np.indices(image.shape)[1]>127
        image[mask]=np.clip(bg[mask]-2*dark[mask],0,255)
        self.assertIsNone(exposure_traces(image,bg,path,length,before,after))

    def test_overlay_marks_only_the_affected_end_as_estimated(self):
        for key in ('blur_midpoints','blur_darker_tips'):
            image=np.zeros((64,64,3),dtype=np.uint8)
            with patch('result_overlay.draw_endpoint') as draw:
                for label in ('H?','T?','A'):
                    draw_pose_endpoint(image,(12,14),label,{key:{'tail':[20.2,40.4]}})
            self.assertEqual([c.args[1:] for c in draw.call_args_list],
                             [((12,14),'H?'),((20,40),'T?~'),((12,14),'A')])
            self.assertEqual(bool(image[40,28,0]>image[40,28,2]),key=='blur_darker_tips')

    def test_historical_midpoints_and_new_tips_follow_same_presentation_gates(self):
        row=dict(worm_id=1,body_sequence_head_known=True,source_valid=True,mask_valid=True,
                 centerline_valid=True,identity_valid=True,head_detection_status='candidate',
                 display_blur_tail_x_px=20,display_blur_tail_y_px=40)
        self.assertEqual(present_frame(row)['blur_midpoints'],{'tail':[20,40]})
        row['display_blur_tail_method']='darker_tip'
        view=present_frame(row)
        self.assertEqual(view['blur_darker_tips'],{'tail':[20,40]})
        self.assertNotIn('blur_midpoints',view)
        self.assertIsNone(view['head_angle_deg'])
        for flag in ('body_sequence_head_known','source_valid','mask_valid','identity_valid'):
            self.assertNotIn('blur_darker_tips',present_frame({**row,flag:False}))
