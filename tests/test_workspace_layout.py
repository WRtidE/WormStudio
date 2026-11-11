import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
import manual_worm_annotation as manual
from pipeline_progress import PipelineProgress
import workspace_layout as layout

class WorkspaceLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def test_partial_updates_preserve_other_pages_and_reset_is_durable(self):
        self.assertFalse(layout.read(self.root)['exists'])
        layout.update(self.root,{'panels':{'studio:library':{'width':300},'studio:box0':{'x':24}}})
        layout.update(self.root,{'panels':{'annotation:工具':{'height':230,'collapsed':False}}})
        self.assertEqual(layout.read(self.root)['panels']['studio:library']['width'],300)
        layout.update(self.root,{'reset':True})
        self.assertEqual(layout.read(self.root),dict(version=1,exists=True,panels={}))
    def test_invalid_geometry_and_keys_do_not_overwrite_saved_layout(self):
        layout.update(self.root,{'panels':{'studio:library':{'width':300}}})
        before=(self.root/'config/layout.local.json').read_bytes()
        for value in [-1,float('nan'),float('inf'),True,'300',100001]:
            with self.assertRaises(ValueError):layout.update(self.root,{'panels':{'studio:library':{'width':value}}})
        for key in ['../../file','studio:a/b','studio:a\\b','studio:a\x00b','other:lib']:
            with self.assertRaises(ValueError):layout.update(self.root,{'panels':{key:{}}})
        with self.assertRaises(ValueError):layout.update(self.root,{'panels':{'studio:library':{'collapsed':1}}})
        self.assertEqual((self.root/'config/layout.local.json').read_bytes(),before)
    def test_corrupt_record_requires_explicit_reset(self):
        folder=self.root/'config';folder.mkdir();(folder/'layout.local.json').write_text('broken')
        with self.assertRaises(ValueError):layout.update(self.root,{'panels':{}})
        layout.update(self.root,{'reset':True});self.assertEqual(layout.read(self.root)['panels'],{})
    def test_failed_atomic_replace_retains_previous_record_and_cleans_temp(self):
        layout.update(self.root,{'panels':{'studio:library':{'width':300}}})
        with patch.object(Path,'replace',side_effect=OSError('disk')):
            with self.assertRaises(OSError):layout.update(self.root,{'panels':{'studio:library':{'width':400}}})
        self.assertEqual(layout.read(self.root)['panels']['studio:library']['width'],300)
        self.assertFalse(list((self.root/'config').glob('*.tmp')))
    def test_http_persists_across_server_restart_and_requires_current_token(self):
        def get(p):
            return json.load(urllib.request.urlopen(p.url.removesuffix('/progress')+'/api/layout'))
        def post(p,token,body):
            return urllib.request.urlopen(urllib.request.Request(p.url.removesuffix('/progress')+'/api/layout',data=json.dumps(body).encode(),headers={'X-Annotation-Token':token,'Content-Type':'application/json'}))
        with patch.object(manual,'ROOT',self.root):
            p=PipelineProgress();p.bind_server(manual.create_server(progress=p))
            first_port=p.server.server_port;old_token=p.token
            try:
                self.assertFalse(get(p)['exists'])
                with self.assertRaises(urllib.error.HTTPError) as caught:post(p,'wrong',{'reset':True})
                self.assertEqual(caught.exception.code,403)
                post(p,p.token,{'panels':{'studio:library':{'width':300}}}).close()
            finally:p.close()
            p=PipelineProgress();p.bind_server(manual.create_server(progress=p))
            try:
                self.assertNotEqual(p.server.server_port,first_port)
                self.assertEqual(get(p)['panels']['studio:library']['width'],300)
                with self.assertRaises(urllib.error.HTTPError) as caught:post(p,old_token,{'reset':True})
                self.assertEqual(caught.exception.code,403)
                with self.assertRaises(urllib.error.HTTPError):post(p,p.token,{'panels':{'studio:library':{'width':-2}}})
                self.assertEqual(get(p)['panels']['studio:library']['width'],300)
            finally:p.close()

if __name__=='__main__':unittest.main()
