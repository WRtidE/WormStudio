"""Pixel measurements and recoverable workspace operations, not biological accuracy."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import shutil
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.request

import cv2
import numpy as np
import manual_worm_annotation as manual
import analyze_phenotypes as metrics
import quality_stage
import worm_pipeline
from quality_fixture import make_fixture
from quality_schema import VERSION, permissions
from frame_metrics import present_frame
from measurement_units import validate_scale, pixel_fields
from pipeline_progress import PipelineProgress
from workspace_sessions import save_draft, get_draft, load_config, confirm_head
from worm_pipeline import run_stage
from video_timing import verify_encoded, verify_timebase


class SingleDeliveryTests(unittest.TestCase):
    def test_encoded_video_checks_all_frames_and_reports_progress(self):
        with patch('video_timing.cv2.VideoCapture') as capture:
            decoder = capture.return_value
            decoder.get.side_effect = {cv2.CAP_PROP_FRAME_WIDTH: 128,
                cv2.CAP_PROP_FRAME_HEIGHT: 80, cv2.CAP_PROP_FPS: 12}.__getitem__
            decoder.read.side_effect = [(True, None)] * 3 + [(False, None)]
            progress = Mock()
            size = verify_encoded('clip', dict(frames=3, fps=12), (127, 79), progress=progress)
            self.assertEqual(size, (128, 80))
            self.assertEqual([call.args for call in progress.call_args_list], [(1, 3), (2, 3), (3, 3)])
            decoder.release.assert_called_once_with()

    def test_encoded_video_validation_closes_decoder_on_every_failure(self):
        cases = [('open', 'Cannot open'), ('size', 'resolution'), ('fps', 'frame rate'),
                 ('short', 'frame count'), ('extra', 'frame count'), ('decode', 'decode failed'),
                 ('cancel', 'cancelled')]
        for case, message in cases:
            with self.subTest(case=case), patch('video_timing.cv2.VideoCapture') as capture:
                decoder = capture.return_value
                decoder.isOpened.return_value = case != 'open'
                decoder.get.side_effect = {cv2.CAP_PROP_FRAME_WIDTH: 126 if case == 'size' else 128,
                    cv2.CAP_PROP_FRAME_HEIGHT: 80, cv2.CAP_PROP_FPS: 10 if case == 'fps' else 12}.__getitem__
                count = 2 if case == 'short' else 4 if case == 'extra' else 3
                decoder.read.side_effect = (RuntimeError('decode failed') if case == 'decode'
                    else [(True, None)] * count + [(False, None)])
                progress = Mock(side_effect=RuntimeError('cancelled')) if case == 'cancel' else None
                with self.assertRaisesRegex(RuntimeError, message):
                    verify_encoded('clip', dict(frames=3, fps=12), (127, 79), progress=progress)
                decoder.release.assert_called_once_with()

    def test_export_startup_failures_release_opened_video_handles(self):
        from review_configured_video import export_video
        source = dict(video='source.mp4', crop_xyxy=[0, 0, 127, 79], frames=3, fps=12)
        for failure in ('source', 'metadata', 'writer'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                cropped, raw, writer = Mock(), Mock(), Mock()
                cropped.isOpened.return_value = True
                raw.isOpened.return_value = failure != 'source'
                raw.get.side_effect = {cv2.CAP_PROP_FRAME_COUNT: 2 if failure == 'metadata' else 3,
                    cv2.CAP_PROP_FPS: 12, cv2.CAP_PROP_FRAME_WIDTH: 128, cv2.CAP_PROP_FRAME_HEIGHT: 80}.__getitem__
                writer.isOpened.return_value = False
                with patch('review_configured_video.cv2.VideoCapture', side_effect=[cropped, raw]), \
                        patch('review_configured_video.cv2.VideoWriter', return_value=writer), \
                        self.assertRaises((RuntimeError, ValueError)):
                    export_video(Path(folder), 'result.mp4', source, [1], Mock())
                cropped.release.assert_called_once_with()
                raw.release.assert_called_once_with()
                if failure == 'writer':
                    writer.release.assert_called_once_with()
                else:
                    writer.release.assert_not_called()

    def test_tracking_review_decode_failure_releases_source(self):
        from review_configured_video import tracking_review
        with patch('review_configured_video.cv2.VideoCapture') as capture:
            capture.return_value.read.return_value = (False, None)
            with self.assertRaisesRegex(RuntimeError, 'Missing source frame'):
                tracking_review(Path('.'), dict(video='clip', crop_xyxy=[0, 0, 128, 80], frames=3), [1], Mock())
            capture.return_value.release.assert_called_once_with()

    def test_short_opening_interval_preserves_pts_but_interior_jumps_fail(self):
        with patch('video_timing.cv2.VideoCapture') as capture:
            def check(milliseconds):
                capture.return_value.read.side_effect = [(True,None)]*len(milliseconds)+[(False,None)]
                capture.return_value.get.side_effect = milliseconds
                return verify_timebase('clip',len(milliseconds),30)
            result=check([0,22,55.333333,88.666666,122])
            self.assertEqual(result['status'],'decoded_pts_short_first_interval')
            np.testing.assert_allclose(result['timestamps_s'],[0,.022,.055333333,.088666666,.122])
            for times in ([0,33.333,55.333,88.666,122], [0,80,113.333,146.666,180]):
                with self.assertRaisesRegex(ValueError,'跳变'): check(times)

    def test_explicit_pixels_have_no_physical_calibration(self):
        config=dict(measurement_mode='pixels',scale_um_per_px=99)
        validate_scale(config)
        self.assertIsNone(config['scale_um_per_px'])
        for value in (None,0,-1,True,float('nan')):
            with self.assertRaises(ValueError): validate_scale(dict(scale_um_per_px=value))
        row=pixel_fields(dict(length_um=10,area_um2=20,speed_um_s=0))
        self.assertEqual(row['length_px'],10);self.assertNotIn('length_um',row)

    def test_unconfirmed_head_is_not_formal_and_scale_not_required_for_angle(self):
        row=dict(worm_id=1,quality_schema_version=VERSION,source_valid=True,mask_valid=True,pose_valid=True,
            identity_valid=True,head_valid=True,head_identity_confirmed=False,scale_valid=False,
            measurement_mode='pixels',volume_valid=True,length_px=100,area_px2=200,speed_px_s=0,
            head_candidate_angle_smoothed_deg=30)
        self.assertTrue(permissions(row)['motion']);self.assertFalse(permissions(row)['head'])
        view=present_frame(row)
        self.assertEqual(view['length_px'],100);self.assertIsNone(view['length_um'])
        self.assertEqual(view['speed_px_s'],0);self.assertIsNone(view['head_angle_deg'])
        row.update(head_identity_confirmed=True,measurement_mode='physical')
        self.assertTrue(permissions(row)['head']);self.assertFalse(permissions(row)['motion'])
        row['time_valid']=False;self.assertFalse(permissions(row)['head'])

    def test_directional_events_do_not_count_hz_as_events(self):
        fps=100.;t=np.arange(1000)/fps;angles=40*np.sin(2*np.pi*t)
        events,cycles,duration=metrics.bend_events(angles,np.ones(len(t),bool),fps,20,.15)
        self.assertEqual(len(events),20);self.assertAlmostEqual(duration,9.99)
        self.assertGreater(len(events)*60/duration,119)
        self.assertEqual(cycles,9)
        valid=np.ones(len(t),bool);valid[400:600]=False
        events,_,duration=metrics.bend_events(angles,valid,fps,20,.15)
        self.assertTrue(all(not 400<=e['frame']<600 for e in events))
        self.assertAlmostEqual(duration,7.98)

    def test_draft_survives_new_sample_but_not_different_frame(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(manual,'ROOT',Path(directory)):
            video=Path(directory)/'video.mp4'
            writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'mp4v'),10.,(80,60))
            for _ in range(5):writer.write(np.zeros((60,80,3),np.uint8))
            writer.release()
            a=manual.Annotation(dict(sample='first',video=str(video)))
            save_draft(a,dict(note='synthetic draft',roi=None))
            b=manual.Annotation(dict(sample='second',video=str(video)))
            self.assertEqual(get_draft(b)['body']['note'],'synthetic draft')
            c=manual.Annotation(dict(sample='second',video=str(video)),frame=1)
            self.assertIsNone(get_draft(c)['body'])

    def test_cancel_stops_worker_and_preserves_log(self):
        with tempfile.TemporaryDirectory() as directory:
            progress=PipelineProgress(); log=Path(directory)/'worker.log'
            timer=threading.Timer(.2,progress.cancel_requested.set);timer.start()
            started=time.monotonic()
            try:
                with self.assertRaisesRegex(RuntimeError,'取消'):
                    run_stage(dict(name='test',command=[sys.executable,'-c','import time;print("started",flush=True);time.sleep(30)']),log,progress)
            finally:timer.join()
            self.assertLess(time.monotonic()-started,5);self.assertTrue(log.exists())

    def test_saved_run_path_is_confined(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ('../escape','..\\escape','/absolute'):
                with self.assertRaises(ValueError):load_config(Path(directory),name)


if __name__=='__main__':unittest.main()
