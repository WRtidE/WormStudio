import copy
import unittest
from pathlib import Path

import cv2
import numpy as np

from compare_niu_skeleton import extract_niu, line_metrics
from extract_centerlines import select_path
from worm_pipeline import build_stages, FIT_DEFAULTS


class CenterlineMethodTests(unittest.TestCase):
    def test_conservative_topology_pipeline_is_independent_of_wormtracer_fit(self):
        config=dict(sample='routing-test',centerline_method='topology',wormtracer=copy.deepcopy(FIT_DEFAULTS))
        stages=build_stages(config,dict(ids=[1],frames=5),Path('cfg.json'),Path('metrics.json'))
        self.assertEqual([s['name'] for s in stages],['tracking','cleanup','quality','topology-worm-1','metrics','export'])
        self.assertIn('centerlines-topology',stages[-1]['command'])

    def test_pipeline_routes_all_three_real_methods(self):
        config=dict(sample='routing-test',wormtracer=copy.deepcopy(FIT_DEFAULTS))
        info=dict(ids=[2,9],frames=5)
        for method in ('wormtracer','niu','hybrid'):
            stages=build_stages(dict(config,centerline_method=method),info,Path('cfg.json'),Path('metrics.json'))
            names=[s['name'] for s in stages]
            self.assertEqual('fit-worm-2' in names,method!='niu')
            self.assertEqual('niu-worm-2' in names,method=='niu')
            self.assertEqual('hybrid-worm-9' in names,method=='hybrid')
            metrics=next(s for s in stages if s['name']=='metrics')['command']
            self.assertEqual(metrics[metrics.index('--centerline-method')+1],method)
            source=metrics[metrics.index('--wormtracer-source')+1]
            export=next(s for s in stages if s['name']=='export')
            self.assertEqual(export['command'][-1],source)
            self.assertIn('phenotypes-'+source,export['outputs'][0])

    def test_hybrid_keeps_uncertainty_and_does_not_average_crossing_paths(self):
        mask=np.zeros((160,200),np.uint8)
        cv2.circle(mask,(80,80),45,1,9)
        niu=extract_niu(mask.astype(bool))
        metric=line_metrics(niu['ordered'],mask.astype(bool),niu['raw_xy'],niu['radius'])
        path=np.column_stack([np.linspace(20,150,101),np.full(101,80)])
        p,selected,reasons=select_path(niu,metric,path,dict(centerline_valid=False,
            quality_reasons=['endpoint_inferred_from_motion']))
        np.testing.assert_array_equal(p,path)
        self.assertEqual(selected,'wormtracer')
        self.assertEqual(reasons,['endpoint_inferred_from_motion'])
        p,_,_=select_path(niu,metric)
        self.assertIsNone(p)

    def test_simple_supported_mask_uses_niu_in_hybrid(self):
        mask=np.zeros((160,210),np.uint8);cv2.line(mask,(25,80),(180,80),1,15)
        niu=extract_niu(mask.astype(bool))
        metric=line_metrics(niu['ordered'],mask.astype(bool),niu['raw_xy'],niu['radius'])
        p,selected,reasons=select_path(niu,metric,niu['ordered']+[0,5],dict(centerline_valid=True))
        self.assertEqual(selected,'niu');self.assertFalse(reasons)
        np.testing.assert_array_equal(p,niu['ordered'])


if __name__=='__main__':unittest.main()
