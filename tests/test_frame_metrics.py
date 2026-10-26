"""Verify measurement validity survives CSV -> HTTP -> viewer data."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from frame_metrics import load_frame_metrics, present_frame
from pipeline_progress import PipelineProgress
import manual_worm_annotation as manual


class FrameMetricsTests(unittest.TestCase):
    def test_rejected_posture_preserves_independent_area_and_real_zero_speed(self):
        row = dict(worm_id='1',mask_valid='True',centerline_valid='False',volume_valid='True',
            head_metrics_valid='True',head_identity_confirmed='True',length_um='650',area_um2='15000',
            volume_estimate_um3='200000',speed_um_s='0',head_candidate_angle_smoothed_deg='25')
        actual = present_frame(row)
        self.assertEqual(actual['area_um2'],15000)
        self.assertEqual(actual['speed_um_s'],0)
        for key in ['length_um','volume_um3','head_angle_deg']:
            self.assertIsNone(actual[key])
        self.assertFalse(actual['head_confirmed'])
        row.update(mask_valid='False',speed_um_s='100')
        self.assertIsNone(present_frame(row)['speed_um_s'])

    def test_nonfinite_missing_and_unconfirmed_head_are_explicit(self):
        row=dict(worm_id=3,mask_valid=True,centerline_valid=True,head_metrics_valid=True,
            head_identity_confirmed=False,area_um2='inf',length_um='NaN',head_candidate_angle_smoothed_deg='0')
        result=present_frame(row)
        self.assertIsNone(result['area_um2']);self.assertIsNone(result['length_um'])
        self.assertEqual(result['head_angle_deg'],0)
        self.assertIn('候选',result['head_status'])
        json.dumps(result,allow_nan=False)

    def test_v5_candidate_is_visible_without_unlocking_head_metrics(self):
        for status in ('unconfirmed_relative_motion_candidate','unconfirmed_appearance_candidate','unconfirmed_temporal_candidate'):
            row=dict(worm_id=1,mask_valid=True,centerline_valid=True,identity_valid=True,
                     head_identity_confirmed=False,head_status=status,head_endpoint=0,
                     head_metrics_valid=False,head_candidate_angle_deg=None)
            result=present_frame(row)
            self.assertTrue(result['head_candidate'])
            self.assertEqual(result['endpoint_labels'],['H?','T?'])
            self.assertFalse(result['head_confirmed'])
            self.assertIsNone(result['head_angle_deg'])

    def test_timeline_alignment_completeness_and_session_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'per_frame_metrics.csv'
            fields=['frame','worm_id','mask_valid','centerline_valid','length_um']
            rows=[dict(frame=f,worm_id=w,mask_valid=True,centerline_valid=True,length_um=100+f+w) for f in range(3) for w in [2,1]]
            def write(items):
                with path.open('w',newline='',encoding='utf-8-sig') as handle:
                    writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(items)
            write(rows)
            result=load_frame_metrics(path,29.97,3)
            self.assertEqual(result['fps'],29.97)
            self.assertEqual(result['frames'][2][0]['length_um'],103)
            for items in [rows[:-1],rows+[rows[0]],rows+[dict(rows[0],frame=3)]]:
                write(items)
                with self.assertRaises(ValueError):load_frame_metrics(path,30,3)
            write(rows)
            progress=PipelineProgress()
            progress.data.update(sample='current',fps=29.97,frames=3)
            progress.artifacts['metrics']=path
            with patch.object(manual,'ROOT',root):
                progress.bind_server(manual.create_server(progress=progress))
                try:
                    url=progress.url.removesuffix('/progress')
                    with urllib.request.urlopen(url+'/api/frame-metrics?run=current') as response:
                        payload=json.load(response)
                    self.assertEqual(payload['frames'][2][0]['length_um'],103)
                    for query in ['old','../../per_frame_metrics.csv']:
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            urllib.request.urlopen(url+'/api/frame-metrics?run='+query)
                        self.assertEqual(error.exception.code,409)
                finally:progress.close()


if __name__=='__main__':unittest.main()
