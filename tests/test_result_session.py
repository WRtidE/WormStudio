"""Results streaming, next-video session transitions, and immutable reruns."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import cv2
import numpy as np
import manual_worm_annotation as manual
from pipeline_progress import PipelineProgress, summarize_quality
from run_storage import preserve_run, input_identity
from worm_pipeline import compatible_identity


class ResultSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='worm-session-')
        self.root = Path(self.temp.name)
        self.patch = patch.object(manual, 'ROOT', self.root); self.patch.start()
        self.video = self.root/'second.mp4'
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'mp4v'), 12, (128,80))
        for _ in range(6): writer.write(np.zeros((80,128,3), np.uint8))
        writer.release()
        self.progress = PipelineProgress()
        self.progress.bind_server(manual.create_server(progress=self.progress))
        self.url = self.progress.url.removesuffix('/progress')

    def tearDown(self):
        self.progress.close(); self.patch.stop(); self.temp.cleanup()

    def post(self, route, body, token=True):
        req = urllib.request.Request(self.url+route, json.dumps(body).encode())
        if token: req.add_header('X-Annotation-Token', self.progress.token)
        return urllib.request.urlopen(req)

    def test_video_range_head_and_artifact_allowlist(self):
        self.progress.artifacts['video'] = self.video
        req = urllib.request.Request(self.url+'/artifact?key=video&inline=1', headers={'Range':'bytes=10-39'})
        with urllib.request.urlopen(req) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers['Content-Type'], 'video/mp4')
            self.assertEqual(response.read(), self.video.read_bytes()[10:40])
        req = urllib.request.Request(self.url+'/artifact?key=video', headers={'Range':'bytes=-7'})
        with urllib.request.urlopen(req) as response: self.assertEqual(response.read(), self.video.read_bytes()[-7:])
        req = urllib.request.Request(self.url+'/artifact?key=video', method='HEAD')
        with urllib.request.urlopen(req) as response:
            self.assertEqual(int(response.headers['Content-Length']), self.video.stat().st_size)
            self.assertEqual(response.read(), b'')
        for route, headers, status in [('/artifact?key=../../secret',{},404),('/artifact?key=video',{'Range':'bytes=999999999-'},416)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(self.url+route,headers=headers))
            self.assertEqual(error.exception.code, status)

    def test_next_video_validates_before_reset_and_refresh_keeps_annotation(self):
        self.progress.fail('example failure')
        with self.assertRaises(urllib.error.HTTPError): self.post('/api/new-video',{'video_path':str(self.video)},False)
        with self.assertRaises(urllib.error.HTTPError): self.post('/api/new-video',{'video_path':str(self.root/'missing.mp4')})
        self.assertEqual(self.progress.snapshot()['status'], 'failed')
        with self.post('/api/new-video',{'video_path':str(self.video)}) as response:
            self.assertEqual(json.load(response)['annotation_url'], '/')
        annotation = self.progress.server.annotation
        self.assertEqual(annotation.n, 6)
        self.assertEqual(annotation.crop,[0,0,128,80])
        self.assertIsNone(annotation.settings['scale_um_per_px'])
        self.assertEqual(self.progress.wait()['video'],str(self.video.resolve()))
        for _ in range(2):
            with urllib.request.urlopen(self.url+'/state') as response:
                state=json.load(response)
                self.assertEqual(state['video_name'],'second.mp4')
                self.assertEqual(state['token'],annotation.token)
        with self.assertRaises(urllib.error.HTTPError) as error: self.post('/api/new-video',{'video_path':str(self.video)})
        self.assertEqual(error.exception.code,409)

    def test_workspace_accepts_first_video_only_while_ready(self):
        self.progress.data.update(status='ready')
        with self.post('/api/new-video', {'video_path':str(self.video)}) as response:
            self.assertEqual(json.load(response)['annotation_url'], '/')
        self.assertEqual(self.progress.snapshot()['status'], 'preparing')
        self.assertEqual(self.progress.server.annotation.settings['centerline_method'], 'topology')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post('/api/new-video', {'video_path':str(self.video)})
        self.assertEqual(error.exception.code,409)
        self.assertEqual(self.progress.wait()['video'],str(self.video.resolve()))

    def test_empty_workspace_can_close_without_a_video(self):
        self.progress.data.update(status='ready')
        with self.post('/close', {}) as response:
            self.assertTrue(json.load(response)['closed'])
        self.assertTrue(self.progress.closed.is_set())

    def test_cancel_preparation_keeps_draft_and_releases_annotation(self):
        from workspace_sessions import save_draft,get_draft
        self.progress.data.update(status='ready')
        with self.post('/api/new-video',{'video_path':str(self.video)}): pass
        self.progress.wait()
        annotation=self.progress.server.annotation
        save_draft(annotation,{'note':'synthetic draft'})
        with self.post('/api/cancel',{}): pass
        self.assertTrue(annotation.done.is_set())
        self.assertIsNone(annotation.result)
        self.assertEqual(self.progress.snapshot()['status'],'cancelled')
        self.assertEqual(get_draft(annotation)['body']['note'],'synthetic draft')

    def test_file_upload_stays_in_import_directory(self):
        self.progress.fail('example failure')
        req=urllib.request.Request(self.url+'/api/upload-video?name=..%2Fsecond.mp4',self.video.read_bytes(),headers={'X-Annotation-Token':self.progress.token})
        with urllib.request.urlopen(req) as response: self.assertEqual(response.status,200)
        imported=Path(self.progress.server.annotation.video)
        self.assertTrue(imported.is_relative_to(self.root/'input_videos'))
        self.assertEqual(imported.read_bytes(),self.video.read_bytes())

    def test_completed_summary_nulls_are_not_reported_as_zero(self):
        config=dict(sample='done',video=str(self.video),centerline_method='niu')
        self.progress.configure(config,dict(frames=6,ids=[1]),[dict(name='metrics',phase='metrics')],self.root)
        folder=self.root/'phenotypes-centerlines-niu';folder.mkdir()
        (folder/'worm_summary.csv').write_text('worm_id,frames,valid_frames,median_length_um,mean_speed_um_s\n1,6,0,,nan\n',encoding='utf-8')
        self.progress.done(0);self.progress.finish(self.root,config,'metrics')
        row=self.progress.snapshot()['results'][0]
        self.assertEqual(row['valid_frames'],0)
        self.assertIsNone(row['median_length_um'])
        self.assertIsNone(row['mean_speed_um_s'])

    def test_changed_mask_gets_new_run_and_keeps_previous_files(self):
        outputs=self.root/'outputs';base=outputs/'same-video';seed=base/'manual-initialization/worm-1.png'
        seed.parent.mkdir(parents=True)
        mask=np.zeros((80,128),np.uint8);mask[10:25,20:60]=255
        seed.write_bytes(manual.png(mask))
        config=dict(sample='same-video',video=str(self.video),groups=[dict(name='manual',anchor_frame=0,worms=[dict(id=1,mask_path=str(seed))])])
        phenotype=self.root/'phenotype.json';phenotype.write_text('{}')
        motion=self.root/'motion.json';motion.write_text('{}')
        old=input_identity(config,phenotype,motion)
        result=base/'old-result.mp4';result.write_bytes(b'previous result')
        mask[25,20]=255;seed.write_bytes(manual.png(mask))
        self.assertFalse(compatible_identity(old,input_identity(config,phenotype,motion)))
        fresh=preserve_run(config,outputs)
        self.assertNotEqual(fresh['sample'],config['sample'])
        self.assertEqual(result.read_bytes(),b'previous result')
        new_seed=Path(fresh['groups'][0]['worms'][0]['mask_path'])
        self.assertEqual(new_seed.read_bytes(),seed.read_bytes())
        self.assertNotEqual(new_seed,seed)

    def test_quality_flags_zero_partial_and_unknown_without_changing_metrics(self):
        rows=[dict(worm_id=1,frames=12,valid_frames=0,mask_valid_frames=1),
              dict(worm_id=2,frames=12,valid_frames=12,mask_valid_frames=12),
              dict(worm_id=3,frames=12,valid_frames=0,mask_valid_frames=3)]
        report=summarize_quality(rows)
        self.assertEqual((report['needs_review'],report['total']),(2,3))
        self.assertEqual(report['worms'][0]['valid_frames_ratio'],0)
        self.assertEqual(report['worms'][2]['mask_valid_frames_ratio'],.25)
        self.assertFalse(report['worms'][1]['needs_review'])
        unknown=summarize_quality([dict(worm_id=4,frames=12,valid_frames=None,mask_valid_frames=11)])
        self.assertTrue(unknown['worms'][0]['needs_review'])
        self.assertIsNone(unknown['worms'][0]['valid_frames_ratio'])

    def test_reannotation_previews_and_uses_selected_frame_in_fresh_run(self):
        base=self.root/'outputs'/'old';base.mkdir(parents=True)
        saved=base/'original.txt';saved.write_text('preserve old annotation and results')
        config=dict(sample='old',video=str(self.video),centerline_method='niu',scale_um_per_px=2.5,
                    crop_xyxy=[10,10,100,70],groups=[dict(anchor_frame=0,worms=[])])
        self.progress.configure(config,dict(frames=6,ids=[1]),[],base)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post('/api/reannotate',{'frame':3})
        self.assertEqual(error.exception.code,409)
        self.progress.fail('quality review test')
        with urllib.request.urlopen(self.url+'/api/frame-preview?frame=3') as response:
            preview=cv2.imdecode(np.frombuffer(response.read(),np.uint8),cv2.IMREAD_COLOR)
            self.assertEqual(preview.shape,(80,128,3))
        for frame in (-1,6,'3',1.5):
            with self.assertRaises(urllib.error.HTTPError):self.post('/api/reannotate',{'frame':frame})
            self.assertEqual(self.progress.snapshot()['status'],'failed')
        with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/reannotate',{'frame':3},False)
        self.assertEqual(error.exception.code,403)
        with self.post('/api/reannotate',{'frame':3}) as response:self.assertEqual(response.status,200)
        annotation=self.progress.server.annotation
        self.assertEqual(annotation.frame,3)
        self.assertEqual(annotation.crop,[0,0,128,80])
        self.assertEqual(annotation.settings['scale_um_per_px'],2.5)
        self.assertEqual(annotation.settings['centerline_method'],'niu')
        self.assertNotEqual(annotation.settings['sample'],'old')
        self.assertEqual(annotation.settings['previous_sample'],'old')
        self.assertEqual(annotation.settings['groups'],[])
        self.assertEqual(saved.read_text(),'preserve old annotation and results')


if __name__ == '__main__': unittest.main()
