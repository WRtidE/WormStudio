import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import weakref

from sam2_frames import init_tracking_state, refine_mask_edges, sam2_input_transform


class BackgroundInputTests(unittest.TestCase):
    def test_edge_refinement_reduces_halo_without_erasing_tips_or_border(self):
        import cv2
        import numpy as np

        outline = np.zeros((61, 100), np.uint8)
        cv2.ellipse(outline, (44,30), (30,7), 0, 0, 360, 1, -1)
        outline[30, :20] = 1
        image = np.repeat(np.where(outline, 60, 180).astype(np.uint8)[:, :, None], 3, axis=2)
        mask = cv2.dilate(outline, np.ones((5,5), np.uint8)).astype(bool)
        refined = refine_mask_edges(image, mask)
        self.assertLess(np.count_nonzero(refined != outline), np.count_nonzero(mask != outline))
        self.assertFalse(np.any(refined & ~mask))
        np.testing.assert_array_equal(refined[:,0], mask[:,0])
        self.assertTrue(refined[30,:72].all())
        np.testing.assert_array_equal(refine_mask_edges(np.zeros_like(image), mask), mask)

    def test_moving_body_survives_stationary_chamber_and_nonzero_crop(self):
        import cv2
        import numpy as np
        from unittest.mock import MagicMock

        background = np.full((16, 27, 3), 160, np.uint8)
        background[:, 8:10] = 110
        frames = []
        for x in (5, 8, 11, 14, 17):
            image = background.copy()
            image[6:9, x:x+2] -= 40
            frames.append(image)
        capture = MagicMock()
        capture.get.return_value = len(frames)
        capture.read.side_effect = [(True, image) for image in frames]
        transform = sam2_input_transform('static_background', capture, [3, 4, 21, 12])
        original = frames[1][4:12, 3:21]
        model_input = transform(original)
        self.assertEqual(model_input.shape, original.shape)
        self.assertGreater(int(model_input[0, 5, 0]) - int(model_input[3, 5, 0]), 90)
        self.assertLess(int(model_input[0, 10, 0]) - int(model_input[0, 5, 0]), 25)
        self.assertEqual(original[3, 5, 0], 70)
        self.assertEqual(capture.get.call_args.args, (cv2.CAP_PROP_FRAME_COUNT,))


@unittest.skipUnless(importlib.util.find_spec('sam2'), 'SAM2 not installed')
class TrackingFramesTests(unittest.TestCase):
    def test_tracking_encoder_finishes_in_order_and_propagates_write_failure(self):
        from concurrent.futures import ThreadPoolExecutor
        from track_configured_video import ImageWrites, write_input_frame
        from image_io import write_png, read_image
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(1) as pool:
            target = Path(directory)
            writes = ImageWrites(pool)
            pixels = np.arange(20*24*3, dtype=np.uint8).reshape(20, 24, 3)
            path = target/'frame.jpg'
            writes.submit(write_input_frame, path, pixels)
            mask = target/'mask.png'
            writes.submit(write_png, mask, pixels[:, :, 0])
            writes.submit(write_png, mask, pixels[:, :, 1])
            writes.finish()
            self.assertEqual(path.read_bytes(), cv2.imencode('.jpg', pixels, [cv2.IMWRITE_JPEG_QUALITY, 94])[1].tobytes())
            np.testing.assert_array_equal(read_image(mask, gray=True), pixels[:, :, 1])
            writes.submit(write_input_frame, target/'missing'/'frame.jpg', pixels)
            with self.assertRaises(OSError):
                writes.finish()

    def test_random_access_matches_upstream_without_retaining_frames(self):
        from PIL import Image
        from sam2.utils.misc import load_video_frames
        import torch

        class Predictor:
            image_size = 32

            def init_state(self, directory, offload_video_to_cpu, offload_state_to_cpu):
                images, h, w = load_video_frames(directory, self.image_size, offload_video_to_cpu)
                return dict(images=images, video_height=h, video_width=w, warmup_count=len(images))

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for i in range(5):
                Image.new('RGB', (20, 12), (17*i, 47, 132)).save(directory / f'{i:06d}.jpg')
            expected, _, _ = load_video_frames(tmp, 32, True)
            with patch('sam2_frames.is_darwin', return_value=True):
                state = init_tracking_state(Predictor(), directory, 5)
            self.assertEqual((state['warmup_count'], state['num_frames']), (1, 5))
            self.assertEqual((state['video_height'], state['video_width']), (12, 20))
            for i in [3, 4, 2, 1, 0, 3]:
                frame = state['images'][i]
                self.assertTrue(torch.equal(frame, expected[i]))
                reference = weakref.ref(frame)
                del frame
                self.assertIsNone(reference())


class TrackingHistoryTests(unittest.TestCase):
    def test_bidirectional_history_matches_unpruned_lookups(self):
        from sam2_frames import propagate_frames

        class Model:
            num_maskmem = 4
            memory_temporal_stride_for_eval = 3
            max_obj_ptrs_in_encoder = 6

            def propagate_in_video(self, state, start_frame_idx, reverse):
                outputs = state['output_dict_per_obj'][0]
                history = outputs['non_cond_frame_outputs']
                step = -1 if reverse else 1
                for frame in range(start_frame_idx, -1 if reverse else 110, step):
                    value = outputs['cond_frame_outputs'].get(frame)
                    if value is None:
                        value = sum(history.get(frame - step * i, 0) for i in (1, 5, 8)) % 65521 + frame
                        history[frame] = value
                    yield frame, [0], value

        def run(bounded):
            outputs = dict(cond_frame_outputs={8: 10, 71: 100}, non_cond_frame_outputs={})
            state = dict(output_dict_per_obj={0: outputs})
            model = Model()
            results = []
            for reverse in (False, True):
                stream = (propagate_frames(model, state, 23, reverse) if bounded else
                          model.propagate_in_video(state, 23, reverse))
                for frame, _, value in stream:
                    results.append((frame, value))
                    if bounded:
                        self.assertLessEqual(len(outputs['non_cond_frame_outputs']), 40)
            return results

        with patch('sam2_frames.is_darwin', return_value=True):
            self.assertEqual(run(True), run(False))
