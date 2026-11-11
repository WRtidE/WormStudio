import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

import cv2
import numpy as np
import manual_worm_annotation as manual
from batch_queue import BatchQueue
from pipeline_progress import PipelineProgress


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = BatchQueue(self.root / 'batch.json')

    def tearDown(self):
        self.temp.cleanup()

    def add(self, name):
        self.queue.add(dict(sample=name, video=name+'.mp4', scale_um_per_px=len(name)))

    def test_fifo_failure_continues_and_cancel_preserves_pending(self):
        for name in ('one', 'two', 'three'): self.add(name)
        self.queue.start()
        self.assertEqual(self.queue.take()['sample'], 'one')
        self.assertIsNone(self.queue.take())
        self.queue.finish('failed', 'bad input')
        self.assertEqual(self.queue.take()['sample'], 'two')
        self.queue.finish('cancelled', cancelled=True)
        self.assertIsNone(self.queue.take())
        self.assertEqual(self.queue.items[2]['status'], 'pending')
        self.queue.start()
        self.assertEqual(self.queue.take()['sample'], 'three')
        self.queue.finish('complete')
        self.assertFalse(self.queue.running)

    def test_restart_requires_explicit_start_and_restores_config(self):
        self.add('one'); self.add('second')
        self.queue.start(); self.queue.take()
        restored = BatchQueue(self.queue.path)
        self.assertFalse(restored.running)
        self.assertIsNone(restored.take())
        restored.start()
        self.assertEqual(restored.take()['scale_um_per_px'], 3)
        restored.finish('complete')
        self.assertEqual(restored.take()['scale_um_per_px'], 6)

    def test_running_queue_rejects_mutations_and_retry_is_explicit(self):
        self.add('one'); self.queue.start(); self.queue.take()
        identifier = self.queue.items[0]['id']
        with self.assertRaises(ValueError): self.queue.remove(identifier)
        with self.assertRaises(ValueError): self.add('two')
        self.queue.finish('failed')
        self.queue.retry(identifier)
        self.assertEqual(self.queue.items[0]['status'], 'pending')

    def test_result_group_excludes_older_jobs_and_survives_restart(self):
        self.add('older');self.queue.start();self.queue.take();self.queue.finish('complete')
        for name in ('first','second','third'):self.add(name)
        self.queue.start(samples=['first','second','third'])
        self.queue.take();self.queue.finish('complete',result_sample='first-revision')
        self.queue.take();self.queue.finish('failed','bad frame')
        self.queue.take();self.queue.finish('complete')
        restored=BatchQueue(self.queue.path)
        group=restored.snapshot()['result_items']
        self.assertEqual([i['sample'] for i in group],['first-revision','second','third'])
        self.assertEqual([i['sample'] for i in group if i['status']=='complete'],['first-revision','third'])
        self.assertEqual(len(restored.snapshot()['items']),4)
        failed=next(i for i in restored.items if i['status']=='failed')
        restored.retry(failed['id']);restored.start();restored.take();restored.finish('complete')
        self.assertEqual([i['sample'] for i in restored.snapshot()['result_items']],['first-revision','second','third'])
        restored.add(dict(sample='new',video='new.mp4'));restored.start(samples=['new'])
        self.assertEqual([i['sample'] for i in restored.snapshot()['result_items']],['new'])

    def test_legacy_queue_does_not_guess_a_result_group(self):
        self.add('old');self.queue.items[0]['status']='complete';self.queue.persist()
        self.assertEqual(BatchQueue(self.queue.path).snapshot()['result_items'],[])

    def test_start_failure_does_not_change_the_active_result_group(self):
        self.add('one')
        with patch.object(Path,'replace',side_effect=OSError('read only')):
            with self.assertRaises(OSError):self.queue.start(samples=['one'])
        self.assertFalse(self.queue.running)
        self.assertEqual(self.queue.result_samples,[])

    def test_stop_requested_during_startup_survives_configure(self):
        self.add('one'); self.queue.start(); self.queue.take()
        progress = PipelineProgress(); progress.batch = self.queue
        progress.cancel_requested.set()
        progress.configure(dict(sample='one', video='one.mp4', centerline_method='niu'),
                           dict(ids=[1], frames=5), [], self.root / 'run')
        self.assertTrue(progress.cancel_requested.is_set())
        self.queue.finish('cancelled', cancelled=True)
        self.assertFalse(self.queue.running)

    def test_two_real_annotations_save_without_dispatch_then_worker_receives_configs(self):
        self.check_annotations(automatic=False)

    def test_multivideo_sequence_advances_and_only_runs_its_own_files(self):
        self.add("older-pending")
        self.check_annotations(automatic=True)

    def check_annotations(self, automatic):
        progress = PipelineProgress()
        progress.batch = self.queue
        progress.data['status'] = 'ready'
        with patch.object(manual, 'ROOT', self.root):
            progress.bind_server(manual.create_server(progress=progress))
            url = progress.url.removesuffix('/progress')
            def post(route, body, token):
                request = urllib.request.Request(url+route, json.dumps(body).encode(),
                    {'Content-Type':'application/json', 'X-Annotation-Token':token})
                with urllib.request.urlopen(request) as response: return json.load(response)
            try:
                for index in range(2):
                    video = self.root / f'input-{index}.mp4'
                    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'mp4v'), 12, (128, 80))
                    for _ in range(5): writer.write(np.zeros((80,128,3), np.uint8))
                    writer.release()
                paths = [str(self.root / f'input-{index}.mp4') for index in range(2)]
                if automatic:
                    post('/api/new-video', dict(video_path=paths[0], sequence=paths), progress.token)
                for index, path in enumerate(paths):
                    if not automatic:
                        post('/api/new-video', dict(video_path=path, batch=True), progress.token)
                    else:
                        with urllib.request.urlopen(url+'/state') as response: state=json.load(response)
                        self.assertEqual(state['sequence_index'], index+1)
                        self.assertEqual(state['sequence_total'], 2)
                        self.assertEqual(state['video_name'], Path(path).name)
                        self.assertFalse(self.queue.running)
                    self.assertFalse(progress.next_ready.is_set())
                    mask = np.zeros((80,128), np.uint8); mask[20:40,30:70] = 255
                    body = dict(worms=[dict(id=1,mask='data:image/png;base64,'+base64.b64encode(manual.png(mask)).decode())],
                                roi=None, measurement_mode='physical', scale_um_per_px=index+1)
                    with patch('tapnext_support.resources'), patch('tapnext_support.dependency_path'):
                        saved = post('/save', body, progress.server.annotation.token)
                    if automatic:
                        if index == 0:
                            self.assertTrue(saved['next_annotation'])
                            self.assertEqual(progress.data['status'], 'preparing')
                        else:
                            self.assertEqual(saved['progress_url'], '/progress')
                            self.assertTrue(self.queue.running)
                            self.assertEqual(len(self.queue.snapshot()['result_items']),2)
                            self.assertNotIn('older-pending',[i['sample'] for i in self.queue.snapshot()['result_items']])
                    else:
                        self.assertTrue(saved['queued'])
                        self.assertEqual(progress.data['status'], 'ready')
                    self.assertFalse(progress.next_ready.is_set())
                self.assertEqual(len(self.queue.items), 3 if automatic else 2)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post('/api/batch', dict(action='start'), 'wrong-token')
                self.assertEqual(error.exception.code, 403)
                if not automatic: post('/api/batch', dict(action='start'), progress.token)
                for index in range(2):
                    config = progress.wait()
                    self.assertTrue(config['_resume_saved'])
                    self.assertEqual(config['video'], str(self.root / f'input-{index}.mp4'))
                    self.assertEqual(config['scale_um_per_px'], index+1)
                    self.assertTrue(Path(config['groups'][0]['worms'][0]['mask_path']).is_file())
                    self.queue.finish('complete')
                self.assertFalse(self.queue.running)
                if automatic: self.assertEqual(self.queue.items[0]['status'], 'pending')
            finally:
                progress.close()
