"""Exercise adapter segmentation with an instrumented solver, not GPU accuracy."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import cv2
import numpy as np
import analyze_phenotypes as metrics
import quality_stage
import repair_wormtracer as repair
from quality_fixture import make_fixture


class GuardedSolverTests(unittest.TestCase):
    def test_wormtracer_never_receives_contact_or_bridges_excluded_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config,source,_=make_fixture(root);seen=[]
            def fit(config_path,input_path,output_path):
                import yaml
                count=yaml.safe_load(Path(config_path).read_text())['plot_n']
                files=sorted(Path(input_path).glob('*.png'));seen.append(len(files));xs=[];ys=[]
                for file in files:
                    mask=cv2.imdecode(np.frombuffer(file.read_bytes(),np.uint8),0);y,x=np.nonzero(mask)
                    xs.append(np.linspace(x.min(),x.max(),count));ys.append(np.full(count,y.mean()))
                out=Path(output_path)/'run';out.mkdir(parents=True)
                np.savetxt(out/'worm_x.csv',xs,delimiter=',');np.savetxt(out/'worm_y.csv',ys,delimiter=',')
            fake=SimpleNamespace(wt=SimpleNamespace(run=fit))
            spec=importlib.util.spec_from_file_location('guarded_wormtracer_test',metrics.ROOT/'src/run_wormtracer.py')
            adapter=importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules,{'WormTracer':fake}):spec.loader.exec_module(adapter)
            # The instrumented solver does not need an external WormTracer checkout.
            settings=root/'WormTracer/WormTracer19_1/WormTracer/config.yaml'
            settings.parent.mkdir(parents=True);settings.write_text('plot_n: 101\n',encoding='utf-8')
            argv=['fit','--sample',config['sample'],'--worm','1','--crop-to-track','--overwrite','--output-name','wormtracer-tuned']
            with patch.object(metrics,'ROOT',root),patch.object(adapter,'ROOT',root),patch.object(adapter,'RESOURCE_ROOT',root),patch('sys.argv',argv),\
                 patch.dict('os.environ',{'WORM_PROGRESS_FILE':str(root/'progress.json')}),\
                 patch.object(adapter.torch.cuda,'is_available',return_value=True),\
                 patch.object(adapter.torch.cuda,'reset_peak_memory_stats'),\
                 patch.object(adapter.torch.cuda,'get_device_name',return_value='instrumented solver'),\
                 patch.object(adapter.torch.cuda,'max_memory_allocated',return_value=0),\
                 patch.object(adapter.torch.cuda,'empty_cache'):
                quality_stage.run(config['sample']);adapter.main()
                self.assertEqual(seen,[2,4,1])
                folder=root/'outputs'/config['sample']/'wormtracer-tuned'/'worm-1'
                raw=np.load(folder/'centerlines.npy')
                self.assertTrue(np.isnan(raw[[2,3,8]]).all());self.assertTrue(np.isfinite(raw[[0,1,4,5,6,7,9]]).all())
                refined=[]
                def refine(points,*args):refined.append(len(points));return points
                with patch.object(repair,'ROOT',root),patch.object(repair,'refine_on_gpu',side_effect=refine):
                    repair.run(config['sample'],'wormtracer-tuned','wormtracer-corrected',1)
                self.assertEqual(refined,[2,4,1])
                output=root/'outputs'/config['sample']/'wormtracer-corrected'/'worm-1'
                q=json.loads((output/'quality.json').read_text())
                self.assertEqual(q[2]['topology'],'mask_excluded')
                self.assertTrue(np.isnan(np.load(output/'centerlines.npy')[[2,3,8]]).all())


if __name__=='__main__':unittest.main()
