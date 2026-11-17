"""Source packages from a checkout must exclude local work, even inside web/."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import check_source, export_source


class SourceExportTests(unittest.TestCase):
    def test_checkout_excludes_untracked_source_and_detects_missing_tracked_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            (root / 'web').mkdir()
            public = root / 'web' / 'player.js'
            public.write_text('const frame = 0;', encoding='utf-8')
            subprocess.run(['git', 'add', '--', 'web/player.js'], cwd=root, check=True)
            (root / 'web' / 'local-notes.md').write_text('draft', encoding='utf-8')
            with patch.multiple(export_source, ROOT=root, TOP=(), DOCS=(), CONFIG=()):
                self.assertEqual(export_source.source_files(), [public])
                public.unlink()
                with self.assertRaisesRegex(ValueError, 'missing'):
                    export_source.source_files()

    def test_source_snapshot_can_be_exported_without_git(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'web').mkdir()
            public = root / 'web' / 'player.js'
            public.write_text('const frame = 0;', encoding='utf-8')
            with patch.multiple(export_source, ROOT=root, TOP=(), DOCS=(), CONFIG=()):
                self.assertEqual(export_source.source_files(), [public])

    def test_public_example_allowed_and_runtime_settings_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / '.env.example').write_text('MODE=test', encoding='utf-8')
            check_source.check(root)
            for name in ('config/runtime.local.cmd', 'config/workspace.local.json',
                         'tools/electron/local.json', '.env.production', 'user-data/state.json'):
                with self.subTest(name=name):
                    path = root / name
                    path.parent.mkdir(exist_ok=True, parents=True)
                    path.write_text('local', encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, 'private config|runtime data'):
                        check_source.check(root)
                    path.unlink()
