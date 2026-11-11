import os, unittest
from unittest.mock import patch
from compute_device import selected_device, inference_context
class DeviceTests(unittest.TestCase):
    def test_cpu_never_queries_cuda(self):
        with patch.dict(os.environ,WORMSTUDIO_DEVICE='cpu'), patch('torch.cuda.is_available',side_effect=AssertionError):
            self.assertEqual(selected_device(),'cpu')
            with inference_context('cpu'):pass
    def test_auto_falls_back(self):
        with patch.dict(os.environ,WORMSTUDIO_DEVICE='auto'), patch('torch.cuda.is_available',return_value=False):
            self.assertEqual(selected_device(),'cpu')
    def test_auto_selects_cuda(self):
        with patch.dict(os.environ,WORMSTUDIO_DEVICE='auto'), patch('torch.cuda.is_available',return_value=True):
            self.assertEqual(selected_device(),'cuda')
    def test_explicit_gpu_error(self):
        with patch.dict(os.environ,WORMSTUDIO_DEVICE='cuda'), patch('torch.cuda.is_available',return_value=False):
            with self.assertRaises(RuntimeError):selected_device()
