"""A fresh checkout must not depend on developer-only settings or sibling files."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import resource_paths as paths
import tapnext_support


class PortableResources(unittest.TestCase):
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
            root=Path(folder);(root/'config').mkdir()
            setting=root/'config/python_path.txt';setting.write_text('  \n',encoding='utf-8')
            self.assertEqual(paths.runtime_python(root),Path(sys.executable).resolve())
            setting.write_text(str(root/'local-python'),encoding='utf-8')
            self.assertEqual(paths.runtime_python(root),root/'local-python')
            os.environ['WORM_STUDIO_PYTHON']=str(root/'override-python')
            self.assertEqual(paths.runtime_python(root),root/'override-python')

    def test_resources_and_checkpoint_prefer_portable_layout(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root=Path(folder)/'app';root.mkdir()
            self.assertEqual(paths.resource_root(root),root/'resources')
            (root.parent/'TWARDISv0.1').mkdir()
            self.assertEqual(paths.resource_root(root),root.parent)
            (root/'resources').mkdir()
            self.assertEqual(paths.resource_root(root),root/'resources')
            (root/'models').mkdir();checkpoint=root/'models/sam2_hiera_large.pt';checkpoint.touch()
            with patch.object(paths,'PROJECT_ROOT',root),patch.object(paths,'RESOURCE_ROOT',root/'resources'):
                self.assertEqual(paths.sam2_checkpoint(checkpoint.name),checkpoint)

    def test_bundled_single_worm_package(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root=Path(folder);package=root/'packages/single-worm-methods'
            (package/'wormstudio_single').mkdir(parents=True)
            (package/'wormstudio_single/fusion_v4.py').touch()
            (root/'models').mkdir();checkpoint=root/'models/tapnextpp_ckpt.pt';checkpoint.touch()
            with patch.object(tapnext_support,'ROOT',root):
                self.assertEqual(tapnext_support.resources(),(package,checkpoint))


if __name__=='__main__':unittest.main()
