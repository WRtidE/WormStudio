"""A fresh checkout must not depend on developer-only settings or sibling files."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from runtime import resource_paths as paths


class PortableResources(unittest.TestCase):
    def test_project_root_is_above_src(self):
        self.assertEqual(paths.PROJECT_ROOT, Path(__file__).resolve().parents[1])

    def test_manual_constraints_stay_local(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'config').mkdir()
            self.assertEqual(paths.motion_constraints_path(root),root/'config/motion_constraints.json')
            local=root/'config/motion_constraints.local.json';local.write_text('{"constraints": []}')
            self.assertEqual(paths.motion_constraints_path(root),local)

    def test_active_python_without_local_configuration(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            self.assertEqual(paths.runtime_python(Path(folder)), Path(sys.executable).resolve())

    def test_override_precedence_and_blank_local_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root=Path(folder).resolve();(root/'config').mkdir()
            setting=root/'config/python_path.txt';setting.write_text('  \n',encoding='utf-8')
            self.assertEqual(paths.runtime_python(root),Path(sys.executable).resolve())
            setting.write_text(str(root/'local-python'),encoding='utf-8')
            self.assertEqual(paths.runtime_python(root),root/'local-python')
            os.environ['WORM_STUDIO_PYTHON']=str(root/'override-python')
            self.assertEqual(paths.runtime_python(root),root/'override-python')

    def test_resources_and_checkpoint_prefer_portable_layout(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root=Path(folder).resolve()/'app';root.mkdir()
            self.assertEqual(paths.resource_root(root),root/'resources')
            (root.parent/'TWARDISv0.1').mkdir()
            self.assertEqual(paths.resource_root(root),root.parent)
            (root/'resources').mkdir()
            self.assertEqual(paths.resource_root(root),root/'resources')
            (root/'models').mkdir();checkpoint=root/'models/sam2_hiera_large.pt';checkpoint.touch()
            with patch.object(paths,'PROJECT_ROOT',root),patch.object(paths,'RESOURCE_ROOT',root/'resources'):
                self.assertEqual(paths.sam2_checkpoint(checkpoint.name),checkpoint)

if __name__=='__main__':unittest.main()
