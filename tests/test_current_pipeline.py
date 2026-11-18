"""End-to-end single-worm contract: quality gates, metrics, HTTP and head review."""
import csv
import json
import shutil
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import analyze_phenotypes as metrics
import extract_body_sequence as body_sequence
import manual_worm_annotation as manual
import quality_stage
import worm_pipeline
from frame_metrics import present_frame
from pipeline_progress import PipelineProgress
from quality_fixture import make_fixture
from quality_schema import VERSION
from workspace_sessions import confirm_head

SOURCE = 'centerlines-body-sequence'


def single_worm_fixture(root):
    """Narrow the shared synthetic fixture to the one worm the current method accepts."""
    config, source, masks = make_fixture(root)
    config['groups'][0]['worms'] = [w for w in config['groups'][0]['worms'] if w['id'] == 1]
    config.update(centerline_method='body_sequence', head_detection='taper_seed', identity_confirmations=[])
    source.update(worm_ids=[1], model='SAM2 Hiera Large', sam2_model='large')
    base = root / 'outputs' / config['sample']
    (base / 'pipeline-config.json').write_text(json.dumps(config), encoding='utf-8')
    (base / 'sam2-tracking' / 'summary.json').write_text(json.dumps(source), encoding='utf-8')
    (base / 'sam2-tracking' / 'component-cleanup.json').write_text(json.dumps(dict(changes=[])))
    (root / 'config').mkdir()
    for name in ('phenotype_config.json', 'motion_constraints.json'):
        shutil.copy2(Path(__file__).resolve().parents[1] / 'config' / name, root / 'config' / name)
    return config, source, masks


class CurrentPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config, self.source, _ = single_worm_fixture(self.root)
        self.settings = json.loads((self.root / 'config/phenotype_config.json').read_text(encoding='utf-8'))

    def tearDown(self):
        self.temp.cleanup()

    def analyze(self, sample):
        with patch.object(metrics, 'render'), patch.object(metrics, 'write_report'), \
                patch('head_appearance.extract_profiles', side_effect=AssertionError('Unexpected legacy video pass')):
            return metrics.analyze(sample, self.settings, 'body_sequence', SOURCE)

    def run_pipeline(self, sample):
        with patch.object(metrics, 'ROOT', self.root), patch.object(body_sequence, 'ROOT', self.root), \
                patch.object(worm_pipeline, 'ROOT', self.root):
            quality_stage.run(sample)
            body_sequence.run(sample, 1)
            return self.analyze(sample)

    def test_csv_json_overlay_and_http_share_one_quality_contract(self):
        report = self.run_pipeline(self.config['sample'])
        output = self.root / 'outputs' / self.config['sample'] / f'phenotypes-{SOURCE}'
        rows = list(csv.DictReader((output / 'per_frame_metrics.csv').open(encoding='utf-8-sig')))
        as_json = json.loads((output / 'per_frame_metrics.json').read_text(encoding='utf-8'))
        quality = json.loads((output / 'quality.json').read_text(encoding='utf-8'))
        self.assertEqual(len(rows), self.source['frames'])
        for row, item, gate in zip(rows, as_json, quality):
            self.assertEqual(present_frame(row), present_frame(item))
            self.assertEqual(present_frame(row)['quality']['permissions'], gate['permissions'])
        self.assertTrue(any(present_frame(r)['length_um'] is None for r in as_json))
        self.assertGreater(report['worms'][0]['valid_frames'], 0)
        progress = PipelineProgress()
        progress.data.update(sample=self.config['sample'], fps=10., frames=self.source['frames'])
        progress.config = self.config
        progress.artifacts['metrics'] = output / 'per_frame_metrics.csv'
        progress.bind_server(manual.create_server(progress=progress))
        try:
            url = progress.url.removesuffix('/progress') + '/api/frame-metrics?run=' + self.config['sample']
            with urllib.request.urlopen(url) as response:
                payload = json.load(response)
            self.assertEqual(payload['frames'][0][0], present_frame(as_json[0]))
        finally:
            progress.close()

    def test_head_confirmation_recomputes_in_a_new_run_and_keeps_the_old_one(self):
        sample = self.config['sample']
        self.run_pipeline(sample)
        folder = self.root / 'outputs' / sample / f'phenotypes-{SOURCE}'
        before = (folder / 'per_frame_metrics.json').read_bytes()
        with patch.object(metrics, 'ROOT', self.root), patch.object(body_sequence, 'ROOT', self.root), \
                patch.object(worm_pipeline, 'ROOT', self.root):
            revised = confirm_head(self.root, sample, 1, 0, 1)
            self.assertNotEqual(revised['sample'], sample)
            self.assertEqual((folder / 'per_frame_metrics.json').read_bytes(), before)
            body_sequence.run(revised['sample'], 1)
            reviewed = self.analyze(revised['sample'])
        self.assertGreater(reviewed['worms'][0]['head_confirmed_frames'], 0)
        self.assertNotEqual((folder / 'per_frame_metrics.json').read_bytes(),
                            (self.root / 'outputs' / revised['sample'] / f'phenotypes-{SOURCE}' /
                             'per_frame_metrics.json').read_bytes())

    def test_quality_schema_version_is_written_for_every_row(self):
        report = self.run_pipeline(self.config['sample'])
        output = self.root / 'outputs' / self.config['sample'] / f'phenotypes-{SOURCE}'
        rows = json.loads((output / 'per_frame_metrics.json').read_text(encoding='utf-8'))
        self.assertEqual(report['quality_schema_version'], VERSION)
        self.assertEqual(len(rows), self.source['frames'])
        self.assertTrue(all(row['quality_schema_version'] == VERSION for row in rows))


if __name__ == '__main__':
    unittest.main()
