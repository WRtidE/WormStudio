"""Check that head identity, ordered points and downstream metrics agree."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import cv2
import numpy as np
import analyze_phenotypes as metrics
from test_head_tail import poses


class HeadMetricIntegrationTests(unittest.TestCase):
    def test_later_manual_anchor_reorients_metrics_without_changing_geometry(self):
        config=json.loads((metrics.ROOT/'config/phenotype_config.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=root/'outputs'/'test'
            fit=base/'centerlines-niu'/'worm-7';fit.mkdir(parents=True)
            p=poses()+[20,40];np.save(fit/'centerlines.npy',p)
            quality=[dict(frame=i,quality_reasons=[],mask_valid=True,topology='simple',selected_candidate='niu',branch_score_margin=None) for i in range(len(p))]
            (fit/'quality.json').write_text(json.dumps(quality))
            masks=[]
            for line in p:
                mask=np.zeros((100,160),np.uint8)
                cv2.polylines(mask,[np.rint(line).astype(np.int32)],False,1,12)
                masks.append(mask.astype(bool))
            video=root/'synthetic.mp4'
            writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'mp4v'),10.,(160,100))
            for mask in masks: writer.write(cv2.cvtColor(mask.astype(np.uint8)*255,cv2.COLOR_GRAY2BGR))
            writer.release()
            source=dict(video=str(video),crop_xyxy=[0,0,160,100],frames=len(p),fps=10.,scale_um_per_px=1.,scale_source='synthetic')
            (base/'pipeline-config.json').write_text(json.dumps(dict(head_annotations={'7':dict(frame=12,point=p[12,-1].tolist())})))
            with patch.object(metrics,'ROOT',root),patch.object(metrics,'load_sample',return_value=(base,source,[7],lambda w,f:masks[f],{})),patch.object(metrics,'render'),patch.object(metrics,'write_report'):
                report=metrics.analyze('test',config,'niu','centerlines-niu')
            self.assertEqual(report['worms'][0]['head_confirmed_frames'],len(p))
            folder=base/'phenotypes-centerlines-niu'
            with (folder/'per_frame_metrics.csv').open(encoding='utf-8-sig') as handle:rows=list(csv.DictReader(handle))
            with np.load(folder/'centerlines.npz') as saved:
                for f,row in enumerate(rows):
                    np.testing.assert_array_equal(saved[f'w7_f{f}'],p[f,::-1])
                    self.assertEqual(float(row['head_x_crop_px']),p[f,-1,0])
                    self.assertAlmostEqual(float(row['head_candidate_angle_deg']),metrics.signed_angle(p[f,::-1]))
                    self.assertEqual(row['head_metrics_valid'],'True')
                    self.assertNotIn('_candidate',row['movement_state'])
            np.testing.assert_array_equal(np.load(fit/'centerlines.npy'),p)
