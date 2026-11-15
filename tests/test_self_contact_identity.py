import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from refit_self_contacts import review_identity


class SelfContactIdentityTests(unittest.TestCase):
    def review(self, reasons, ambiguous=False):
        rows = [
            {
                "frame": i,
                "worm_id": 1,
                "mask_valid": i != 5,
                "identity_valid": i < 5,
                "identity_segment": int(i >= 5),
                "identity_status": "test",
                "identity_evidence": "synthetic",
                "source_frame_invalid": False,
                "reasons": reasons if i == 5 else [],
            }
            for i in range(12)
        ]
        source = {"frames": len(rows), "fps": 12}
        with patch("refit_self_contacts.ImageEvidence") as provider:
            image = provider.return_value

            def compare(worm, before, after):
                candidates = [{"overlap": 0.95}]
                if ambiguous and 5 in after:
                    candidates.append({"overlap": 0.35})
                return {
                    "accepted": True,
                    "after": [
                        {
                            "candidate_count": len(candidates),
                            "candidates": candidates,
                            "foreground_support": 0.95,
                        }
                    ],
                }

            image.compare.side_effect = compare
            reviewed, audit = review_identity(source, Path("."), 1, rows, [])
            image.close.assert_called_once()
        self.assertFalse(rows[5]["mask_valid"])
        return reviewed, audit

    def test_jump_frame_needs_its_own_source_evidence(self):
        rows, audit = self.review(["centroid_jump"])
        self.assertTrue(rows[5]["identity_valid"] and rows[5]["mask_valid"])
        self.assertEqual(rows[4]["identity_segment"], rows[6]["identity_segment"])
        self.assertEqual(
            [item["frame"] for item in audit["image_supported_jumps"]], [5]
        )

    def test_neighbour_link_does_not_override_an_ambiguous_frame_or_bad_mask(self):
        for reasons, ambiguous in [
            (["centroid_jump"], True),
            (["fragmented_mask"], False),
        ]:
            with self.subTest(reasons=reasons):
                rows, audit = self.review(reasons, ambiguous)
                self.assertFalse(rows[5]["mask_valid"])
                self.assertTrue(rows[6]["identity_valid"])
                self.assertFalse(audit["image_supported_jumps"])


if __name__ == "__main__":
    unittest.main()
