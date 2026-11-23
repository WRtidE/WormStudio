import os, unittest
from unittest.mock import patch
from compute_device import selected_device, inference_context
class DeviceTests(unittest.TestCase):
    def test_cpu_never_queries_cuda(self):
        with patch.dict(os.environ,WORMSTUDIO_DEVICE='cpu'), patch('torch.cuda.is_available',side_effect=AssertionError):
            self.assertEqual(selected_device(),'cpu')
            with inference_context('cpu'):pass
    def test_auto_falls_back(self):
        with patch.dict(os.environ,WORMSTUDIO_DEVICE='auto'), patch('torch.cuda.is_available',return_value=False), patch('torch.backends.mps.is_available',return_value=False):
            self.assertEqual(selected_device(),'cpu')
    def test_auto_selects_cuda(self):
        with patch.dict(os.environ,WORMSTUDIO_DEVICE='auto'), patch('torch.cuda.is_available',return_value=True):
            self.assertEqual(selected_device(),'cuda')
    def test_explicit_gpu_error(self):
        with patch.dict(os.environ,WORMSTUDIO_DEVICE='cuda'), patch('torch.cuda.is_available',return_value=False):
            with self.assertRaises(RuntimeError):selected_device()


class CacheReleaseTests(unittest.TestCase):
    def test_cuda_releases_cuda_cache(self):
        from compute_device import release_device_cache
        with patch('torch.cuda.is_available', return_value=True), patch('torch.cuda.empty_cache') as cuda:
            release_device_cache('cuda')
        cuda.assert_called_once_with()

    def test_mps_releases_metal_cache(self):
        from compute_device import release_device_cache
        with patch('torch.backends.mps.is_available', return_value=True), patch('torch.mps.empty_cache') as mps:
            release_device_cache('mps')
        mps.assert_called_once_with()

    def test_cpu_does_not_touch_any_cache(self):
        from compute_device import release_device_cache
        with patch('torch.cuda.empty_cache') as cuda, patch('torch.mps.empty_cache') as mps:
            release_device_cache('cpu')
        cuda.assert_not_called(); mps.assert_not_called()

    def test_unavailable_backend_is_ignored(self):
        from compute_device import release_device_cache
        with patch('torch.cuda.is_available', return_value=False), patch('torch.cuda.empty_cache') as cuda:
            release_device_cache('cuda')
        cuda.assert_not_called()

    def test_auto_detection_prefers_available_backend(self):
        from compute_device import release_device_cache
        with patch('torch.cuda.is_available', return_value=False), \
                patch('torch.backends.mps.is_available', return_value=True), \
                patch('torch.mps.empty_cache') as mps:
            release_device_cache()
        mps.assert_called_once_with()
