"""Exercise the export command's normal branch, including output-name routing."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import review_configured_video as review


class ExportEntryTests(unittest.TestCase):
    def test_normal_export_resolves_video_name_before_optional_refresh_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, ids, masks = {'frames':5}, [1], object()
            with patch.object(review,'ROOT',root), patch.object(review,'load_sample',return_value=(None,source,ids,masks,None)), \
                    patch.object(review,'tracking_review') as tracking, patch.object(review,'export_video') as export, \
                    patch('sys.argv',['review_configured_video.py','--sample','practice']):
                review.main()
            tracking.assert_called_once_with(root/'outputs/practice',source,ids,masks)
            self.assertEqual(export.call_args.args[0],root/'outputs/practice/phenotypes-centerlines-body-sequence')
            self.assertTrue(export.call_args.args[1].endswith('.mp4'))
            self.assertEqual(export.call_args.args[2:],(source,ids,masks))
