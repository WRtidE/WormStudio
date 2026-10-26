import json
from pathlib import Path
import tempfile
import unittest

from result_metrics import phenotype_results


class PhenotypeResultTests(unittest.TestCase):
    def test_twardis_candidate_is_visible_with_unknown_coverage(self):
        row=dict(worm_id=1,frames=120,valid_frames=100,mask_valid_frames=120,
            head_status='unconfirmed_twardis_motion_candidate',head_known_frames=80,
            head_confirmed_frames=0,head_candidate_frames=80,head_unknown_frames=40,
            head_candidate_amplitude_deg=25,directional_bends_per_min=30,
            movement_state_counts={'forward_candidate':50,'moving_head_unconfirmed':40})
        result=self.present(row)
        self.assertIn('TWARDIS',result['head']['label'])
        self.assertIn('无法判断 40 帧',result['head']['note'])
        metric=next(m for m in result['metrics'] if m['key']=='head_candidate_amplitude_deg')
        self.assertEqual(metric['status'],'candidate')
        self.assertEqual(metric['value'],25)
        self.assertEqual(result['movement'][0]['count'],50)

    def present(self, row, fps=12):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            report=dict(fps=fps,scale_um_per_px=1.07,config=dict(circling_window_s=5),worms=[row])
            (path/'summary.json').write_text(json.dumps(report),encoding='utf-8')
            return phenotype_results(path)['worms'][0]

    def test_unconfirmed_head_and_short_video_are_unavailable_not_zero(self):
        row=dict(worm_id=2,frames=12,valid_frames=12,mask_valid_frames=12,
                 median_length_um=666,median_area_um2=24812,median_volume_estimate_um3=694773,
                 mean_speed_um_s=41,head_status='unconfirmed_A_B_endpoints',
                 head_candidate_amplitude_deg=None,directional_bends_per_min=None,
                 activity_class_provisional='B',movement_state_counts={'moving_head_unconfirmed':12})
        result=self.present(row)
        metrics={r['key']:r for r in result['metrics']}
        self.assertEqual(len(metrics),6)
        self.assertEqual(metrics['median_area_um2']['unit'],'μm²')
        self.assertEqual(metrics['median_volume_estimate_um3']['status'],'estimate')
        self.assertIsNone(metrics['directional_bends_per_min']['value'])
        self.assertIn('头尾未确认',metrics['directional_bends_per_min']['note'])
        self.assertTrue(result['activity']['provisional'])
        self.assertEqual([m['count'] for m in result['movement']],[None,None,None])
        self.assertIn('不足 5 s',result['movement'][2]['note'])

    def test_confirmed_zero_frequency_is_a_measured_zero(self):
        row=dict(worm_id=1,frames=120,valid_frames=120,mask_valid_frames=120,
                 head_status='confirmed_by_user_point',directional_bends_per_min=0,
                 directional_bend_count=0,observed_valid_duration_s=10,
                 head_candidate_amplitude_deg=8,head_candidate_half_excursion_deg=6,
                 movement_state_counts={'forward':48,'stationary':72},activity_class_provisional='B')
        result=self.present(row)
        frequency=next(m for m in result['metrics'] if m['key']=='directional_bends_per_min')
        self.assertEqual(frequency['value'],0)
        self.assertEqual(frequency['status'],'measured')
        self.assertEqual(frequency['unit'],'次/min')
        self.assertEqual(result['movement'][0]['count'],48)
        self.assertEqual(result['movement'][0]['ratio'],.4)
        self.assertEqual(result['movement'][2]['count'],0)

    def test_csv_fallback_nan_and_candidate_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            (path/'worm_summary.csv').write_text('worm_id,frames,valid_frames,head_status,head_candidate_amplitude_deg,mean_speed_um_s\n1,12,12,unconfirmed_taper_candidate,45,nan\n',encoding='utf-8')
            result=phenotype_results(path)['worms'][0]
            amplitude=next(m for m in result['metrics'] if m['key']=='head_candidate_amplitude_deg')
            speed=next(m for m in result['metrics'] if m['key']=='mean_speed_um_s')
            self.assertEqual(amplitude['status'],'candidate')
            self.assertEqual(amplitude['value'],45)
            self.assertIsNone(speed['value'])
            self.assertIsNone(result['movement'][0]['count'])


if __name__=='__main__':unittest.main()
