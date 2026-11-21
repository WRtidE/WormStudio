"""Exact geometry and independent snapshots after storage/runtime optimizations."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from serialization import json_safe
from image_io import read_image, write_png
from body_frame_cache import cached_frames
from body_sequence_solver import signature, solve
from run_storage import copy_tracking_snapshot
from test_current_pipeline import single_worm_fixture


class SignatureTests(unittest.TestCase):
    def test_geometry_appearance_and_anchor_dependencies_invalidate_the_cache(self):
        before = signature()
        read = Path.read_bytes
        for name in ('head_appearance.py', 'head_tail.py', 'analyze_phenotypes.py', 'body_frame_cache.py',
                     'skeleton_geometry.py', 'head_evidence.py', 'tracking_data.py', 'image_io.py', 'serialization.py'):
            with self.subTest(name=name):
                def changed(path):
                    return read(path) + (b'\n# changed dependency' if path.name == name else b'')
                with patch.object(Path, 'read_bytes', changed):
                    self.assertNotEqual(signature(), before)
        self.assertEqual(signature(), before)


class FrameCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config, self.source, self.masks = single_worm_fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_mapped_pixels_and_solver_match_the_original_list_input_exactly(self):
        cap = cv2.VideoCapture(self.source['video'])
        original = []
        try:
            for _ in range(self.source['frames']):
                ok, image = cap.read()
                self.assertTrue(ok)
                original.append(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
        finally:
            cap.release()
        options = dict(manual=dict(frame=0, point=[160,185]), source_invalid=[8])
        expected = solve(self.masks[1], original, self.source['fps'], **options)
        for limit in (0, 256*1024*1024):
            with self.subTest(memory_limit=limit):
                with cached_frames(self.source, lambda w,f: self.masks[w][f], 1, memory_limit=limit) as (gray, masks):
                    np.testing.assert_array_equal(gray, original)
                    np.testing.assert_array_equal(masks, self.masks[1])
                    self.assertFalse(gray.flags.writeable)
                    actual = solve(masks, gray, self.source['fps'], **options)
                    np.testing.assert_array_equal(actual[0], expected[0])
                    self.assertEqual(json_safe(actual[1:]), json_safe(expected[1:]))
                    mappings = [a._mmap for a in (gray,masks) if isinstance(a,np.memmap)]
                    paths = [Path(a.filename) for a in (gray,masks) if isinstance(a,np.memmap) and a.filename is not None]
                self.assertTrue(all(not p.exists() for p in paths))
                self.assertTrue(all(m.closed for m in mappings))

    def test_cancellation_closes_mappings_and_removes_temporary_files(self):
        with tempfile.TemporaryDirectory() as parent:
            allocate = tempfile.TemporaryFile
            with patch('body_frame_cache.TemporaryFile', side_effect=lambda **kw: allocate(dir=parent, **kw)):
                def cancel(current, total):
                    if current == 2:
                        raise RuntimeError('cancelled')
                with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                    with cached_frames(self.source, lambda w,f: self.masks[w][f], 1, progress=cancel, memory_limit=0):
                        self.fail('Cancelled loading must not yield frames')
            self.assertEqual(list(Path(parent).iterdir()), [])

    def test_missing_source_frame_is_not_replaced_or_silently_skipped(self):
        source = dict(self.source, frames=self.source['frames']+1)
        with self.assertRaisesRegex(ValueError, 'F10'):
            with cached_frames(source, lambda w,f: self.masks[w][f], 1, memory_limit=0):
                self.fail('An incomplete video must not reach the solver')

    def test_mask_shape_mismatch_is_rejected_before_solving(self):
        with self.assertRaisesRegex(ValueError, '尺寸'):
            with cached_frames(self.source, lambda w,f: np.zeros((3,3),bool), 1, memory_limit=0):
                self.fail('An incorrectly sized mask must not be broadcast')


class TrackingSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/'old'
        self.mask = self.source/'masks/worm-1/000000.png'
        self.mask.parent.mkdir(parents=True)
        self.image = np.full((12,20), 255, np.uint8)
        write_png(self.mask, self.image)
        (self.source/'summary.json').write_text('{}', encoding='utf-8')
        (self.source/'frames-test').mkdir()
        (self.source/'frames-test/000000.jpg').write_bytes(b'temporary frame')

    def tearDown(self):
        self.temp.cleanup()

    def test_shared_masks_detach_on_edits_and_survive_original_run_deletion(self):
        probe = self.root/'link-probe'
        try:
            os.link(self.mask, probe)
            supported = True
            probe.unlink()
        except OSError:
            supported = False
        derived = self.root/'derived'
        copy_tracking_snapshot(self.source, derived)
        saved = derived/'masks/worm-1/000000.png'
        # On filesystems supporting links this saves the actual PNG data blocks.
        if supported:
            self.assertTrue(os.path.samefile(self.mask, saved))
        self.assertFalse(os.path.samefile(self.source/'summary.json', derived/'summary.json'))
        self.assertFalse((derived/'frames-test').exists())
        write_png(self.mask, np.zeros_like(self.image))
        np.testing.assert_array_equal(read_image(saved, True), self.image)
        copy_tracking_snapshot(derived, self.root/'third')
        write_png(saved, np.full_like(self.image, 127))
        third = self.root/'third/masks/worm-1/000000.png'
        np.testing.assert_array_equal(read_image(third, True), self.image)
        shutil.rmtree(self.source)
        np.testing.assert_array_equal(read_image(saved, True), np.full_like(self.image, 127))
        np.testing.assert_array_equal(read_image(third, True), self.image)

    def test_filesystems_without_hardlinks_fall_back_to_independent_copies(self):
        with patch('run_storage.os.link', side_effect=OSError('not supported')) as link:
            copy_tracking_snapshot(self.source, self.root/'copy')
        self.assertEqual(link.call_count, 1)
        saved = self.root/'copy/masks/worm-1/000000.png'
        self.assertFalse(os.path.samefile(self.mask,saved))
        self.assertEqual(self.mask.read_bytes(), saved.read_bytes())

    def test_failed_image_publication_preserves_all_snapshots_and_cleans_temporary_file(self):
        copy_tracking_snapshot(self.source, self.root/'copy')
        original = self.mask.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('publication failed')):
            with self.assertRaisesRegex(OSError, 'publication failed'):
                write_png(self.mask, np.zeros_like(self.image))
        self.assertEqual(self.mask.read_bytes(), original)
        self.assertEqual((self.root/'copy/masks/worm-1/000000.png').read_bytes(), original)
        self.assertEqual(list(self.mask.parent.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
