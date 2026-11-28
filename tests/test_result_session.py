"""Results streaming, next-video session transitions, and immutable reruns."""
import csv
from concurrent.futures import ThreadPoolExecutor
import io
import http.client
import json
from pathlib import Path
import tempfile
import threading
import socket
import time
import unittest
from unittest.mock import Mock, patch
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
        self.root = Path(self.temp.name).resolve()
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

    def test_long_revision_uses_the_same_short_video_for_export_and_playback(self):
        from centerline_methods import method_source
        from run_storage import annotated_video_path, fresh_sample, result_video_files
        from worm_pipeline import build_stages
        sample = '222324-槽1-20261120-153720-0b55-20261120-160055-088-20261120-160719-64d0'
        outputs = self.root/('a'*32)/'outputs'
        base = outputs/sample
        folder = base/('phenotypes-'+method_source('body_sequence'))
        folder.mkdir(parents=True)
        revised = fresh_sample(sample, outputs)
        self.assertRegex(revised, r'^222324-槽1-\d{8}-\d{6}-[a-f0-9]{4}$')
        config = dict(sample=sample, centerline_method='body_sequence', video=str(self.video))
        with patch('worm_pipeline.ROOT',outputs.parent):
            stages = build_stages(config,dict(ids=[1],frames=6),base/'cfg.json',base/'metrics.json')
        expected = folder/'annotated.mp4'
        self.assertEqual(Path(stages[-1]['outputs'][0]),expected)
        self.assertEqual(result_video_files(base,config)[0][2],expected)
        self.assertEqual(annotated_video_path(folder,sample, clean=True),folder/'annotated_clean.mp4')
        # A short-named result must still be found if the workspace gets moved.
        (self.root/'annotated.mp4').touch()
        self.assertEqual(annotated_video_path(self.root,sample),self.root/'annotated.mp4')

    def test_completed_chamber_streams_its_own_video_while_another_run_is_active(self):
        from batch_queue import BatchQueue
        from run_storage import result_video_files
        sample = 'first-chamber'
        config = dict(sample=sample, video=str(self.video), centerline_method='body_sequence')
        base = self.root/'outputs'/sample
        annotated = result_video_files(base, config)[0][2]
        annotated.parent.mkdir(parents=True)
        annotated.write_bytes(b'chamber-one')
        (base/'pipeline-config.json').write_text(json.dumps(config))
        (base/'pipeline-state.json').write_text(json.dumps({'stages': {'export': {'status': 'complete'}}}))
        batch = BatchQueue(self.root/'config/batch.json')
        batch.groups = [dict(id='saved', samples=[sample])]
        batch.archived[sample] = dict(sample=sample, status='complete')
        self.progress.batch = batch
        self.progress.data.update(sample='next', status='running')
        self.progress.artifacts['video'] = self.video
        url = self.url+'/artifact?key=video&inline=1&run=first-chamber'
        request = urllib.request.Request(url+'&group=saved', headers={'Range':'bytes=0-6'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual((response.status, response.read()), (206, b'chamber'))
        for suffix, status in [('', 409), ('&group=unknown', 404)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url+suffix)
            self.assertEqual(error.exception.code, status)
        self.assertEqual(self.progress.data['sample'], 'next')

    def test_chamber_export_groups_by_source_handles_overlap_and_requires_explicit_partial(self):
        import time
        from batch_queue import BatchQueue
        from chamber_videos import members_for_video
        batch = BatchQueue(self.root/'batch.json')
        self.progress.batch = batch
        boxes = {22: (0, 0, 96, 78), 23: (32, 0, 128, 78)}
        for slot, box in boxes.items():
            sample = f'slot-{slot}'
            config = dict(sample=sample, video=str(self.video), centerline_method='body_sequence',
                          chamber_id=slot, crop_xyxy=box)
            batch.add(config)
            base = self.root/'outputs'/sample
            tracking = base/'sam2-tracking'
            masks = tracking/'masks/worm-1'
            masks.mkdir(parents=True)
            (base/'pipeline-config.json').write_text(json.dumps(config))
            (base/'pipeline-state.json').write_text('{}')
            (tracking/'summary.json').write_text(json.dumps(dict(video=str(self.video), frames=6, fps=12,
                                                               worm_ids=[1], crop_xyxy=box)))
            (tracking/'tracking-quality.csv').write_text('frame,worm_id,low_confidence\n')
            folder = base/'phenotypes-centerlines-body-sequence'
            folder.mkdir()
            (folder/'summary.json').write_text(json.dumps(dict(crop_xyxy=box)))
            x = 55 if slot == 22 else 67
            mask = np.zeros((78,96),np.uint8)
            mask[28:45,x-17:x+17] = 255
            points = np.array([[x-13,36],[x+12,36]], dtype=float)
            lines = {}
            with (folder/'per_frame_metrics.csv').open('w', newline='') as handle:
                w = csv.writer(handle)
                w.writerow(['frame','worm_id','mask_valid','centerline_valid','identity_valid'])
                for frame in range(6):
                    valid = not (slot == 22 and frame == 4)
                    w.writerow([frame,1,True,valid,True])
                    if valid: lines[f'w1_f{frame}'] = points
                    cv2.imencode('.png',mask)[1].tofile(str(masks/f'{frame:06d}.png'))
            np.savez(folder/'centerlines.npz', **lines)
        batch.add(dict(sample='same-name',video=str(self.root/'other'/self.video.name),chamber_id=22))
        batch.start()
        group = batch.snapshot()['group_id']
        batch.take(); batch.finish('complete')
        batch.take(); batch.finish('failed')
        batch.take(); batch.finish('failed')
        request = dict(group=group, sample='slot-22')
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.post('/api/chamber-video',request,token=False)
        self.assertEqual(denied.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as partial:
            self.post('/api/chamber-video',request)
        self.assertEqual(partial.exception.code,400)
        with self.post('/api/chamber-video',dict(request,missing=[23])) as response:
            partial_job=json.load(response)['job']
        batch.retry(batch.items[1]['id'], start=True, group=group)
        batch.take(); batch.finish('complete')
        with self.post('/api/chamber-video',request) as response:
            job=json.load(response)['job']
        with self.post('/api/chamber-video',dict(request,sample='slot-23')) as response:
            self.assertEqual(job,json.load(response)['job'])
        self.assertNotEqual(job,partial_job)
        for key in [partial_job,job]:
            for _ in range(150):
                with urllib.request.urlopen(self.url+'/api/chamber-video?job='+key) as response:
                    state=json.load(response)
                if state['status'] != 'running': break
                time.sleep(.1)
            self.assertEqual(state['status'],'complete',state)
        with urllib.request.urlopen(self.url+'/artifact?chamber_video='+job) as response:
            output=self.root/'export.mp4'
            output.write_bytes(response.read())
        cap=cv2.VideoCapture(str(output))
        try:
            self.assertEqual((int(cap.get(3)),int(cap.get(4)),cap.get(5),int(cap.get(7))), (128,80,12,6))
            for frame in range(6):
                ok,pixels=cap.read()
                self.assertTrue(ok)
                for x in [55,99]:
                    # Slot 22 is inside slot 23's ROI and must survive its draw.
                    self.assertGreater(int(pixels[28,x,1]),135)
                    self.assertEqual(int(pixels[36,x,0])>150,frame!=4 or x==99)
        finally:
            cap.release()
        ready,_=members_for_video(batch.group_items(group),'slot-22',single=True)
        self.assertEqual([i['chamber_id'] for i in ready],[22])

    def test_next_video_validates_before_reset_and_refresh_keeps_annotation(self):
        self.progress.fail('example failure')
        with self.assertRaises(urllib.error.HTTPError): self.post('/api/new-video',{'video_path':str(self.video)},False)
        with self.assertRaises(urllib.error.HTTPError): self.post('/api/new-video',{'video_path':str(self.root/'missing.mp4')})
        self.assertEqual(self.progress.snapshot()['status'], 'failed')
        with self.post('/api/new-video',{'video_path':str(self.video),'preview_seconds':.25}) as response:
            self.assertEqual(json.load(response)['annotation_url'], '/')
        annotation = self.progress.server.annotation
        self.assertEqual(annotation.n, 6)
        self.assertEqual(annotation.frame, 3)
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
        self.assertEqual(self.progress.server.annotation.settings['centerline_method'], 'body_sequence')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post('/api/new-video', {'video_path':str(self.video)})
        self.assertEqual(error.exception.code,409)
        self.assertEqual(self.progress.wait()['video'],str(self.video.resolve()))

    def test_empty_workspace_can_close_without_a_video(self):
        self.progress.data.update(status='ready')
        with self.post('/close', {}) as response:
            self.assertTrue(json.load(response)['closed'])
        self.assertTrue(self.progress.closed.is_set())

    def test_slow_resume_cannot_replace_a_newly_finished_run(self):
        self.progress.fail('first run ended')
        old = dict(sample='old', video=str(self.video), centerline_method='body_sequence')
        entered, release = threading.Event(), threading.Event()

        def read_config(root, sample):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('pending resume did not finish')
            return root/'outputs'/sample, old

        with ThreadPoolExecutor(1) as client, patch('workspace_sessions.load_config', side_effect=read_config):
            request = client.submit(self.post, '/api/resume-run', {'sample': 'old'})
            try:
                self.assertTrue(entered.wait(3))
                self.progress.configure(dict(old, sample='new'), dict(frames=6, ids=[1]), [], self.root/'new')
                self.progress.fail('new run ended')
            finally:
                release.set()
            with self.assertRaises(urllib.error.HTTPError) as error:
                request.result(timeout=3)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.progress.snapshot()['sample'], 'new')
        self.assertFalse(self.progress.next_ready.is_set())

    def test_cancel_before_dispatch_leaves_only_the_replacement_for_worker(self):
        self.progress.data['status'] = 'ready'
        with self.post('/api/new-video', {'video_path': str(self.video)}):
            pass
        with self.post('/api/cancel', {}):
            pass
        old = self.progress.server.annotation
        self.assertTrue(old.done.is_set())
        self.assertFalse(self.progress.next_ready.is_set())
        with self.post('/api/new-video', {'video_path': str(self.video)}):
            pass
        new = self.progress.server.annotation
        self.assertIsNot(new, old)
        settings = self.progress.wait()
        self.assertEqual(settings, new.settings)
        self.assertIsNot(settings, new.settings)
        self.assertFalse(self.progress.next_ready.is_set())
        self.assertIsNone(self.progress.next_settings)

    def test_cancel_preparation_keeps_draft_and_releases_annotation(self):
        from workspace_sessions import save_draft,get_draft
        self.progress.data.update(status='ready')
        with self.post('/api/new-video',{'video_path':str(self.video)}): pass
        settings = self.progress.wait()
        annotation=self.progress.server.annotation
        save_draft(annotation,{'note':'synthetic draft'})
        with self.post('/api/cancel',{}): pass
        self.assertTrue(annotation.done.is_set())
        self.assertIsNone(annotation.result)
        self.assertEqual(self.progress.snapshot()['status'],'cancelled')
        self.assertEqual(get_draft(annotation)['body']['note'],'synthetic draft')
        with patch.object(manual, 'Annotation', side_effect=AssertionError('cancelled editor reopened')):
            self.assertIsNone(manual.collect_annotation(settings, reuse=False, progress=self.progress))

    def test_resumed_session_publishes_config_and_anchors_before_dispatch(self):
        config = dict(sample='older', video=str(self.video), centerline_method='body_sequence',
                      head_annotations={'1': dict(frame=2, point=[12, 15])})
        self.progress.configure(dict(config, sample='previous'), dict(frames=6, ids=[1]), [], self.root/'previous')
        self.progress.fail('previous run ended')
        with patch('workspace_sessions.load_config', return_value=(self.root/'older', config)):
            with self.post('/api/resume-run', {'sample':'older'}):
                pass
        with self.assertRaises(urllib.error.HTTPError) as stale:
            urllib.request.urlopen(self.url+'/api/head-anchors?run=previous')
        self.assertEqual(stale.exception.code, 409)
        with urllib.request.urlopen(self.url+'/api/head-anchors?run=older') as response:
            self.assertEqual(json.load(response)['anchors']['1'][0]['point'], [12, 15])
        self.assertEqual(self.progress.snapshot()['status'], 'preparing')
        self.assertEqual(self.progress.wait(), dict(config, _resume_saved=True))

    def test_file_upload_stays_in_import_directory(self):
        self.progress.fail('example failure')
        req=urllib.request.Request(self.url+'/api/upload-video?name=..%2Fsecond.mp4',self.video.read_bytes(),headers={'X-Annotation-Token':self.progress.token})
        with urllib.request.urlopen(req) as response: self.assertEqual(response.status,200)
        imported=Path(self.progress.server.annotation.video)
        self.assertTrue(imported.is_relative_to(self.root/'input_videos'))
        self.assertEqual(imported.read_bytes(),self.video.read_bytes())

    def test_progress_responds_while_a_large_download_is_paused(self):
        path = self.root / 'large.mp4'
        with path.open('wb') as stream:
            stream.truncate(32 * 1024 * 1024)
        self.progress.artifacts['video'] = path
        client = http.client.HTTPConnection('127.0.0.1', self.progress.server.server_port, timeout=3)
        try:
            client.connect()
            client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
            client.request('GET', '/artifact?key=video', headers={'Range': 'bytes=128-'})
            with client.getresponse() as download:
                self.assertEqual(download.status, 206)
                self.assertEqual(download.read(16), bytes(16))
                with urllib.request.urlopen(self.url + '/api/progress', timeout=3) as response:
                    self.assertEqual(json.load(response)['token'], self.progress.token)
        finally:
            client.close()

    def test_upload_rechecks_destination_after_a_concurrent_workspace_change(self):
        with self.post('/api/library-tree', {'op': 'mkdir', 'name': 'target'}) as response:
            folder = json.load(response)['folder']['id']
        client = http.client.HTTPConnection('127.0.0.1', self.progress.server.server_port, timeout=3)
        try:
            client.request('POST', '/api/library-upload?name=worms.csv&parent=' + folder,
                           body=b'ab', headers={'Content-Length': '4', 'X-Annotation-Token': self.progress.token})
            staging = self.root / 'input_videos/library'
            deadline = time.monotonic() + 3
            while not list(staging.glob('*/upload.part')):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            with urllib.request.urlopen(self.url + '/api/progress', timeout=3) as response:
                self.assertEqual(response.status, 200)
            with self.post('/api/library-tree', {'op': 'remove', 'id': folder}):
                pass
            client.send(b'cd')
            with client.getresponse() as response:
                self.assertEqual(response.status, 400)
            self.assertEqual(list(staging.iterdir()), [])
            with urllib.request.urlopen(self.url + '/api/library') as response:
                self.assertEqual(json.load(response)['files'], [])
        finally:
            client.close()

    def test_late_video_upload_does_not_replace_the_new_annotation(self):
        self.progress.fail('test')
        data = self.video.read_bytes()
        client = http.client.HTTPConnection('127.0.0.1', self.progress.server.server_port, timeout=3)
        try:
            client.request('POST', '/api/upload-video?name=slow.mp4', body=data[:1],
                           headers={'Content-Length': str(len(data)), 'X-Annotation-Token': self.progress.token})
            imports = self.root / 'input_videos'
            deadline = time.monotonic() + 3
            while not list(imports.glob('*/slow.mp4')):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            with self.post('/api/new-video', {'video_path': str(self.video)}):
                pass
            client.send(data[1:])
            with client.getresponse() as response:
                self.assertEqual(response.status, 400)
            self.assertEqual(Path(self.progress.server.annotation.video), self.video)
            self.assertEqual(list(imports.iterdir()), [])
        finally:
            client.close()

    def test_completed_summary_nulls_are_not_reported_as_zero(self):
        config=dict(sample='done',video=str(self.video),centerline_method='body_sequence')
        self.progress.configure(config,dict(frames=6,ids=[1]),[dict(name='metrics',phase='metrics')],self.root)
        folder=self.root/'phenotypes-centerlines-body-sequence';folder.mkdir()
        (folder/'worm_summary.csv').write_text('worm_id,frames,valid_frames,median_length_um,mean_speed_um_s\n1,6,0,,nan\n',encoding='utf-8')
        self.progress.done(0);self.progress.finish(self.root,config,'metrics')
        row=self.progress.snapshot()['results'][0]
        self.assertEqual(row['valid_frames'],0)
        self.assertIsNone(row['median_length_um'])
        self.assertIsNone(row['mean_speed_um_s'])

    def test_read_only_history_while_another_video_is_running(self):
        base = self.root/'outputs'/'older'
        folder = base/'phenotypes-centerlines-body-sequence'
        folder.mkdir(parents=True)
        (base/'pipeline-config.json').write_text(json.dumps(dict(sample='older',video=str(self.video),centerline_method='body_sequence')))
        (base/'pipeline-state.json').write_text(json.dumps(dict(stages={'metrics':{'status':'complete'}})))
        (folder/'summary.json').write_text(json.dumps(dict(fps=12, worms=[dict(worm_id=1, frames=1, valid_frames=1)])))
        csv_bytes = b'frame,worm_id\n0,1\n'
        (folder/'per_frame_metrics.csv').write_bytes(csv_bytes)
        self.progress.data.update(sample='current',status='running')
        before = self.progress.run_id
        with urllib.request.urlopen(self.url+'/api/history-result?run=older') as response:
            result = json.load(response)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['video_path'], str(self.video))
        with urllib.request.urlopen(self.url+'/artifact?history=1&run=older&key=metrics') as response:
            self.assertEqual(response.read(), csv_bytes)
        with urllib.request.urlopen(self.url+'/api/saved-timeline?history=1&detail=1&run=older') as response:
            self.assertEqual(json.load(response)['frame_count'], 1)
        self.assertEqual((self.progress.run_id,self.progress.data['sample'],self.progress.data['status']), (before,'current','running'))
        self.assertIsNone(self.progress.server.annotation)

    def test_reopened_result_uses_saved_duration_without_changing_history(self):
        from workspace_sessions import reopen
        base=self.root/'outputs'/'saved';folder=base/'phenotypes-centerlines-body-sequence'
        folder.mkdir(parents=True)
        (folder/'summary.json').write_text(json.dumps({'worms':[{'frames':6}], 'fps':12}),encoding='utf-8')
        (base/'pipeline-state.json').write_text(json.dumps({'stages':{'metrics':{'status':'complete'}}}),encoding='utf-8')
        config=dict(sample='saved',video=str(self.video),centerline_method='body_sequence')
        record=base/'pipeline-progress.json'
        cases=[({'sample':'saved','status':'complete','elapsed_seconds':125.5,'finished_at':123456,'device':'saved GPU'},125.5),
               ({'sample':'other','status':'complete','elapsed_seconds':900},None),
               ({'sample':'saved','status':'running','elapsed_seconds':40},None),
               ({'sample':'saved','status':'complete','elapsed_seconds':-1},None),
               ({'sample':'saved','status':'complete','elapsed_seconds':True},None),
               ({},None)]
        for saved,expected in cases:
            with self.subTest(saved=saved):
                record.write_text(json.dumps(saved),encoding='utf-8');before=record.read_bytes()
                self.progress.started=1  # An unrelated previous session must not leak into history.
                self.progress.data['device']='old GPU'
                with patch('workspace_sessions.load_config',return_value=(base,config)):
                    reopen(self.progress,self.root,'saved')
                result=self.progress.snapshot()
                self.assertTrue(result['history_reopened'])
                self.assertEqual(result['elapsed_seconds'],expected)
                self.assertIsNone(result['preview_run'])
                self.assertEqual(result['status'],'complete')
                self.assertEqual(record.read_bytes(),before)
                self.assertEqual(result['device'],'saved GPU' if expected else '')
        record.unlink()
        with patch('workspace_sessions.load_config',return_value=(base,config)):
            reopen(self.progress,self.root,'saved')
        self.assertIsNone(self.progress.snapshot()['elapsed_seconds'])
        self.progress.configure(config,dict(frames=6,ids=[1]),[],base)
        current=self.progress.snapshot()
        self.assertFalse(current['history_reopened'])
        self.assertIsNotNone(current['elapsed_seconds'])
        self.assertIsNone(current['finished_at'])

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

    def test_reannotation_rejection_consumes_the_body_before_connection_close(self):
        body = b'{"frame":3}'
        stream = io.BytesIO(b'POST /api/reannotate HTTP/1.0\r\nContent-Length: '
                            + str(len(body)).encode() + b'\r\n\r\n' + body)
        connection = Mock()
        connection.makefile.return_value = stream
        with patch.object(stream, 'close'):
            self.progress.server.RequestHandlerClass(connection, ('127.0.0.1', 1), self.progress.server)
        response = b''.join(call.args[0] for call in connection.sendall.call_args_list)
        self.assertTrue(response.startswith(b'HTTP/1.0 403 '))
        self.assertEqual(stream.read(), b'')

    def test_reannotation_previews_and_uses_selected_frame_in_fresh_run(self):
        base=self.root/'outputs'/'old';base.mkdir(parents=True)
        saved=base/'original.txt';saved.write_text('preserve old annotation and results')
        config=dict(sample='old',video=str(self.video),centerline_method='body_sequence',scale_um_per_px=2.5,
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
        self.assertEqual(annotation.settings['centerline_method'],'body_sequence')
        self.assertNotEqual(annotation.settings['sample'],'old')
        self.assertEqual(annotation.settings['previous_sample'],'old')
        self.assertEqual(annotation.settings['groups'],[])
        self.assertEqual(saved.read_text(),'preserve old annotation and results')


if __name__ == '__main__': unittest.main()
