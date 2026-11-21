"""Online components must be reproducible, relocatable and retain legal notices."""
import io
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from zipfile import ZipFile

from tools.release.build_online import build, pack_parts, runtime_file, tree_entries


class OnlinePackageTests(unittest.TestCase):
    def test_components_reuse_content_hash_despite_rebuild_and_source_mtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entries = [('python/a.txt', 4, lambda: io.BytesIO(b'data'), 0o644)]
            for folder in ('first', 'second'):
                (root/folder).mkdir()
            first = pack_parts(entries,root/'first','common-win32-x64','https://example.org/v1')
            second = pack_parts(entries,root/'second','common-win32-x64','https://example.org/v2')
            self.assertEqual(first[0]['sha256'],second[0]['sha256'])
            with tarfile.open(next((root/'first').iterdir())) as archive:
                self.assertEqual(archive.extractfile('python/a.txt').read(), b'data')
                self.assertEqual(archive.getmember('python/a.txt').mtime,0)

    def test_runtime_excludes_compile_material_and_old_overlay_but_keeps_licenses(self):
        for name in ('torch/lib/cublas.dll','torch/_C.pyd','torch/include/LICENSE',
                     'torch-2.7.0.dist-info/licenses/LICENSE','python/Lib/site-packages/distutils-precedence.pth'):
            self.assertTrue(runtime_file(name), name)
        for name in ('torch/include/torch.h','torch/lib/dnnl.lib','python/Lib/site-packages/zz-wormstudio-gpu.pth',
                     'torch/__pycache__/module.pyc','torch-2.7.0.dist-info/direct_url.json'):
            self.assertFalse(runtime_file(name), name)

    def test_runtime_does_not_ship_links_to_external_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'runtime/python').mkdir(parents=True)
            (root/'outside.txt').write_text('private')
            try:
                (root/'runtime/python/escape').symlink_to(root/'outside.txt')
            except OSError:
                self.skipTest('Creating symlinks requires Windows developer privileges')
            with self.assertRaisesRegex(ValueError,'External runtime link'):
                list(tree_entries(root/'runtime','python'))

    def test_complete_build_separates_runtime_and_verifies_published_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, desktop, output = root/'source', root/'desktop', root/'output'
            files = {
                'python/python.exe': b'portable python',
                'python/Lib/site-packages/numpy/data': b'common dependency',
                'python/Lib/site-packages/torch-2.7.0+cpu.dist-info/METADATA': b'cpu metadata',
                'python/Lib/site-packages/torch/lib/torch_cpu.dll': b'cpu library',
                'python/Lib/site-packages/torch/include/skip.h': b'compile only',
                'sam2/sam2/__init__.py': b'sam2',
                'fonts/font.ttf': b'font',
                'models/model.pt': b'weights',
            }
            for name, content in files.items():
                file = desktop/'resources/analysis'/name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(content)
            (desktop/'WormStudio.exe').write_bytes(b'electron')
            sources = {
                'src/example.py': 'print("app")',
                'tools/electron/package.json': '{"version":"1.1.0"}',
                'tools/release/desktop.cjs': '// desktop entry',
                'docs/models.json': json.dumps({'sam2': {'filename': 'model.pt', 'sha256': hashlib.sha256(b'weights').hexdigest()}}),
            }
            for name, content in sources.items():
                file = source/name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(content, encoding='utf-8')
            records = {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in sources}
            (source/'SOURCE_MANIFEST.json').write_text(json.dumps({'files': records}))
            manifest = build(source, desktop, output, 'https://example.org/v1')
            members = {}
            for kind, assets in [('common', manifest['common']), ('cpu', manifest['backends']['cpu'])]:
                with tarfile.open(output/'components'/assets[0]['url'].rsplit('/', 1)[1]) as archive:
                    members[kind] = {member.name for member in archive.getmembers()}
                    self.assertEqual(assets[0]['unpackedSize'], sum(m.size for m in archive.getmembers()))
                    for member in archive.getmembers():
                        self.assertEqual(archive.extractfile(member).read(), files[member.name])
            self.assertFalse(members['common'] & members['cpu'])
            self.assertIn('python/Lib/site-packages/torch/lib/torch_cpu.dll', members['cpu'])
            self.assertIn('python/Lib/site-packages/numpy/data', members['common'])
            self.assertEqual(members['common'] | members['cpu'], {name for name in files if not name.startswith('models/') and runtime_file(name)})
            for line in (output/'SHA256SUMS.txt').read_text().splitlines():
                sha, name = line.split('  ')
                file = output/name if name.endswith('.zip') else output/'components'/name
                self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), sha)
            with ZipFile(output/'WormStudio-Windows-x64.zip') as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn('WormStudio/resources/analysis/code/src/example.py', archive.namelist())
                self.assertFalse(any('/analysis/python/' in name for name in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
