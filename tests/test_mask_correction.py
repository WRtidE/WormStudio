import base64
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import cv2
import numpy as np

import manual_worm_annotation as manual
from live_preview import report_preview
from mask_correction import request_edit
from pipeline_progress import PipelineProgress
from worm_pipeline import run_stage, validate_config


class MaskCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='worm-mask-edit-')
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(manual, 'ROOT', self.root)
        self.root_patch.start()
        video = self.root / 'video.mp4'
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'mp4v'), 12, (128, 80))
        self.assertTrue(writer.isOpened())
        for _ in range(8): writer.write(np.full((80, 128, 3), 130, np.uint8))
        writer.release()
        self.base = self.root / 'outputs' / 'source'
        self.base.mkdir(parents=True)
        self.mask = np.zeros((60, 100), np.uint8); self.mask[20:35, 25:50] = 255
        seed = self.base / 'seed.png'; seed.write_bytes(manual.png(self.mask[5:55, 10:80]))
        seed2 = self.base / 'seed2.png'; seed2.write_bytes(manual.png(self.mask))
        self.config = dict(sample='source', video=str(video), crop_xyxy=[10, 10, 110, 70],
            measurement_mode='pixels', scale_um_per_px=None, centerline_method='niu', sam2_model='large',
            head_detection='manual_seed', head_annotations={'1':[dict(frame=0, point=[25,20]), dict(frame=3, point=[25,20])]},
            groups=[dict(name='first', crop_xyxy=[20,15,90,65], anchor_frame=0,
                         worms=[dict(id=1, mask_path=str(seed))],
                         additional_anchors=[dict(frame=2, worms=[dict(id=1, mask_path=str(seed))])]),
                    dict(name='second', crop_xyxy=[10,10,110,70], anchor_frame=0,
                         worms=[dict(id=2, mask_path=str(seed2))])])
        # Only the first group has reached this frame; the second must be retained unchanged.
        self.mask_path = self.base / 'sam2-tracking' / 'masks' / 'worm-1' / '000003.png'
        self.mask_path.parent.mkdir(parents=True)
        self.mask_path.write_bytes(manual.png(self.mask))
        self.progress = PipelineProgress()
        self.progress.configure(self.config, dict(frames=8,ids=[1,2]), [dict(name='tracking',phase='tracking')], self.base)
        self.progress.bind_server(manual.create_server(progress=self.progress))
        with patch.dict(os.environ, WORM_PREVIEW_DIR=str(self.progress.preview_dir)):
            report_preview(np.zeros((60,100,3),np.uint8), 3, 8)
        self.run = self.progress.run_id

    def tearDown(self):
        self.progress.close(); self.root_patch.stop(); self.temp.cleanup()

    def stopped_editor(self):
        self.progress.fail('stopped for edit', cancelled=True)
        result = request_edit(self.progress, self.root, self.run, 1)
        self.assertEqual(result['frame'], 3)
        return self.progress.server.annotation

    def body(self, annotation):
        state = annotation.initial_state()
        return dict(worms=[dict(id=1,mask=state['previous']['worms'][0]['mask'])], roi=[0,0,100,60],
                    measurement_mode='pixels',scale_um_per_px=None,centerline_method='niu',head_detection='manual_seed')

    def test_waits_for_stop_then_loads_unconfirmed_native_mask_and_retries_idempotently(self):
        result = request_edit(self.progress, self.root, self.run, 1)
        self.assertTrue(result['waiting']); self.assertTrue(self.progress.cancel_requested.is_set())
        self.assertIsNone(self.progress.server.annotation)
        annotation = self.stopped_editor()
        self.assertTrue(self.progress.next_ready.is_set())
        self.assertFalse(self.progress.cancel_requested.is_set())
        state = annotation.initial_state()
        self.assertEqual((state['frame'],state['width'],state['height']),(3,100,60))
        self.assertFalse(state['previous']['worms'][0]['confirmed'])
        data = base64.b64decode(state['previous']['worms'][0]['mask'].split(',')[1])
        np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(data,np.uint8),-1)[:,:,3],self.mask)
        self.assertIn('mask_correction',annotation.identity)
        again=request_edit(self.progress,self.root,self.run,1)
        self.assertEqual(again['annotation_url'],'/'); self.assertIs(self.progress.server.annotation,annotation)

    def test_save_adds_anchor_keeps_other_groups_and_previous_anchors_without_overwrites(self):
        original=copy.deepcopy(self.config)
        old_files={path:path.read_bytes() for path in self.base.rglob('*.png')}
        annotation=self.stopped_editor(); annotation.save(self.body(annotation))
        config=annotation.result
        checked,info=validate_config(config)
        self.assertEqual(info['ids'],[1,2]); self.assertNotEqual(config['sample'],'source')
        self.assertEqual(config['previous_sample'],'source')
        self.assertEqual(config['groups'][1],original['groups'][1])
        group=config['groups'][0]
        self.assertEqual(group['worms'],original['groups'][0]['worms'])
        self.assertEqual([a['frame'] for a in group['additional_anchors']],[2,3])
        mask_path=Path(group['additional_anchors'][1]['worms'][0]['mask_path'])
        np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(mask_path.read_bytes(),np.uint8),0),self.mask[5:55,10:80])
        self.assertEqual([a['frame'] for a in config['head_annotations']['1']],[0])
        self.assertEqual(config['mask_correction']['worm_ids'],[1])
        self.assertEqual(self.config,original)
        for path,data in old_files.items(): self.assertEqual(path.read_bytes(),data)
        self.assertIn('tracking',[s['phase'] for s in __import__('worm_pipeline').build_stages(checked,info,annotation.path,self.base/'phenotypes-niu')])

    def test_replacing_existing_anchor_does_not_duplicate_it(self):
        self.progress.config['groups'][0]['additional_anchors'][0]['frame']=3
        annotation=self.stopped_editor();annotation.save(self.body(annotation))
        self.assertEqual([a['frame'] for a in annotation.result['groups'][0]['additional_anchors']],[3])
        validate_config(annotation.result)

    def test_wrong_ids_region_and_group_coordinates_rejected_before_save(self):
        annotation=self.stopped_editor()
        for mutate in (lambda b:b['worms'][0].update(id=9),lambda b:b.update(roi=[1,0,100,60])):
            body=self.body(annotation);mutate(body)
            with self.assertRaises(ValueError): annotation.save(body)
            self.assertFalse(annotation.done.is_set()); self.assertFalse(annotation.path.exists())
        bad=self.mask.copy();bad[0:4,0:10]=255
        body=self.body(annotation);body['worms'][0]['mask']='data:image/png;base64,'+base64.b64encode(manual.png(bad)).decode()
        with self.assertRaisesRegex(ValueError,'原跟踪区域'):annotation.save(body)

    def test_stale_or_invalid_requests_do_not_stop_the_worker(self):
        for run,sequence in [('other',1),(self.run,0),(self.run,2),(self.run,True),(self.run,'../x')]:
            with self.assertRaises(ValueError):request_edit(self.progress,self.root,run,sequence)
            self.assertFalse(self.progress.cancel_requested.is_set())
        self.progress.data['read_only']=True
        with self.assertRaises(ValueError):request_edit(self.progress,self.root,self.run,1)
        self.progress.data['read_only']=False
        self.mask_path.unlink()
        with self.assertRaisesRegex(ValueError,'没有已保存'):request_edit(self.progress,self.root,self.run,1)
        self.assertFalse(self.progress.cancel_requested.is_set())

    def test_http_auth_and_locked_frame(self):
        base=self.progress.url.removesuffix('/progress')
        def post(path,body,token):
            return urllib.request.urlopen(urllib.request.Request(base+path,json.dumps(body).encode(),
                headers={'Content-Type':'application/json','X-Annotation-Token':token}),timeout=5)
        with self.assertRaises(urllib.error.HTTPError) as error:
            post('/api/edit-preview-mask',dict(run=self.run,sequence=1),'wrong')
        self.assertEqual(error.exception.code,403);self.assertFalse(self.progress.cancel_requested.is_set())
        with post('/api/edit-preview-mask',dict(run=self.run,sequence=1),self.progress.token) as response:
            self.assertEqual(response.status,202)
        annotation=self.stopped_editor()
        with self.assertRaises(urllib.error.HTTPError) as error:
            post('/api/annotation-frame',dict(frame=4),annotation.token)
        self.assertEqual(error.exception.code,400);self.assertEqual(annotation.frame,3)

    def test_edit_waits_for_real_child_process_termination(self):
        marker=self.base/'child-started'
        command=[sys.executable,'-c',f'import time; from pathlib import Path; Path({str(marker)!r}).write_text("ready"); time.sleep(20)']
        self.progress.begin(0,self.base/'child.log')
        def worker():
            try:run_stage(dict(name='test',command=command),self.base/'child.log',self.progress)
            except RuntimeError as error:self.progress.fail(error,cancelled=True)
        thread=threading.Thread(target=worker);thread.start()
        try:
            deadline=time.monotonic()+8
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.03)
            self.assertTrue(marker.exists())
            self.assertTrue(request_edit(self.progress,self.root,self.run,1)['waiting'])
            thread.join(8);self.assertFalse(thread.is_alive())
            self.assertEqual(self.progress.snapshot()['status'],'cancelled')
            self.assertEqual(request_edit(self.progress,self.root,self.run,1)['annotation_url'],'/')
        finally:
            self.progress.cancel_requested.set();thread.join(8)


if __name__=='__main__': unittest.main()
