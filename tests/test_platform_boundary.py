"""Only src/platforms may test the host OS; everything else asks for a capability."""
import ast
import os
import re
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
PLATFORMS = SRC / 'platforms'
ENTRY = sorted(ROOT.glob('*.py'))
BRANCH = re.compile(r"sys\.platform|os\.name\s*(?:==|!=)|platform\.(?:system|machine|node)\(")


def python_files(root):
    return sorted(p for p in root.rglob('*.py') if '__pycache__' not in p.parts)


class PlatformBoundaryTests(unittest.TestCase):
    def test_platform_checks_live_only_in_platforms(self):
        offenders = []
        for path in python_files(SRC) + ENTRY:
            if path.is_relative_to(PLATFORMS):
                continue
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if BRANCH.search(line):
                    offenders.append(f'{path.relative_to(ROOT)}:{number}: {line.strip()}')
        self.assertEqual(offenders, [], '平台判断必须收进 src/platforms：\n' + '\n'.join(offenders))

    def test_platforms_package_does_not_import_application_modules(self):
        local = {p.stem for p in SRC.glob('*.py')}
        local.update(p.parent.name for p in SRC.glob('*/__init__.py') if p.parent != PLATFORMS)
        offenders = []
        for path in python_files(PLATFORMS):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split('.')[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split('.')[0]]
                for name in names:
                    if name in local:
                        offenders.append(f'{path.name}: {name}')
        self.assertEqual(offenders, [], 'platforms 不得依赖应用模块（避免反向依赖）：\n' + '\n'.join(offenders))


class ElectronEnvironmentTests(unittest.TestCase):
    def test_run_as_node_is_not_inherited(self):
        from platforms import shell
        with patch.dict(os.environ, {'ELECTRON_RUN_AS_NODE': '1'}):
            environment = shell.environment()
        self.assertNotIn('ELECTRON_RUN_AS_NODE', environment)
        self.assertEqual(environment['WORM_STUDIO_WORKSPACE'], str(shell.ROOT))


if __name__ == '__main__':
    unittest.main()
