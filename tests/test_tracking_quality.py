import unittest
from tracking_quality import mark_quality, reference_area


class TrackingQualityTests(unittest.TestCase):
    def test_majority_missing_detections_do_not_erase_valid_initial_frames(self):
        areas=[5800,5700,5900]+[0]*12
        rows=[dict(frame=f,worm_id=1,area_px=a,ambiguous_overlap_px=0) for f,a in enumerate(areas)]
        mark_quality(rows,[1])
        self.assertEqual(reference_area(areas),5800)
        self.assertTrue(all(not r['low_confidence'] for r in rows[:3]))
        self.assertTrue(all(r['low_confidence'] for r in rows[3:]))

    def test_outliers_overlaps_and_review_intervals_remain_invalid(self):
        rows=[dict(frame=f,worm_id=1,area_px=a,ambiguous_overlap_px=20 if f==1 else 0)
              for f,a in enumerate([1000,1000,1600,1000,1000])]
        mark_quality(rows,[1],[dict(worm_ids=[1],frames=[4,4])])
        self.assertFalse(rows[0]['low_confidence'])
        self.assertTrue(all(r['low_confidence'] for r in rows[1:]))


if __name__=='__main__':unittest.main()
