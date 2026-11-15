import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import weakref

from sam2_frames import init_tracking_state


@unittest.skipUnless(importlib.util.find_spec('sam2'), 'SAM2 not installed')
class TrackingFramesTests(unittest.TestCase):
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
            with patch('sam2_frames.sys.platform', 'darwin'):
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

        with patch('sam2_frames.sys.platform', 'darwin'):
            self.assertEqual(run(True), run(False))
