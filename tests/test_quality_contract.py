import csv
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

import cv2
import numpy as np
from measurements import analyze_phenotypes as metrics
import manual_worm_annotation as manual
from quality import quality_stage
import result_overlay
import review_configured_video as review
from measurements.frame_metrics import present_frame
from quality.multiworm_quality import sequence_guards, validate_reviews
from pipeline_progress import PipelineProgress
from quality.quality_schema import VERSION, permissions
from quality_fixture import make_fixture
from worm_pipeline import compatible_identity


class OptionalIsolationTests(unittest.TestCase):
    def test_slow_or_failing_optional_probe_cannot_break_state_or_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config,_,_=make_fixture(root)
            release=threading.Event();entered=threading.Event()
            def failing():entered.set();release.wait(5);raise PermissionError('simulated optional permission error')
            progress=PipelineProgress()
            with patch.object(manual,'ROOT',root),patch('pose.body_sequence_solver.availability',side_effect=failing):
                annotation=manual.Annotation(config)
                progress.bind_server(manual.create_server(annotation=annotation,progress=progress))
                url=progress.url.removesuffix('/progress')
                def get(route):
                    with urllib.request.urlopen(url+route,timeout=2) as response:return json.load(response)
                try:
                    self.assertEqual(get('/state')['frame_count'],10)
                    self.assertFalse(entered.is_set())
                    self.assertTrue(get('/api/algorithms')['algorithm']['checking'])
                    self.assertTrue(entered.wait(1))
                    self.assertEqual(get('/state')['sample'],config['sample'])
                    with urllib.request.urlopen(url+'/image.png',timeout=2) as response:self.assertEqual(response.status,200)
                    release.set()
                    for _ in range(100):
                        status=get('/api/algorithms')['algorithm']
                        if not status['checking']:break
                        time.sleep(.01)
                    self.assertFalse(status['available']);self.assertIn('permission',status['detail'])
                finally:release.set();progress.close()


class IdentityContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.config,self.source,self.masks=make_fixture(self.root)
    def tearDown(self):self.temp.cleanup()

    def test_confirmation_is_traceable_and_forward_only(self):
        guards=sequence_guards(self.masks,source_invalid=[8],fps=10,
            identity_confirmations=self.config['identity_confirmations'])
        q=guards[1]
        self.assertTrue(q[0]['identity_valid']);self.assertFalse(q[2]['mask_valid'])
        self.assertTrue(q[4]['mask_valid']);self.assertFalse(q[4]['identity_valid'])
        self.assertTrue(q[6]['identity_valid']);self.assertEqual(q[6]['identity_status'],'reviewed_continuous')
        self.assertEqual(q[9]['identity_evidence'],self.config['identity_confirmations'][0])
        self.assertFalse(q[8]['identity_valid']);self.assertTrue(q[9]['identity_valid'])
        self.assertFalse(guards[2][9]['identity_valid'])

    def test_confirmation_without_evidence_or_at_contact_is_rejected(self):
        event=dict(self.config['identity_confirmations'][0]);event.pop('evidence')
        with self.assertRaises(ValueError):validate_reviews(dict(identity_confirmations=[event]),[1,2],10)
        event=dict(self.config['identity_confirmations'][0],frame=2)
        with self.assertRaisesRegex(ValueError,'不能恢复身份'):
            sequence_guards(self.masks,identity_confirmations=[event])

    def test_new_missing_dimensions_fail_closed_legacy_remains_labelled(self):
        old=dict(worm_id=1,mask_valid=True,centerline_valid=True,length_um=100,speed_um_s=0)
        self.assertEqual(present_frame(old)['length_um'],100)
        self.assertEqual(present_frame(old)['identity_status'],'legacy_unassessed')
        self.assertTrue(present_frame(old)['quality']['legacy'])
        new=dict(old,quality_schema_version=VERSION)
        self.assertFalse(permissions(new)['posture']);self.assertIsNone(present_frame(new)['length_um'])

    def test_schema_upgrade_and_reviews_invalidate_reuse(self):
        old=dict(config=dict(self.config));old['config'].pop('quality_schema_version')
        self.assertFalse(compatible_identity(old,dict(config=self.config)))
        changed=dict(self.config,identity_confirmations=[])
        self.assertFalse(compatible_identity(dict(config=self.config),dict(config=changed)))

    def test_new_schema_requires_each_dimension_and_rejects_unknown_versions(self):
        row=dict(quality_schema_version=VERSION,source_valid=True,mask_valid=True,pose_valid=True,
            identity_valid=True,head_valid=True,head_identity_confirmed=True,scale_valid=True,volume_valid=True)
        self.assertTrue(all(permissions(row).values()))
        for name in ('source','mask','pose','identity','head'):
            missing=dict(row);missing.pop(name+'_valid')
            self.assertFalse(permissions(missing)['head'],name)
        self.assertFalse(any(permissions(dict(row,quality_schema_version='future-unknown')).values()))

if __name__=='__main__':unittest.main()
