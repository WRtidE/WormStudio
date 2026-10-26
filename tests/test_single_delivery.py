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
from unittest.mock import patch
import urllib.request

import cv2
import numpy as np
import manual_worm_annotation as manual
import analyze_phenotypes as metrics
import extract_centerlines
import quality_stage
import worm_pipeline
from quality_fixture import make_fixture
from quality_schema import VERSION, permissions
from frame_metrics import present_frame
from measurement_units import validate_scale, pixel_fields
from pipeline_progress import PipelineProgress
from workspace_sessions import save_draft, get_draft, load_config, confirm_head
from worm_pipeline import run_stage
from video_timing import verify_timebase


class SingleDeliveryTests(unittest.TestCase):
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

    def test_pixel_pipeline_csv_json_summary_and_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config,source,_=make_fixture(root)
            base=root/'outputs'/config['sample']
            config.update(measurement_mode='pixels',scale_um_per_px=None)
            source.update(measurement_mode='pixels',scale_um_per_px=None,model='SAM2 Hiera Large',
                          provenance='Synthetic contract fixture, not an actual model run')
            (base/'sam2-tracking/component-cleanup.json').write_text(json.dumps(dict(changes=[],provenance='synthetic fixture has no duplicate components')))
            (base/'pipeline-config.json').write_text(json.dumps(config),encoding='utf-8')
            (base/'sam2-tracking/summary.json').write_text(json.dumps(source),encoding='utf-8')
            settings=json.loads((metrics.ROOT/'config/phenotype_config.json').read_text(encoding='utf-8'))
            with patch.object(extract_centerlines,'ROOT',root),patch.object(metrics,'ROOT',root):
                quality_stage.run(config['sample']);extract_centerlines.run(config['sample'],'niu')
                with patch.object(metrics,'render'),patch.object(metrics,'write_report'):
                    report=metrics.analyze(config['sample'],settings,'niu','centerlines-niu')
            self.assertIsNone(report['scale_um_per_px'])
            self.assertEqual(report['timebase']['status'],'decoded_pts_verified_cfr')
            folder=base/'phenotypes-centerlines-niu'
            rows=json.loads((folder/'per_frame_metrics.json').read_text(encoding='utf-8'))
            self.assertTrue(any(r['length_px'] is not None for r in rows))
            self.assertTrue(all('length_um' not in r and 'area_um2' not in r for r in rows))
            self.assertTrue(all(r['head_candidate_angle_deg'] is None for r in rows))
            invalid=next(r for r in rows if r['frame']==2)
            self.assertIsNone(invalid['centroid_x_source_px'])
            self.assertIsNone(invalid['length_px'])
            # The review action must preserve the old run and reuse identical geometry.
            (root/'config').mkdir()
            for name in ('phenotype_config.json','motion_constraints.json'):
                shutil.copy2(metrics.ROOT/'config'/name,root/'config'/name)
            before=(folder/'per_frame_metrics.json').read_bytes()
            with patch.object(worm_pipeline,'ROOT',root):
                revised=confirm_head(root,config['sample'],1,0,1)
            self.assertNotEqual(revised['sample'],config['sample'])
            newbase=root/'outputs'/revised['sample']
            np.testing.assert_array_equal(np.load(base/'centerlines-niu/worm-1/centerlines.npy'),
                np.load(newbase/'centerlines-niu/worm-1/centerlines.npy'))
            self.assertEqual((folder/'per_frame_metrics.json').read_bytes(),before)
            with patch.object(metrics,'ROOT',root),patch.object(metrics,'render'),patch.object(metrics,'write_report'):
                reviewed=metrics.analyze(revised['sample'],settings,'niu','centerlines-niu')
            self.assertGreater(reviewed['worms'][0]['head_confirmed_frames'],0)
            self.assertEqual(report['worms'][0]['head_confirmed_frames'],0)
            # Contradict the displayed H end; the shared segment must lose formal head measurements.
            from workspace_sessions import remove_head,derive_geometry_run
            with patch.object(worm_pipeline,'ROOT',root):
                conflicted=confirm_head(root,revised['sample'],1,0,1)
            with patch.object(metrics,'ROOT',root),patch.object(metrics,'render'),patch.object(metrics,'write_report'):
                conflict_report=metrics.analyze(conflicted['sample'],settings,'niu','centerlines-niu')
            conflict_rows=json.loads((root/'outputs'/conflicted['sample']/'phenotypes-centerlines-niu/per_frame_metrics.json').read_text())
            self.assertTrue(any(r['head_anchor_conflict'] for r in conflict_rows))
            self.assertTrue(all(r['head_candidate_angle_deg'] is None for r in conflict_rows if r['head_anchor_conflict']))
            self.assertEqual(conflict_report['worms'][0]['head_confirmed_frames'],0)
            with patch.object(worm_pipeline,'ROOT',root):
                restored=remove_head(root,conflicted['sample'],1,conflicted['head_annotations']['1'][-1]['id'])
            with patch.object(metrics,'ROOT',root),patch.object(metrics,'render'),patch.object(metrics,'write_report'):
                restored_report=metrics.analyze(restored['sample'],settings,'niu','centerlines-niu')
            self.assertEqual(restored_report['worms'][0]['head_confirmed_frames'],reviewed['worms'][0]['head_confirmed_frames'])
            # A finite, correctly shaped but modified pose must not be reused.
            pose_path=newbase/'centerlines-niu/worm-1/centerlines.npy'
            corrupted=np.load(pose_path);corrupted[0,20,0]+=1;np.save(pose_path,corrupted)
            with patch.object(worm_pipeline,'ROOT',root),self.assertRaisesRegex(ValueError,'缓存内容'):
                derive_geometry_run(root,revised['sample'])

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
