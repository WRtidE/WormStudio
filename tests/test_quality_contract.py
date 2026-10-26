import csv
import hashlib
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
import analyze_phenotypes as metrics
import extract_centerlines
import manual_worm_annotation as manual
import quality_stage
import result_overlay
import review_configured_video as review
import tapnext_support as tap
from frame_metrics import present_frame
from multiworm_quality import sequence_guards, validate_reviews
from pipeline_progress import PipelineProgress
from quality_schema import VERSION, permissions
from quality_fixture import make_fixture
from crossing_centerline import resolve_sequence
from pose_segments import accepted_segments
from worm_pipeline import compatible_identity


class OptionalIsolationTests(unittest.TestCase):
    def test_missing_module_and_permission_are_local_unavailability(self):
        with patch.object(tap,'resources',return_value=(Path('.'),Path('model'))):
            for error in (ModuleNotFoundError('einops'), PermissionError('dependency denied'), RuntimeError('broken import')):
                with patch.object(tap,'prepare_runtime',side_effect=error):
                    status=tap.availability()
                    self.assertFalse(status['available']);self.assertTrue(status['detail'])

    def test_missing_dependency_never_falls_back_to_validation(self):
        with patch.object(tap.importlib.util,'find_spec',return_value=None):
            with self.assertRaisesRegex(ValueError,'einops==0.8.1'):tap.dependency_path()

    def test_missing_weights_and_corrupt_weights_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'docs').mkdir();model=root/'model.pt'
            (root/'docs/models.json').write_text(json.dumps(dict(tapnext=dict(sha256=hashlib.sha256(b'good').hexdigest()))))
            with patch.object(tap,'resources',return_value=(root,model)),patch.object(tap,'prepare_runtime',return_value={}):
                self.assertFalse(tap.availability()['available'])
                model.write_bytes(b'bad');self.assertIn('SHA-256',tap.availability()['detail'])
                model.write_bytes(b'good');self.assertTrue(tap.availability()['available'])
                model.write_bytes(b'evil');self.assertFalse(tap.availability()['available'])

    def test_slow_or_failing_optional_probe_cannot_break_state_or_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config,_,_=make_fixture(root)
            release=threading.Event();entered=threading.Event()
            def failing():entered.set();release.wait(5);raise PermissionError('simulated optional permission error')
            progress=PipelineProgress()
            with patch.object(manual,'ROOT',root),patch.object(tap,'availability',side_effect=failing):
                annotation=manual.Annotation(config)
                progress.bind_server(manual.create_server(annotation=annotation,progress=progress))
                url=progress.url.removesuffix('/progress')
                def get(route):
                    with urllib.request.urlopen(url+route,timeout=2) as response:return json.load(response)
                try:
                    self.assertEqual(get('/state')['frame_count'],10)
                    self.assertFalse(entered.is_set())
                    self.assertTrue(get('/api/algorithms')['tapnext']['checking'])
                    self.assertTrue(entered.wait(1))
                    self.assertEqual(get('/state')['sample'],config['sample'])
                    with urllib.request.urlopen(url+'/image.png',timeout=2) as response:self.assertEqual(response.status,200)
                    release.set()
                    for _ in range(100):
                        status=get('/api/algorithms')['tapnext']
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

    def test_contact_does_not_enter_self_contact_solver(self):
        with patch('crossing_centerline.topology_candidates',side_effect=AssertionError('not a self crossing')):
            p,q,diagnostic=resolve_sequence([self.masks[1][2]],[False],10)
        self.assertTrue(np.isnan(p).all());self.assertTrue(np.isnan(diagnostic).all())
        self.assertEqual(q[0]['topology'],'mask_excluded')
        np.testing.assert_array_equal(accepted_segments(4,1,{(1,1):True})[1],[2,3])

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

    def test_fixture_ui_csv_json_overlay_and_summary_share_rules(self):
        phenotype=json.loads((metrics.ROOT/'config/phenotype_config.json').read_text(encoding='utf-8'))
        observed=[]
        def overlay(row):
            result=present_frame(row);observed.append((int(row['frame']),int(row['worm_id']),result));return result
        with patch.object(metrics,'ROOT',self.root),patch.object(extract_centerlines,'ROOT',self.root),patch.object(review,'ROOT',self.root):
            quality_stage.run(self.config['sample'])
            extract_centerlines.run(self.config['sample'],'niu')
            with patch.object(result_overlay,'present_frame',side_effect=overlay):
                report=metrics.analyze(self.config['sample'],phenotype,'niu','centerlines-niu')
            output=self.root/'outputs'/self.config['sample']/'phenotypes-centerlines-niu'
            with patch('sys.argv',['review','--sample',self.config['sample'],'--centerline-source','centerlines-niu']):review.main()
        csv_rows=list(csv.DictReader((output/'per_frame_metrics.csv').open(encoding='utf-8-sig')))
        json_rows=json.loads((output/'per_frame_metrics.json').read_text(encoding='utf-8'))
        qualities=json.loads((output/'quality.json').read_text(encoding='utf-8'))
        lookup={(int(r['frame']),int(r['worm_id'])):r for r in csv_rows}
        for r,j,q in zip(csv_rows,json_rows,qualities):
            self.assertEqual(present_frame(r),present_frame(j))
            self.assertEqual(present_frame(r)['quality']['permissions'],q['permissions'])
        for f,w,view in observed:self.assertEqual(view,present_frame(lookup[f,w]))
        post=lookup[4,1];confirmed=lookup[6,1]
        self.assertEqual(post['mask_valid'],'True');self.assertEqual(post['identity_valid'],'False')
        self.assertGreater(float(post['area_um2']),0)
        self.assertEqual(post['speed_um_s'],'');self.assertEqual(post['length_um'],'')
        self.assertEqual(confirmed['identity_valid'],'True');self.assertGreater(float(confirmed['length_um']),0)
        self.assertEqual(report['worms'][0]['identity_valid_frames'],5)
        self.assertEqual(report['worms'][1]['identity_valid_frames'],2)
        with np.load(output/'centerlines.npz') as accepted:
            self.assertNotIn('w1_f4',accepted.files);self.assertIn('w1_f6',accepted.files)
        with np.load(output/'diagnostic-centerlines.npz') as diagnostic:self.assertIn('w1_f4',diagnostic.files)
        progress=PipelineProgress();progress.data.update(sample=self.config['sample'],fps=10.,frames=10)
        progress.artifacts['metrics']=output/'per_frame_metrics.csv'
        progress.bind_server(manual.create_server(progress=progress))
        try:
            with urllib.request.urlopen(progress.url.removesuffix('/progress')+'/api/frame-metrics?run='+self.config['sample']) as response:payload=json.load(response)
            self.assertEqual(payload['frames'][4][0],present_frame(post))
        finally:progress.close()
        verification=json.loads((output/'verification.json').read_text(encoding='utf-8'))
        self.assertEqual(verification['frames'],10);self.assertEqual(verification['quality_schema_version'],VERSION)


if __name__=='__main__':unittest.main()
