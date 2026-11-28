import io
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from openpyxl import load_workbook

from channel_counts import ChannelCounts, event_images, read, save
from wormtracker.core.counter import count_crossings
from workspace_http import PreviewFrameReader
from workspace_library import register, select, listing
from wormtracker.engine.mog2 import MOG2Engine


def crossing_video(path, frames=130, up_start=27):
    base = np.full((360, 640, 3), 176, np.uint8)
    for x in (65, 185, 305, 425, 545):
        cv2.line(base, (x, 0), (x, 359), (52, 52, 52), 4)
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 12, (640,360))
    try:
        for i in range(frames):
            frame = base.copy()
            if up_start <= i < up_start + 33:
                cv2.ellipse(frame,(125,295-(i-up_start)*5),(7,13),0,0,360,(28,28,28),-1)
            if 70 <= i < 103:
                cv2.ellipse(frame,(365,143+(i-70)*5),(7,13),0,0,360,(28,28,28),-1)
            out.write(frame)
    finally:
        out.release()


class ChannelIntegration(unittest.TestCase):
    def test_review_and_replay_use_the_grid_version_of_the_original_run(self):
        from channel_runs import new_run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root/'lanes.avi'
            crossing_video(video, frames=8)
            service = ChannelCounts(root)
            file = register(root, video)['id']
            try:
                current = new_run()
                with patch.object(service, 'launch'):
                    service.start_run(file, video, current, dict(num_channels=4))
                state = read(service.folder(file)/current/'state.json')
                self.assertEqual(state['grid_version'], 2)
                old = dict(state, id=new_run(), frame=3, status='interrupted')
                del old['grid_version']
                bounds = []
                for task, method in [(state, 'peaks'), (old, 'equidistant')]:
                    folder = service.folder(file)/task['id']
                    with patch.object(service, 'launch'):
                        service.resume_run(video, task)
                    final = service._count_video(video, folder, dict(state=task, paused=False), None, False)
                    task.update(final)
                    save(folder/'state.json', task)
                    preview = service.setup(file, 4, run=task['id'])
                    reviewed = service.frames(file, task['id'], 1, 1)['frames'][0]
                    for image in (final['picture'], preview, reviewed):
                        self.assertEqual(image['grid_method'], method)
                        self.assertEqual(image['bounds'], preview['bounds'])
                    bounds.append(preview['bounds'])
                self.assertNotEqual(*bounds)
            finally:
                service.close()

    def test_gpu_failure_after_crossing_replays_on_cpu_without_duplicate_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root/'crossing.avi'
            crossing_video(video, frames=24, up_start=0)
            service = ChannelCounts(root)
            self.addCleanup(service.close)
            params = dict(num_channels=4)
            reference = dict(frame=0, events=[], file='test', settings=params)
            with patch('channel_counts.opencv_gpu_name', return_value=None):
                service.process(video, root, dict(state=reference, paused=False))
            self.assertEqual(len(reference['events']), 1)
            self.assertEqual(reference['device'], 'cpu')
            self.assertFalse(reference['gpu_fallback'])

            state = dict(frame=0, events=[], file='test', settings=params)
            morphology = MOG2Engine._apply_morphology

            def lose_gpu(engine, mask):
                if engine.use_gpu and state['events']:
                    raise cv2.error('test OpenCL device lost')
                return morphology(engine, mask)

            with patch('channel_counts.opencv_gpu_name', return_value='test GPU'), \
                    patch.object(MOG2Engine, '_apply_morphology', lose_gpu):
                service.process(video, root, dict(state=state, paused=False))
            self.assertEqual(state['status'], 'complete')
            self.assertEqual(state['device'], 'cpu')
            self.assertTrue(state['gpu_fallback'])
            self.assertEqual(state['frame'], 24)
            self.assertEqual(state['events'], reference['events'])
            self.assertEqual(state['counts'], reference['counts'])

    def test_review_detail_uses_the_requested_source_frame_at_native_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root/'detail.avi'
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 12, (1600, 480))
            try:
                for gray in (32, 192):
                    writer.write(np.full((480, 1600, 3), gray, dtype=np.uint8))
            finally:
                writer.release()
            counts, reader = ChannelCounts(root), PreviewFrameReader()
            try:
                file = register(root, video)['id']
                run = '20261125120000000000-00000001'
                save(counts.folder(file)/run/'state.json', dict(id=run, status='complete', frame=2))
                for number, gray in [(2, 192), (1, 32)]:
                    image = counts.frame_image(file, run, number, reader)
                    decoded = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
                    self.assertEqual(decoded.shape, (480, 1600))
                    self.assertAlmostEqual(float(decoded[240, 800]), gray, delta=2)
                with self.assertRaisesRegex(ValueError, '帧号超出'):
                    counts.frame_image(file, run, 3, reader)
            finally:
                reader.close()
                counts.close()

    def test_batch_uses_each_confirmed_line_even_when_drafts_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = ChannelCounts(root)
            self.addCleanup(service.close)
            files = []
            for name in ('first','second'):
                video = root/(name+'.avi')
                crossing_video(video, frames=2)
                files.append(register(root, video)['id'])
            first, second = files
            base = dict(num_channels=4, mask_top_ratio=.15, tripwire_ratio=.45, mask_bottom_ratio=.8)
            confirmed = {second:dict(base, tripwire_ratio=.6), first:base}
            service.action(first, 'draft', {'settings':dict(base, tripwire_ratio=.7)})
            for invalid in ({first:base}, {first:base,second:{}}, {first:base,second:dict(base,tripwire_ratio=.9)}):
                with self.assertRaises(ValueError):
                    service.batch.action(dict(action='start', id=str(uuid.uuid4()), files=files, settings_by_file=invalid))
            self.assertIsNone(service.batch.record['id'])
            rows = service.prepare_batch(dict(files=files, settings_by_file=confirmed))
            self.assertEqual([r['file'] for r in rows], files)
            self.assertEqual([r['settings']['tripwire_ratio'] for r in rows], [.45,.6])

    def test_comparison_removal_is_persistent_and_scoped_to_displayed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counts = ChannelCounts(root)
            self.addCleanup(counts.close)
            first, second = 'a'*32, 'b'*32
            older = '20261125100000000000-00000001'
            latest = '20261125110000000000-00000002'
            next_run = '20261125120000000000-00000003'
            for file, run in [(first, older), (first, latest), (second, older)]:
                save(counts.folder(file)/run/'state.json', dict(id=run, name=file, status='complete', counts=[2,0]))
            for _ in range(2):
                counts.action(first, 'remove-comparison', {'run':latest})
            reopened = ChannelCounts(root)
            self.addCleanup(reopened.close)
            self.assertEqual([row['file'] for row in reopened.comparison()], [second])
            self.assertEqual([(r['run'],r['removed']) for r in reopened.comparison(include_removed=True)],
                             [(latest,True),(older,False)])
            self.assertEqual(reopened.state(first)['history'], [latest,older])
            self.assertEqual(reopened.completed(first, latest)['counts'], [2,0])
            reopened.action(first, 'restore-comparison', {'run':latest})
            self.assertEqual(reopened.comparison()[0]['run'], latest)
            reopened.action(first, 'remove-comparison', {'run':latest})
            path = reopened.folder(first)/next_run/'state.json'
            state = dict(id=next_run, name=first, status='running', counts=[3])
            save(path, state)
            with self.assertRaisesRegex(ValueError, '已完成'):
                reopened.action(first, 'remove-comparison', {'run':next_run})
            self.assertEqual(len(reopened.comparison()), 1)
            save(path, dict(state, status='complete'))
            self.assertEqual(reopened.comparison()[0]['run'], next_run)

    def test_removing_count_history_preserves_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = ChannelCounts(root)
            self.addCleanup(service.close)
            file = 'b'*32
            older = '20261125100000000000-00000001'
            newer = '20261125110000000000-00000002'
            for run in (older, newer):
                save(service.folder(file)/run/'state.json', dict(id=run, status='complete', counts=[1]))
            self.assertEqual(service.state(file)['history'], [newer, older])
            with self.assertRaisesRegex(ValueError, '任务编号无效'):
                service.action(file, 'remove-history', {})
            with self.assertRaisesRegex(ValueError, '计数记录不存在'):
                service.action(file, 'remove-history', {'run': newer[:-1]+'3'})
            service.action(file, 'remove-history', {'run': newer})
            service.action(file, 'remove-history', {'run': newer})
            reopened = ChannelCounts(root)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.state(file)['history'], [older])
            self.assertEqual(reopened.state(file)['task']['id'], older)
            self.assertEqual(reopened.completed(file, newer)['counts'], [1])
            reopened.action(file, 'remove-history', {'run': older})
            self.assertIsNone(reopened.state(file)['task'])
            self.assertEqual(reopened.state(file)['history'], [])

    def test_batch_recovery_failure_isolation_and_scoped_retry(self):
        def settle(service):
            for _ in range(1200):
                snapshot = service.batch.snapshot()
                if not snapshot['running'] and not service.jobs:
                    return snapshot['items']
                time.sleep(.01)
            self.fail('batch did not settle')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            videos = [root/(name+'.avi') for name in ('first','missing','last')]
            for path in videos:
                crossing_video(path, frames=26, up_start=0)
            first, missing, last = [register(root,p)['id'] for p in videos]
            key = str(uuid.uuid4())
            body = dict(action='start', id=key, files=[first,missing,first,last], settings={'num_channels':4})
            service = ChannelCounts(root)
            try:
                service.action(first,'draft',{'settings':{'num_channels':7}})
                with service.condition:
                    initial = service.batch.action(body)['items']
                    self.assertEqual([r['file'] for r in initial], [first,missing,last])
                    self.assertEqual(service.batch.action(body)['items'],initial)
                    self.assertEqual(len(service.jobs),1)
                    with self.assertRaisesRegex(ValueError,'批次'):
                        service.action(last,'start',{'settings':{'num_channels':4}})
                    service.action(first,'pause',{'run':initial[0]['run']})
                    with self.assertRaisesRegex(ValueError, '计数结束后'):
                        service.action(first, 'remove-history', {'run': initial[0]['run']})
                    self.assertEqual(service.batch.snapshot()['items'][0]['status'],'paused')
            finally:
                service.close()
            service = ChannelCounts(root)
            try:
                recovered = service.batch.snapshot()
                self.assertFalse(recovered['running'])
                self.assertEqual([r['status'] for r in recovered['items']],['interrupted','pending','pending'])
                hidden = videos[1].with_suffix('.parked')
                videos[1].rename(hidden)
                with service.condition:
                    service.batch.action(dict(action='resume',id=key))
                    service.batch.action(dict(action='pause',id=key))
                paused = settle(service)
                self.assertEqual([r['status'] for r in paused], ['complete','pending','pending'])
                service.batch.action(dict(action='resume', id=key))
                rows = settle(service)
                self.assertEqual([r['status'] for r in rows], ['complete','failed','complete'])
                self.assertTrue(rows[1]['error'])
                book = load_workbook(io.BytesIO(service.export_batch(key)),read_only=True)
                self.assertEqual(book.active['G2'].value,1)
                self.assertEqual(book.active['I2'].value,initial[0]['run'])
                self.assertEqual(book.active['M2'].value,1)
                self.assertEqual(book.active['B3'].value,'失败')
                self.assertIsNone(book.active['G3'].value)
                self.assertIsNone(book.active['J3'].value)
                book.close()
                hidden.rename(videos[1])
                service.batch.action(dict(action='retry', id=key))
                rows = settle(service)
                self.assertEqual([r['total'] for r in rows], [1,1,1])
                self.assertEqual([r['run'] for r in rows[::2]], [r['run'] for r in initial[::2]])
                for row in rows:
                    state = service.state(row['file'],row['run'])
                    self.assertEqual(len(state['history']),1)
                    self.assertEqual(state['task']['settings']['num_channels'],4)
                self.assertEqual(service.state(first)['draft']['num_channels'],7)
                with service.condition:
                    next_id = str(uuid.uuid4())
                    service.batch.action(dict(action='start',previous=key,id=next_id,files=[first,last]))
                    service.batch.action(dict(action='cancel',id=next_id))
                    self.assertEqual(service.batch.record['items'][0]['settings']['num_channels'],7)
                    self.assertEqual(service.batch.record['items'][1]['status'],'skipped')
                    with self.assertRaisesRegex(ValueError,'批次已改变'):
                        service.batch.action(dict(action='resume',id=key))
            finally:
                service.close()

    def test_two_tracks_crossing_same_channel_in_one_frame(self):
        tracks = {1:[(10,30),(10,16)], 2:[(11,29),(11,14)]}
        counts, cooldown, events = {1:0}, {}, []
        _, _, channels = count_crossings(tracks, counts, [(0,20)], 20, cooldown, events=events)
        self.assertEqual(channels, {1})
        self.assertEqual(counts[1], 2)
        self.assertEqual([event['track'] for event in events], [1,2])
        count_crossings(tracks, counts, [(0,20)], 20, cooldown, events=events)
        self.assertEqual(len(events), 2)

    def test_short_clip_crossing_during_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root/'early.avi'
            crossing_video(video, frames=18, up_start=0)
            service = ChannelCounts(root)
            state = dict(frame=0, events=[], file='test', settings={'num_channels': 4})
            try:
                service.process(video, root, dict(state=state, paused=False))
                self.assertEqual(state['status'], 'complete')
                self.assertEqual(state['counts'], [0, 0, 0, 1])
                event, = state['events']
                self.assertLessEqual(abs(event['frame'] - 14), 1)
                prior = event['frame'] - 3
                photo = event_images(video, dict(event, prev_frame=prior), 18)
                self.assertEqual([f['frame'] for f in photo['frames']],
                                 [prior, event['frame'], event['frame'] + 1])
            finally:
                service.close()

    def test_checkpoint_resume_direction_history_and_excel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root/'test.avi'
            crossing_video(video)
            file = register(root,video)['id']
            select(root,file)
            service = ChannelCounts(root)
            cfg = {'num_channels':4}
            first = service.action(file,'start',{'settings':cfg})['task']['id']
            try:
                for _ in range(200):
                    if service.state(file)['task']['counts'][-1]: break
                    time.sleep(.01)
                paused = service.action(file,'pause',{'run':first})['task']
                self.assertEqual(paused['status'],'paused')
                saved_event, = paused['events']
            finally:
                service.close()
            other = ChannelCounts(root)
            try:
                self.assertEqual(other.state(file)['task']['status'],'interrupted')
                other.action(file,'resume',{'run':first})
                for _ in range(800):
                    if other.state(file)['task']['status']=='complete': break
                    time.sleep(.01)
                task = other.state(file)['task']
                self.assertEqual(task['id'],first)
                self.assertEqual(task['frame'],130)
                self.assertEqual(task['counts'],[0,0,0,1])
                event, = task['events']
                self.assertEqual(event, saved_event)
                self.assertEqual((event['id'], event['channel']), (1, 4))
                self.assertAlmostEqual(event['time_s'], (event['frame']-1)/12)
                start = event['frame'] - 2
                preview = other.frames(file, first, start, 4)['frames']
                self.assertEqual([item['frame'] for item in preview], list(range(start,start+4)))
                self.assertEqual([sum(item['counts']) for item in preview], [0,0,1,1])
                for item in preview:
                    self.assertEqual(item['image'], other.setup(file, 4, item['frame']-1, first)['image'])
                following = other.frames(file,first,start+4,2)['frames']
                self.assertEqual([item['frame'] for item in following], [start+4,start+5])
                self.assertEqual(other.frames(file,first,start,4)['frames'],preview)
                self.assertEqual([f['frame'] for f in other.frames(file,first,129,8)['frames']], [129,130])
                photo = other.event(file, first, event['id'])
                self.assertEqual([f['frame'] for f in photo['frames']],
                                 [event['frame']-1, event['frame'], event['frame']+1])
                self.assertEqual(len({f['image'] for f in photo['frames']}), 3)
                ending = event_images(video, dict(event, frame=130), 130)
                self.assertEqual([f['frame'] for f in ending['frames']], [129,130])
                book = load_workbook(io.BytesIO(other.export(file,first)),read_only=True)
                sheet = book.active
                self.assertEqual(sheet['B2'].value,1)
                self.assertEqual(sheet['B7'].value,1)
                self.assertEqual(book['越线明细'].max_row, 2)
                self.assertEqual([c.value for c in book['越线明细'][2]][:3], [1, 4, event['frame']])
                book.close()
                for channel, total in [(4, 1), (2, 0)]:
                    scoped = load_workbook(io.BytesIO(other.export(file, first, channel)), read_only=True)
                    self.assertEqual(scoped.active['B2'].value, total)
                    self.assertEqual(scoped.active.max_row, 4)
                    self.assertEqual(scoped['越线明细'].max_row, total + 1)
                    scoped.close()
                self.assertEqual(other.comparison()[0]['counts'], [0,0,0,1])
                next_run = other.action(file,'start',{'settings':cfg})['task']['id']
                self.assertEqual(other.state(file,first)['active_run'], next_run)
                self.assertEqual(len(other.state(file)['history']),2)
                self.assertEqual(other.state(file,first)['task']['counts'],[0,0,0,1])
                self.assertEqual(listing(root)['selected'],file)
                stamp = video.stat()
                os.utime(video, ns=(stamp.st_atime_ns, stamp.st_mtime_ns+1_000_000_000))
                with self.assertRaisesRegex(ValueError, '原视频已改变'):
                    other.event(file,first,1)
                with self.assertRaisesRegex(ValueError, '原视频已改变'):
                    other.frames(file,first,1,4)
                path = other.folder(file)/first/'state.json'
                old = read(path)
                del old['events']
                save(path,old)
                self.assertNotIn('events', other.state(file,first)['task'])
                legacy = load_workbook(io.BytesIO(other.export(file,first)),read_only=True)
                self.assertEqual(legacy.sheetnames, ['通道计数'])
                legacy.close()
            finally:
                other.close()
