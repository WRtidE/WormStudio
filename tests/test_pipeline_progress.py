"""Real subprocess events, lifecycle, and local progress API regression tests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

from manual_worm_annotation import create_server
from pipeline_progress import PipelineProgress
from worm_pipeline import run_stage


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='worm-progress-')
        self.base = Path(self.temp.name)
        self.progress = PipelineProgress()
        self.config = dict(sample='test-progress', video='input.mp4', centerline_method='niu')
        self.stages = [dict(name='tracking', phase='tracking'), dict(name='niu-worm-7', phase='fit')]
        self.progress.configure(self.config, dict(ids=[7], frames=12), self.stages, self.base, 'Test GPU')

    def tearDown(self):
        self.progress.close()
        self.temp.cleanup()

    def test_actual_child_events_and_logs_reach_saved_status(self):
        log = self.base/'child.log'
        self.progress.begin(0, log)
        command = [sys.executable, '-c', "from pipeline_progress import report_progress; print('real child output'); report_progress(4,12,'forward tracking',force=True)"]
        run_stage(dict(name='tracking', command=command), log, self.progress)
        state = self.progress.snapshot()
        self.assertEqual(state['stages'][0]['current'], 4)
        self.assertAlmostEqual(state['percent'], 16.7)
        self.assertIn('real child output', state['logs'])
        persisted = json.loads((self.base/'pipeline-progress.json').read_text(encoding='utf-8'))
        self.assertNotIn('token', persisted)
        self.progress.done(0)
        self.progress.done(1, reused=True)
        self.assertEqual(self.progress.snapshot()['completed_stages'], 2)
        self.assertEqual(self.progress.snapshot()['stages'][1]['status'], 'reused')

    def test_nonzero_exit_remains_failed_and_does_not_claim_completion(self):
        log = self.base/'failure.log'
        self.progress.begin(0, log)
        with self.assertRaises(RuntimeError) as error:
            run_stage(dict(name='tracking', command=[sys.executable,'-c',"print('expected test failure'); raise SystemExit(3)"]), log, self.progress)
        self.progress.fail(error.exception)
        state = self.progress.snapshot()
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['stages'][0]['status'], 'failed')
        self.assertEqual(state['completed_stages'], 0)
        self.assertIn('expected test failure', state['error'])
        self.assertEqual(state['artifacts'], [])

    def test_stale_or_incomplete_event_does_not_break_tracking(self):
        self.progress.begin(0, self.base/'one.log')
        old = self.progress.event_path
        old.write_text('{', encoding='utf-8')
        self.progress.poll()
        self.progress.done(0)
        self.progress.begin(1, self.base/'two.log')
        self.assertNotEqual(old, self.progress.event_path)
        self.progress.poll()
        self.assertIsNone(self.progress.snapshot()['stages'][1]['current'])

    def test_api_cannot_close_running_job_and_downloads_only_current_outputs(self):
        self.progress.bind_server(create_server(progress=self.progress))
        url = self.progress.url.removesuffix('/progress')
        with urllib.request.urlopen(url+'/api/progress') as response:
            self.assertEqual(json.load(response)['frames'], 12)
        request = urllib.request.Request(url+'/close', b'')
        with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 403)
        request.add_header('X-Annotation-Token', self.progress.token)
        with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(url+'/artifact?key=../../worm_pipeline.py')
        # Even a stale video on disk must not be exposed after an early stop.
        folder=self.base/'phenotypes-centerlines-niu';folder.mkdir()
        (folder/'test-progress_annotated_full-frame.mp4').write_bytes(b'stale video')
        self.progress.done(0); self.progress.done(1)
        self.progress.finish(self.base,self.config,'fit')
        self.assertEqual(self.progress.snapshot()['artifacts'], [])
        with urllib.request.urlopen(request) as response: self.assertTrue(json.load(response)['closed'])
        self.assertTrue(self.progress.closed.is_set())


if __name__=='__main__': unittest.main()
