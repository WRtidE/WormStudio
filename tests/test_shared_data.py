"""Shared storage boundaries preserve missing values and reject corrupt outputs."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from image_io import read_image, write_png
from review_configured_video import verify
from serialization import json_safe, write_json_records
from tracking_data import load_sample


class SharedDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_streamed_json_preserves_unicode_missing_values_and_inputs(self):
        rows = [dict(label='线虫', value=np.float64(np.nan), worm=np.int64(7)),
                dict(values=(1., np.float32(np.inf), np.float64(3.5)))]
        target = self.root/'records.json'
        write_json_records(target, iter(rows))
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), json_safe(rows))
        self.assertTrue(np.isnan(rows[0]['value']))
        self.assertIsInstance(rows[1]['values'], tuple)
        write_json_records(target, iter(()))
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), [])

    def test_stream_failure_preserves_existing_file_and_removes_partial_output(self):
        target = self.root/'records.json'
        target.write_text('[{"saved": true}]', encoding='utf-8')
        original = target.read_bytes()
        def failing():
            yield dict(frame=0)
            raise OSError('interrupted')
        with self.assertRaisesRegex(OSError, 'interrupted'):
            write_json_records(target, failing())
        self.assertEqual(target.read_bytes(), original)
        with patch.object(Path, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaisesRegex(PermissionError, 'locked'):
                write_json_records(target, [dict(frame=1)])
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_tracking_reader_uses_explicit_workspace_and_shared_image_io(self):
        base = self.root/'outputs'/'sample'
        folder = base/'sam2-tracking'
        masks = folder/'masks'/'worm-7'
        masks.mkdir(parents=True)
        (base/'pipeline-config.json').write_text('{}', encoding='utf-8')
        (folder/'summary.json').write_text(json.dumps(dict(frames=1, worm_ids=[7])), encoding='utf-8')
        (folder/'tracking-quality.csv').write_text('frame,worm_id,low_confidence\n0,7,False\n', encoding='utf-8')
        pixels = np.full((12, 20), 255, np.uint8)
        write_png(masks/'000000.png', pixels)
        data, _, ids, get_mask, bad = load_sample('sample', root=self.root)
        self.assertEqual(data, folder)
        self.assertEqual(ids, [7])
        np.testing.assert_array_equal(get_mask(7, 0), pixels > 0)
        np.testing.assert_array_equal(read_image(masks/'000000.png', True), pixels)
        self.assertFalse(bad[0, 7])

    def records(self):
        return [dict(worm_id=w, frame=f, mask_valid=True, centerline_valid=True,
                     valid_for_summary=True, identity_valid=True, length_um=20)
                for f in range(2) for w in (2, 7)]

    def save_records(self, rows, lines=None):
        with (self.root/'per_frame_metrics.csv').open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.records()[0]))
            writer.writeheader()
            writer.writerows(rows)
        if lines is None:
            lines = {f'w{w}_f{f}': np.zeros((101, 2)) for w in (2, 7) for f in range(2)}
        np.savez_compressed(self.root/'centerlines.npz', **lines)

    def verify_records(self, **source):
        return verify(self.root, None, dict(frames=2, fps=12, **source), [2, 7], (128, 80))

    def test_verification_keeps_counts_for_interleaved_worm_records(self):
        self.save_records(self.records())
        report = self.verify_records()
        self.assertEqual(report['metric_records'], 4)
        self.assertEqual(report['missing_centerlines'], 0)
        self.assertEqual(report['quality_counts'], {w: dict(mask_valid=2, posture_valid=2, identity_valid=2) for w in (2, 7)})

    def test_verification_rejects_duplicate_missing_and_out_of_range_records(self):
        rows = self.records()
        cases = [rows[:-1], rows + [rows[0]], [dict(rows[0], frame=2), *rows[1:]],
                 [dict(rows[0], worm_id=99), *rows[1:]]]
        for records in cases:
            with self.subTest(records=records):
                self.save_records(records)
                with self.assertRaisesRegex(RuntimeError, 'metric records'):
                    self.verify_records()

    def test_verification_rejects_corrupt_or_extra_centerlines(self):
        valid = {f'w{w}_f{f}': np.zeros((101, 2)) for w in (2, 7) for f in range(2)}
        cases = [dict(valid, w2_f0=np.zeros((100, 2))), dict(valid, w2_f0=np.full((101, 2), np.nan)),
                 dict(valid, w99_f0=np.zeros((101, 2))), {k: v for k, v in valid.items() if k != 'w2_f0'}]
        for lines in cases:
            with self.subTest(keys=list(lines)):
                self.save_records(self.records(), lines)
                with self.assertRaisesRegex(RuntimeError, 'centerline records'):
                    self.verify_records()

    def test_review_interval_must_remain_excluded_from_formal_measurements(self):
        interval = dict(frames=[1, 1], worm_ids=[7])
        rows = self.records()
        self.save_records(rows)
        with self.assertRaisesRegex(RuntimeError, 'Review interval'):
            self.verify_records(review_intervals=[interval])
        rows[-1].update(mask_valid=False, centerline_valid=False, valid_for_summary=False, length_um='')
        lines = {f'w{w}_f{f}': np.zeros((101, 2)) for w in (2, 7) for f in range(2) if (w, f) != (7, 1)}
        self.save_records(rows, lines)
        report = self.verify_records(review_intervals=[interval])
        self.assertEqual(report['review_intervals'], [dict(interval, excluded_records=1)])
        self.assertEqual(report['missing_centerlines'], 1)
