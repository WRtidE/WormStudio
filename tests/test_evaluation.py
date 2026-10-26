import copy
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from evaluation_annotations import DatasetStore, RevisionConflict, validate_frames
from evaluation_dataset import VERSION, build_tasks, digest, freeze_split, inventory, read_json, write_new
from evaluation_fixture import make_dataset, marked_frame


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='evaluation-test-')
        self.root=Path(self.temp.name)/'fixture'
        self.manifest,self.splits,self.tasks=make_dataset(self.root)
        self.store=DatasetStore(self.root); self.task=self.tasks['tasks'][0]
        self.video=self.manifest['videos'][0]

    def tearDown(self): self.temp.cleanup()

    def body(self,role='annotator_a',author='Reviewer A',status='submitted'):
        return dict(task_id=self.task['task_id'],role=role,annotator=author,status=status,base_revision=None,
            frames=[marked_frame(n) for n in self.task['frames']],active_seconds=12,review=None)

    def test_exact_copies_merge_and_keep_both_paths(self):
        source=Path(self.video['primary_path']); (self.root/'copy.avi').write_bytes(source.read_bytes())
        result=inventory([self.root],[],Path(self.temp.name)/'inventory')
        self.assertEqual(result['summary']['unique_videos'],1)
        self.assertEqual(result['summary']['exact_duplicate_copies'],1)

    def test_unknown_provenance_never_becomes_test(self):
        m=copy.deepcopy(self.manifest); m['videos'][0]['development_exposure']='unknown'
        result=freeze_split(m)
        self.assertEqual(result['counts'],{'pending':1})
        self.assertNotEqual(result['readiness'],'ready')

    def test_known_development_cannot_be_reclassified_as_holdout(self):
        result=freeze_split(self.manifest,{'videos':{self.video['video_id']:{'development_exposure':'never_used'}}})
        self.assertEqual(result['counts'],{'development':1})

    def test_transitive_groups_and_seed_are_stable(self):
        rows=[]; metadata={}
        for n in range(40):
            v=copy.deepcopy(self.video);v['video_id']=f'vid-{n}';v['development_exposure']='unknown';rows.append(v)
            metadata[v['video_id']]=dict(acquisition_id=f'acq-{n}',batch_id=f'batch-{n}',pool_ids=[f'pool-{n}'],individual_ids=[f'worm-{n}'],
                device_id='camera',lineage_confirmed=True,development_exposure='never_used',reviewer='Owner',evidence='lab book',reviewed_at='2026-10-19T10:00:00+08:00')
        metadata['vid-1']['pool_ids']=['pool-0'];metadata['vid-2']['parent_video_ids']=['vid-1'];rows[0]['development_exposure']='observed'
        a=freeze_split(dict(videos=rows),dict(videos=metadata));b=freeze_split(dict(videos=list(reversed(rows))),dict(videos=metadata))
        self.assertEqual(a['assignments'],b['assignments'])
        first=[r for r in a['assignments'] if r['video_id'] in ('vid-0','vid-1','vid-2')]
        self.assertEqual(len({r['group_id'] for r in first}),1)
        self.assertTrue(all(r['split']=='development' for r in first))
        self.assertGreater(a['counts'].get('test',0),0)

    def test_random_tasks_reproduce_and_event_includes_full_context(self):
        plan=dict(video_ids=[self.video['video_id']],seed=1,random_frames_per_video=3,events=[dict(video_id=self.video['video_id'],
            start_frame=2,end_frame=4,context_before=1,context_after=6,evidence='synthetic test',status='proposal_needs_confirmation')])
        a=build_tasks(self.manifest,self.splits,plan); b=build_tasks(self.manifest,self.splits,plan)
        self.assertEqual(a['tasks'],b['tasks']);self.assertEqual(a['tasks'][-1]['frames'],list(range(1,7)))

    def test_pending_and_test_material_not_opened_for_pilot(self):
        for split in ('test','pending'):
            modified=copy.deepcopy(self.splits);modified['assignments'][0]['split']=split
            with self.assertRaises(ValueError): build_tasks(self.manifest,modified,dict(video_ids=[self.video['video_id']]))

    def test_task_out_of_bounds_and_tampered_manifest_fail(self):
        plan=dict(video_ids=[self.video['video_id']],events=[dict(video_id=self.video['video_id'],start_frame=1,end_frame=5,context_before=0,context_after=8,evidence='test',status='proposal_needs_confirmation')])
        with self.assertRaises(ValueError):build_tasks(self.manifest,self.splits,plan)
        (self.root/'manifest.json').write_text(json.dumps(dict(self.manifest,purpose='tampered')),encoding='utf-8')
        with self.assertRaises(ValueError):DatasetStore(self.root)

    def test_unknown_dimensions_are_valid_and_not_filled_from_predictions(self):
        row=self.store.save(self.body())
        self.assertEqual(row['frames'][0]['objects'][0]['head_tail']['status'],'unknown')
        self.assertEqual(row['assist'],'none');self.assertEqual(row['truth_status'],'independent_annotation')

    def test_invalid_coordinates_and_nan_rejected(self):
        for p in ([640,10],[-1,10],[1,float('nan')],[True,10]):
            body=self.body();body['frames'][0]['objects'][0]['endpoints']['a']['point']=p
            with self.assertRaises(ValueError):self.store.save(body)

    def test_invisible_endpoints_and_unfounded_identity_rejected(self):
        for field in ('endpoint','identity','head'):
            body=self.body();o=body['frames'][0]['objects'][0]
            if field=='endpoint':o['endpoints']['a']['visibility']='occluded'
            if field=='identity':o['identity']=dict(status='confirmed',individual_id='worm-1',evidence=None)
            if field=='head':o['head_tail']=dict(status='confirmed',head_endpoint='a',evidence='')
            with self.assertRaises(ValueError):self.store.save(body)

    def test_incomplete_or_unobserved_frames_cannot_claim_measurements(self):
        body=self.body();body['frames'][0]['complete']=False
        with self.assertRaises(ValueError):self.store.save(body)
        body=self.body();body['frames'][0]['source_status']='corrupt'
        with self.assertRaises(ValueError):self.store.save(body)

    def test_append_only_revisions_detect_stale_clients(self):
        body=self.body(status='draft');first=self.store.save(body)
        with self.assertRaises(RevisionConflict):self.store.save(body)
        body['base_revision']=first['revision_id'];self.store.save(body)
        self.assertEqual(self.store.verify()['annotation_revisions'],2)

    def test_independent_role_has_no_other_annotation_and_no_self_review(self):
        self.store.save(self.body())
        b=self.store.load(self.task['task_id'],'annotator_b','Reviewer B')
        self.assertIsNone(b['annotation']);self.assertEqual(b['comparisons'],[])
        with self.assertRaises(ValueError):self.store.save(self.body('annotator_b','Reviewer A'))
        with self.assertRaises(ValueError):self.store.load(self.task['task_id'],'annotator_a','Reviewer B')

    def test_adjudication_requires_two_submissions_and_current_revisions(self):
        with self.assertRaises(ValueError):self.store.load(self.task['task_id'],'adjudicator','Reviewer C')
        a=self.store.save(self.body());b=self.store.save(self.body('annotator_b','Reviewer B'))
        body=self.body('adjudicator','Reviewer C')
        body['review']=dict(compared_revision_ids=[a['revision_id'],'stale'],decision_notes='Reviewed both')
        with self.assertRaises(ValueError):self.store.save(body)
        body['review']['compared_revision_ids'][1]=b['revision_id'];row=self.store.save(body)
        self.assertEqual(row['truth_status'],'adjudicated')
        exported=self.store.export_truth(self.root/'truth.json');self.assertEqual(exported['adjudicated_tasks'],1)
        newer=self.body();newer['base_revision']=a['revision_id'];self.store.save(newer)
        with self.assertRaises(ValueError):self.store.export_truth(self.root/'stale-truth.json')
        self.assertFalse((self.root/'stale-truth.json').exists())

    def test_empty_annotations_cannot_be_exported_as_truth(self):
        with self.assertRaises(ValueError):self.store.export_truth(self.root/'empty-truth.json')

    def test_source_frame_integrity_and_task_membership(self):
        self.assertTrue(self.store.frame_image(self.task['task_id'],self.task['frames'][0]).startswith(b'\xff\xd8'))
        with self.assertRaises(ValueError):self.store.frame_image(self.task['task_id'],100)
        with Path(self.video['primary_path']).open('ab') as handle:handle.write(b'changed')
        with self.assertRaises(ValueError):self.store.frame_image(self.task['task_id'],self.task['frames'][0])

    def test_write_new_never_overwrites_a_snapshot(self):
        with self.assertRaises(FileExistsError):write_new(self.root/'manifest.json',{})

    def test_http_auth_static_assets_and_missing_dataset_isolation(self):
        from manual_worm_annotation import create_server
        from pipeline_progress import PipelineProgress
        progress=PipelineProgress();server=create_server(progress=progress);server.evaluation_store=self.store
        progress.bind_server(server);base=progress.url.removesuffix('/progress')
        try:
            for route in ('/studio','/studio-evaluation.js','/studio-evaluation.css','/api/evaluation/catalog','/api/progress'):
                with urllib.request.urlopen(base+route) as response:self.assertEqual(response.status,200)
            payload=json.dumps(self.body()).encode()
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(urllib.request.Request(base+'/api/evaluation/save',data=payload))
            self.assertEqual(error.exception.code,403)
            req=urllib.request.Request(base+'/api/evaluation/save',data=payload,headers={'X-Annotation-Token':progress.token,'Content-Type':'application/json'})
            with urllib.request.urlopen(req) as response:self.assertEqual(response.status,200)
            server.evaluation_store=None
            with patch.dict('os.environ',{'WORMSTUDIO_EVALUATION_DATASET':str(self.root/'missing')}):
                with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(base+'/api/evaluation/catalog')
                with urllib.request.urlopen(base+'/api/progress') as response:self.assertEqual(response.status,200)
        finally:progress.close()
