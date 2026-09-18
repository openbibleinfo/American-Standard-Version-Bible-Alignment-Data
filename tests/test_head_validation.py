import copy
import unittest

from alignment_research.alignment import validate_response


class HeadValidationTests(unittest.TestCase):
    def setUp(self):
        self.packet = {
            "ref": "TST.1.1",
            "source_tokens": [{"id": "s001"}, {"id": "s002"}],
            "target_tokens": [{"id": "t001"}, {"id": "t002"}],
        }
        self.response = {
            "ref": "TST.1.1",
            "review_notes": [],
            "alignments": [{
                "source": ["s001", "s002"],
                "source_head": "s001",
                "target": ["t001", "t002"],
                "target_head": "t002",
                "status": "noncompositional",
                "note": "An indivisible test phrase.",
            }],
        }

    def test_noncompositional_requires_both_heads(self):
        for field in ("source_head", "target_head"):
            with self.subTest(field=field):
                response = copy.deepcopy(self.response)
                response["alignments"][0][field] = None
                self.assertIn(
                    "unit 1 noncompositional heads must belong to the unit",
                    validate_response(self.packet, response),
                )

    def test_noncompositional_accepts_member_heads(self):
        self.assertEqual(validate_response(self.packet, self.response), [])


if __name__ == "__main__":
    unittest.main()
