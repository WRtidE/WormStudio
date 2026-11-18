import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from tools.release import windows_gpu as gpu
from tools.release import package_windows_kits as kits


class OfflineGpuTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stage = self.root / 'staging'
        self.stage.mkdir()
        self.wheel = self.root / 'test.whl'
        with ZipFile(self.wheel, 'w') as archive:
            archive.writestr('torch/example.py', b'verified')
        self.digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()

    def prepare(self, callback=lambda *args: None):
        with patch.dict(gpu.WHEELS, {self.wheel.name: self.digest}, clear=True):
            gpu.prepare(self.root, self.stage, callback)

    def test_downloaded_parts_restore_and_verify_the_original_wheels(self):
        vision = self.root / 'vision.whl'
        with ZipFile(vision, 'w') as archive:
            archive.writestr('vision/example.py', b'vision')
        wheels = {self.wheel.name: self.digest, vision.name: hashlib.sha256(vision.read_bytes()).hexdigest()}
        output = self.root / 'downloads'
        with patch.dict(gpu.WHEELS, wheels, clear=True), patch.object(kits, 'PART_BYTES', 60):
            kits.pack_gpu_parts(output, self.root)
            with gpu.wheel_directory(output, self.root, lambda *_: None) as restored:
                gpu.prepare(restored, self.stage, lambda *_: None)
            self.assertEqual((self.stage / 'torch/example.py').read_bytes(), b'verified')
            self.assertEqual((self.stage / 'vision/example.py').read_bytes(), b'vision')
            self.assertFalse(restored.exists())
            second = output / gpu.GPU_PARTS[1]
            second.unlink()
            with self.assertRaisesRegex(ValueError, 'part2'):
                with gpu.wheel_directory(output, self.root, lambda *_: None):
                    self.fail('Accepted an incomplete download')
            second.write_bytes(b'broken')
            with self.assertRaisesRegex(ValueError, '损坏'):
                with gpu.wheel_directory(output, self.root, lambda *_: None):
                    self.fail('Accepted a corrupt download')

    def test_unpacks_verified_bytes_and_reports_both_stages(self):
        events = []
        self.prepare(lambda *args: events.append(args))
        self.assertEqual((self.stage / 'torch/example.py').read_bytes(), b'verified')
        self.assertEqual([event[0] for event in events], ['正在校验离线组件', '正在解压加速组件'])
        self.assertTrue(all(done == total for _, done, total in events))

    def test_bad_digest_is_rejected_before_unpacking(self):
        self.wheel.write_bytes(b'partial download')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            self.prepare()
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_missing_wheel_is_reported_without_network(self):
        self.wheel.unlink()
        with self.assertRaisesRegex(ValueError, 'wheels'):
            self.prepare()

    def test_cancelled_verification_leaves_no_unpacked_files(self):
        def cancel(*_):
            raise InterruptedError()
        with self.assertRaises(InterruptedError):
            self.prepare(cancel)
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_detect_checks_actual_compute_before_recommending_a_download(self):
        python = self.root / 'python/python.exe'
        before = set(self.root.rglob('*'))
        with (patch.object(gpu, 'check_driver'),
              patch.object(gpu, 'runtime_status', return_value=('ready', 'Test NVIDIA'))):
            state, message = gpu.detect(python)
            self.assertEqual(state, 'ready')
            self.assertIn('Test NVIDIA', message)
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_unavailable_driver_does_not_offer_install_or_start_torch(self):
        with (patch.object(gpu, 'check_driver', side_effect=RuntimeError()),
              patch.object(gpu, 'runtime_status') as probe):
            self.assertEqual(gpu.detect(self.root / 'python.exe')[0], 'cpu')
            probe.assert_not_called()

    def test_only_compatible_cpu_installation_offers_optional_components(self):
        python = self.root / 'python/python.exe'
        with (patch.object(gpu, 'check_driver'),
              patch.object(gpu, 'runtime_status', return_value=('cpu', '')) as probe):
            self.assertEqual(gpu.detect(python)[0], 'incompatible')
            for package in gpu.BASE_PACKAGES:
                (python.parent / 'Lib/site-packages' / (package + '+cpu.dist-info')).mkdir(parents=True)
            self.assertEqual(gpu.detect(python)[0], 'available')
            (self.root / gpu.OVERLAY).mkdir()
            self.assertEqual(gpu.detect(python)[0], 'prepared')
            probe.return_value = ('failed', '')
            self.assertEqual(gpu.detect(python)[0], 'retry')

    @unittest.skipUnless(sys.platform == 'win32' and sys.version_info[:2] == (3, 11), 'Windows desktop')
    def test_active_session_delays_activation_and_retry_reuses_prepared_files(self):
        payload = self.root / 'resources/analysis'
        (self.root / 'WormStudio.exe').touch()
        (payload / 'code/docs').mkdir(parents=True)
        (payload / 'code/docs/models.json').touch()
        python = payload / 'python/python.exe'
        site = python.parent / 'Lib/site-packages'
        for name in gpu.BASE_PACKAGES:
            (site / (name + '+cpu.dist-info')).mkdir(parents=True)
        hook = site / 'zz-wormstudio-gpu.pth'
        with (patch.dict(gpu.WHEELS, {self.wheel.name: self.digest}, clear=True),
              patch.object(gpu, 'check_driver'),
              patch.object(gpu, 'gpu_name', side_effect=['', 'GPU', '', 'GPU', '', '', 'GPU', 'GPU']),
              patch.object(gpu, 'ensure_studio_closed', side_effect=[RuntimeError('请退出'), None, None])):
            with self.assertRaisesRegex(RuntimeError, '请退出'):
                gpu.install(python, self.root, lambda *_: None)
            self.assertFalse(hook.exists())
            self.wheel.unlink()
            with self.assertRaisesRegex(RuntimeError, '复查未通过'):
                gpu.install(python, self.root, lambda *_: None)
            self.assertFalse(hook.exists())
            self.assertIn('已启用', gpu.install(python, self.root, lambda *_: None))
            self.assertIn(gpu.OVERLAY, hook.read_text(encoding='ascii'))
