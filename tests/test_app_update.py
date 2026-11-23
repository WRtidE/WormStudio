"""Exercise Windows update staging and rollback without touching an installed app."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'src/platforms/app_update.ps1'


@unittest.skipUnless(sys.platform == 'win32', 'Windows update helper')
class AppUpdateTests(unittest.TestCase):
    def run_helper(self, root, entries, prepare=True):
        archive = root / 'update.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            for name, value in entries.items():
                package.writestr(name, value)
        config = dict(archive=str(archive), version='9.0.0', target=str(root/'App'),
                      stage=str(root/'.WormStudio-update-abc'), backup=str(root/'App.previous-abc'),
                      parentPid=2147483647, log=str(root/'result.log'))
        plan = root/'plan.json'
        plan.write_text(json.dumps(config), encoding='utf-8')
        return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                               'Bypass', '-File', str(SCRIPT), '-Plan', str(plan),
                               *(['-Prepare'] if prepare else [])], capture_output=True, timeout=30)

    def test_stage_validates_version_and_rejects_traversal(self):
        entries = {'WormStudio/WormStudio.exe': 'test',
                   'WormStudio/resources/app/desktop.cjs': 'test',
                   'WormStudio/resources/app/package.json': json.dumps(dict(name='WormStudio', version='9.0.0'))}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.run_helper(root, entries)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root/'.WormStudio-update-abc/WormStudio.exe').exists())
        for name in ('WormStudio/../../escape', 'Other/app.exe'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                result = self.run_helper(root, {name:'bad'})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root/'.WormStudio-update-abc').exists())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entries['WormStudio/resources/app/package.json'] = json.dumps(dict(name='WormStudio', version='8.0.0'))
            self.assertNotEqual(self.run_helper(root, entries).returncode, 0)

    def test_failed_launch_restores_previous_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'App').mkdir()
            (root/'App/old.txt').write_text('preserve')
            (root/'.WormStudio-update-abc').mkdir()
            result = self.run_helper(root, {}, prepare=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root/'App/old.txt').read_text(), 'preserve')

    def test_successful_swap_starts_new_executable_and_keeps_backup(self):
        import time
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'App').mkdir()
            (root/'App/old.txt').write_text('preserve')
            stage = root/'.WormStudio-update-abc'
            stage.mkdir()
            code = 'public class App { public static void Main() { System.IO.File.WriteAllText(System.IO.Path.Combine(System.AppDomain.CurrentDomain.BaseDirectory, "started.txt"), "new"); } }'
            script = root/'compile.ps1'
            script.write_text('param($Output)\nAdd-Type -TypeDefinition \''+code+"' -OutputAssembly $Output -OutputType WindowsApplication\n")
            subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                            str(script), str(stage/'WormStudio.exe')], check=True, capture_output=True, timeout=30)
            result = self.run_helper(root, {}, prepare=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            marker = root/'App/started.txt'
            deadline = time.monotonic()+5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(marker.read_text(), 'new')
            self.assertEqual((root/'App.previous-abc/old.txt').read_text(), 'preserve')


if __name__ == '__main__':
    unittest.main()
