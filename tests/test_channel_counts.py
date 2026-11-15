import io
import os
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np
from openpyxl import load_workbook

from channel_counts import ChannelCounts, event_images, read, save
from wormtracker.core.counter import count_crossings
from workspace_library import register, select, listing


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
