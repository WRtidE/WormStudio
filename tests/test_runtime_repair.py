import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('repair_runtime', Path(__file__).resolve().parents[1]/'tools/repair_windows_runtime.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RuntimeRepairTests(unittest.TestCase):
    def test_removes_only_retired_missing_entries_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'Scripts').mkdir()
            (root/'keep.txt').write_text('required')
            records = [(name, 'old', 'text') for name in sorted(module.RETIRED)]+[('keep.txt', 'old', 'text')]
            script = root/'Scripts/conda-unpack-script.py'
            script.write_text('_prefix_records = '+repr(records)+'\n', encoding='utf-8')
            self.assertEqual(set(module.repair(root)), module.RETIRED)
            namespace = {}
            exec(script.read_text(encoding='utf-8'), namespace)
            self.assertEqual(namespace['_prefix_records'], [('keep.txt','old','text')])
            self.assertEqual(module.repair(root), [])

    def test_does_not_hide_unexpected_missing_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'Scripts').mkdir()
            script = root/'Scripts/conda-unpack-script.py'
            original = "_prefix_records = [('Lib/required.py','old','text')]\n"
            script.write_text(original, encoding='utf-8')
            with self.assertRaises(FileNotFoundError):
                module.repair(root)
            self.assertEqual(script.read_text(encoding='utf-8'), original)
