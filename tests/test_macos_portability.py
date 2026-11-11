"""Host-independent routing tests; these do not claim Metal kernel compatibility."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from compute_device import selected_device, inference_context, device_label


def tool(name):
    path = Path(__file__).resolve().parents[1]/'tools'/f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MacPortabilityTests(unittest.TestCase):
    def test_unavailable_metal_has_actionable_error(self):
        with patch.dict(os.environ, WORMSTUDIO_DEVICE='mps'), patch('torch.backends.mps.is_available', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'CPU'):
                selected_device()

    def test_metal_avoids_cuda_query_and_autocast(self):
        with patch.dict(os.environ, WORMSTUDIO_DEVICE='mps'), patch('torch.backends.mps.is_available', return_value=True), patch('torch.cuda.is_available', side_effect=AssertionError), patch('torch.autocast', side_effect=AssertionError):
            self.assertEqual(selected_device(), 'mps')
            with inference_context('mps'):
                self.assertIn('Metal', device_label('mps'))

    def test_auto_does_not_enable_unvalidated_metal(self):
        with patch.dict(os.environ, WORMSTUDIO_DEVICE='auto'), patch('torch.cuda.is_available', return_value=False), patch('torch.backends.mps.is_available', return_value=True):
            self.assertEqual(selected_device(), 'cpu')

    def test_native_mac_runtime_layout(self):
        root = Path('bundle with spaces')
        python, search, unpack = tool('portable_launcher').runtime_paths(root, 'darwin')
        self.assertEqual(python, root/'runtime/bin/python')
        self.assertEqual(unpack, root/'runtime/bin/conda-unpack')
        self.assertEqual(search, [root/'runtime/bin'])

    def test_windows_layout_retained(self):
        python, search, unpack = tool('portable_launcher').runtime_paths(Path('bundle'), 'win32')
        self.assertEqual(python.name, 'python.exe')
        self.assertEqual(len(search), 3)
        self.assertEqual(unpack.name, 'conda-unpack-script.py')

    def test_build_refuses_foreign_platform_and_rosetta(self):
        builder = tool('build_macos')
        for system, arch in [('Windows', 'AMD64'), ('Darwin', 'x86_64'), ('Linux', 'aarch64')]:
            with self.assertRaises(RuntimeError):
                builder.require_native_mac(system, arch)
        builder.require_native_mac('Darwin', 'arm64')

    def test_command_is_quoted_and_uses_lf(self):
        text = tool('build_macos').command_text('cpu')
        self.assertNotIn('\r', text)
        self.assertIn('"./runtime/bin/python"', text)
        self.assertIn('"$(dirname -- "$0")"', text)
        with self.assertRaises(ValueError):
            tool('build_macos').command_text('invalid')
