"""The Windows launcher must keep the project environment and explicit overrides."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv


@unittest.skipUnless(os.name == 'nt', 'Windows batch launcher')
class StartupEnvironmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve() / '项目 with spaces'
        cls.root.mkdir()
        (cls.root / 'config').mkdir()
        shutil.copy2(Path(__file__).resolve().parents[1] / 'start.bat', cls.root)
        venv.EnvBuilder(with_pip=False).create(cls.root / '.venv')
        cls.python = cls.root / '.venv/Scripts/python.exe'
        entry = 'import json, sys; print(json.dumps([sys.executable, sys.argv[1:]]))\n'
        for name in ('studio.py', 'worm_pipeline.py'):
            (cls.root / name).write_text(entry, encoding='utf-8')

    def setUp(self):
        self.env = dict(os.environ, CI='1', VIRTUAL_ENV=sys.prefix, CONDA_PREFIX=sys.base_prefix)
        self.env.pop('WORM_STUDIO_PYTHON', None)
        self.setting = self.root / 'config/python_path.txt'
        self.setting.unlink(missing_ok=True)

    def launch(self, *args):
        result = subprocess.run([os.environ['ComSpec'], '/d', '/c', 'start.bat', *args],
                                cwd=self.root, env=self.env, capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        executable, arguments = json.loads(result.stdout.strip())
        return Path(executable).resolve(), arguments

    def test_project_environment_wins_over_active_environments(self):
        self.assertEqual(self.launch(), (self.python, []))
        self.assertEqual(self.launch('--help'), (self.python, ['--help']))

    def test_local_file_supports_utf8_bom_and_blank_fallback(self):
        self.setting.write_text(str(sys.executable), encoding='utf-8-sig')
        self.assertEqual(self.launch()[0], Path(sys.executable).resolve())
        self.setting.write_text(' \n', encoding='utf-8-sig')
        self.assertEqual(self.launch()[0], self.python)

    def test_environment_override_wins_over_local_file(self):
        self.setting.write_text(str(self.python), encoding='utf-8')
        self.env['WORM_STUDIO_PYTHON'] = sys.executable
        self.assertEqual(self.launch()[0], Path(sys.executable).resolve())
