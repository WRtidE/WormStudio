"""Coordinate, identity and publication regressions for SAM2 crop replay."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from crop_recovery import place, replay_incident, touched, incidents
from head_tail import anchors_in_crop
from mask_correction import prepare_recovered_edit


class CropReplayTests(unittest.TestCase):
    def test_all_four_sides_and_source_coordinates(self):
        box = [11, 8, 50, 37]
        canvas = [2, 0, 65, 53]
        for side in range(4):
            m = np.zeros((29,39),bool)
            if side == 0: m[12:18,0:9] = True
            if side == 1: m[:9,12:18] = True
            if side == 2: m[12:18,-9:] = True
            if side == 3: m[-9:,12:18] = True
            self.assertEqual(touched(m), [side])
            restored = place(m,box,canvas)
            self.assertEqual(touched(restored), [])
            np.testing.assert_array_equal(place(restored,canvas,box),m)

    def test_contiguous_missing_masks_only_join_a_boundary_event(self):
        records = [dict(frame=f,area_px=0 if f in [1,5,6] else 100,source_frame_invalid=False) for f in range(8)]
        self.assertEqual(incidents({4:[0]},records),[[4,5,6]])

    def replay(self, mode):
        original=[15,0,90,60]
        expanded=[0,0,90,60]
        real=[]
        for x in [18,10,17]:
            m=np.zeros((60,90),bool)
            m[24:32,x:x+36]=True
            real.append(m)
        def get(w,f):
            if w==1: return place(real[f],expanded,original)
            other=np.zeros_like(real[f])
            other[24:29,21:27]=True
            return place(other,expanded,original)
        def predictor(cap,box,frames,seed):
            np.testing.assert_array_equal(seed, real[0])
            for i in frames[1:]:
                candidate=real[i].copy()
                if i==2 and mode=='wrong_rejoin':
                    candidate=np.roll(candidate,23,axis=0)
                yield i,candidate
        ids=[1,2] if mode=='contact' else [1]
        with tempfile.TemporaryDirectory() as temp:
            pending,reason=replay_incident(predictor,None,expanded,original,original,[0,1,2],{1},1,ids,get,Path(temp))
            if pending:
                box,path=pending[1,1]
                np.testing.assert_array_equal(cv2.imread(str(path),0)>0,real[1])
            return set(pending),reason

    def test_only_boundary_frame_is_replaced_when_rejoin_matches(self):
        self.assertEqual(self.replay('ok'),({(1,1)},'recovered'))

    def test_wrong_rejoin_and_other_worm_preserve_original_track(self):
        self.assertEqual(self.replay('wrong_rejoin'),(set(),'track_disagreement'))
        self.assertEqual(self.replay('contact'),(set(),'other_worm_contact'))

    def test_reverse_seed_recovers_first_frames_and_preserves_a_true_edge(self):
        from analyze_phenotypes import load_sample, read_image, write_png
        from crop_recovery import run
        from quality_stage import run as guard_stage
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'中文 Windows'
            base=root/'outputs'/'replay'
            seg=base/'sam2-tracking'
            masks=seg/'masks'/'worm-1'
            masks.mkdir(parents=True)
            crop=[16,0,128,100]
            video=root/'sample.mp4'
            writer=cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'mp4v'), 12, (128,100))
            real=[]
            for f,x in enumerate((14,14,20,22,24,96)):
                m=np.zeros((100,128), bool)
                m[40:46,x:x+32]=True
                real.append(m)
                writer.write(cv2.cvtColor(m.astype(np.uint8)*175,cv2.COLOR_GRAY2BGR))
                write_png(masks/f'{f:06}.png', m[:,16:].astype(np.uint8)*255)
            writer.release()
            config=dict(sample='replay',video=str(video),centerline_method='self_contact',crop_xyxy=crop,
                        groups=[dict(name='g',anchor_frame=2,crop_xyxy=crop,worms=[dict(id=1)])])
            (base/'pipeline-config.json').write_text(json.dumps(config))
            (seg/'summary.json').write_text(json.dumps(dict(video=str(video),frames=6,fps=12,crop_xyxy=crop,worm_ids=[1])))
            with (seg/'tracking-quality.csv').open('w') as fp:
                writer=csv.writer(fp)
                writer.writerow(['frame','worm_id','low_confidence'])
                writer.writerows((f,1,False) for f in range(6))
            def predict(cap,box,frames,seed):
                self.assertEqual(frames,[2,1,0])
                np.testing.assert_array_equal(seed,real[2])
                for i in frames[1:]:
                    yield i,real[i]
            with patch('analyze_phenotypes.ROOT',root):
                guard_stage('replay')
                report=run('replay',predict)
                self.assertEqual([e['decision'] for e in report['events']],['recovered','source_boundary'])
                data,source,ids,get,bad=load_sample('replay')
                self.assertEqual(source['crop_xyxy'],[0,0,128,100])
                np.testing.assert_array_equal(get(1,0),real[0])
                self.assertFalse(bad[0,1])
                self.assertTrue(bad[5,1])
                self.assertEqual(read_image(masks/'000000.png', gray=True).shape,(100,112))

    def test_head_clicks_and_correction_seeds_keep_source_positions(self):
        crop=[20,10,80,70]
        canvas=[0,0,80,70]
        clicks=[dict(frame=1,point=[8,7]),dict(frame=4,point=[15,8],point_space='source')]
        shifted=anchors_in_crop(clicks,crop,canvas)
        self.assertEqual([s['point'] for s in shifted],[[28,17],[15,8]])
        self.assertEqual(clicks[0]['point'],[8,7])
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            original=np.zeros((60,60),np.uint8)
            original[7:12,8:16]=255
            seed=folder/'manual.png'
            cv2.imwrite(str(seed),original)
            entry=dict(id=1,mask_path=str(seed))
            source=dict(crop_xyxy=crop,head_annotations={'1':clicks},groups=[dict(crop_xyxy=crop,anchor_frame=0,worms=[entry])])
            report=dict(crop_xyxy=canvas,crop_recovery=dict(events=[dict(worm_id=1,decision='recovered',crop_xyxy=canvas)]))
            prepare_recovered_edit(source,report,folder/'review')
            np.testing.assert_array_equal(cv2.imread(entry['mask_path'],0),place(original,crop,canvas))
            np.testing.assert_array_equal(cv2.imread(str(seed),0),original)
            self.assertEqual(source['crop_xyxy'],canvas)
            self.assertEqual(source['head_annotations']['1'],shifted)
