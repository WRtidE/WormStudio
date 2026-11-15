import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from wormstudio_single.pipeline import read_frames


class TrackingWindowFrames(unittest.TestCase):
    def test_window_matches_full_decode_with_crop_and_reverse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'clip.avi'
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 12, (32,32))
            self.assertTrue(writer.isOpened())
            for i in range(7):
                writer.write(np.full((32,32,3), i*25, np.uint8))
            writer.release()
            cfg = dict(video=path, crop_xyxy=[5,8,30,31])
            roi = (3,2,15,14)
            full = read_frames(cfg, roi)
            for ids in ([3,4,5,6], [2,1,0], [6]):
                np.testing.assert_array_equal(read_frames(cfg,roi,ids,7), full[ids])
            for ids,count in (([0,2],7),([6,7],8),([2,3],4)):
                with self.assertRaises(ValueError):
                    read_frames(cfg,roi,ids,count)
