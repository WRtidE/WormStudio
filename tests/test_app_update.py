"""Exercise Windows update staging and rollback without touching an installed app."""
import hashlib
import json
from pathlib import Path
import shutil
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
                      stage=str(root/'.App.updates/stage-abc'), backup=str(root/'.App.updates/previous-abc'),
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
            self.assertTrue((root/'.App.updates/stage-abc/WormStudio.exe').exists())
        for name in ('WormStudio/../../escape', 'Other/app.exe'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                result = self.run_helper(root, {name:'bad'})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root/'.App.updates/stage-abc').exists())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entries['WormStudio/resources/app/package.json'] = json.dumps(dict(name='WormStudio', version='8.0.0'))
            self.assertNotEqual(self.run_helper(root, entries).returncode, 0)

    def test_failed_launch_restores_previous_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'App').mkdir()
            (root/'App/old.txt').write_text('preserve')
            (root/'.App.updates/stage-abc').mkdir(parents=True)
            result = self.run_helper(root, {}, prepare=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root/'App/old.txt').read_text(), 'preserve')
            self.assertFalse((root/'.App.updates/stage-abc').exists())

    @unittest.skipUnless(shutil.which('node'), 'Node required to test the desktop handoff')
    def test_desktop_handoff_survives_parent_exit_and_starts_new_executable(self):
        import time
        with tempfile.TemporaryDirectory(prefix='WormStudio 更新 ') as folder:
            root = Path(folder)
            (root/'App').mkdir()
            (root/'App/old.txt').write_text('preserve')
            code = 'public class App { public static void Main() { System.IO.File.WriteAllText(System.IO.Path.Combine(System.AppDomain.CurrentDomain.BaseDirectory, "started.txt"), "new"); } }'
            script = root/'compile.ps1'
            script.write_text('param($Output)\nAdd-Type -TypeDefinition \''+code+"' -OutputAssembly $Output -OutputType WindowsApplication\n")
            subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                            str(script), str(root/'fixture.exe')], check=True, capture_output=True, timeout=30)
            archive = root/'update.zip'
            with zipfile.ZipFile(archive, 'w') as package:
                package.write(root/'fixture.exe', 'WormStudio/WormStudio.exe')
                package.writestr('WormStudio/resources/app/desktop.cjs', '// test')
                package.writestr('WormStudio/resources/app/package.json', '{"name":"WormStudio","version":"9.0.0"}')
            # Exercise the same Node spawn options as Electron. A successful spawn
            # alone does not prove that Windows PowerShell ran the script.
            cache = root/'.App.updates'
            old_backup = cache/'previous-001'
            old_backup.mkdir(parents=True)
            (cache/'last-backup.txt').write_text(old_backup.name)
            (cache/'user-file').write_text('keep')
            record = dict(archive=str(archive), executable=str(root/'App/WormStudio.exe'),
                          digest='sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest(),
                          directory=str(root/'profile'), version='9.0.0')
            handoff = """const {prepare} = require(process.argv[1]);
                prepare(JSON.parse(process.argv[2])).then(update => update.start())
                    .catch(error => { console.error(error); process.exitCode = 1; });"""
            result = subprocess.run(['node', '-e', handoff, str(SCRIPT.with_suffix('.cjs')),
                                     json.dumps(record)], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            marker = root/'App/started.txt'
            deadline = time.monotonic()+15
            while (not marker.exists() or old_backup.exists()) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(marker.read_text(), 'new')
            self.assertFalse(old_backup.exists())
            self.assertEqual((cache/'user-file').read_text(), 'keep')
            backups = list((root/'.App.updates').glob('previous-*/old.txt'))
            self.assertEqual([file.read_text() for file in backups], ['preserve'])


if __name__ == '__main__':
    unittest.main()
