"""Large units are review candidates, not rejected alignments."""

import unittest

from alignment_research.diagnostics import large_source_unit_candidates
from alignment_research.evaluation import large_source_units_markdown
from alignment_research.render import render_response


def unit(count: int, status: str = "aligned") -> dict:
    source = [f"s{number:03d}" for number in range(1, count + 1)]
    return {
        "source": source,
        "target": ["t001"] if status != "unrepresented" else [],
        "source_head": source[0],
        "target_head": "t001" if status != "unrepresented" else None,
        "status": status,
        "note": "",
    }


class LargeSourceUnitDiagnosticTests(unittest.TestCase):
    def test_threshold_begins_at_three_and_preserves_actionable_context(self):
        record = {
            "ref": "GEN.1.1",
            "alignments": [unit(2), unit(3), unit(4, "unrepresented")],
        }

        candidates = large_source_unit_candidates([record])

        self.assertEqual([item["unit"] for item in candidates], [2, 3])
        self.assertEqual([item["source_count"] for item in candidates], [3, 4])
        self.assertEqual(candidates[1]["status"], "unrepresented")
        self.assertEqual(candidates[0]["source"], unit(3)["source"])

    def test_review_render_flags_the_candidate_without_calling_it_invalid(self):
        packet = {
            "ref": "GEN.1.1",
            "source_tokens": [
                {"id": f"s{number:03d}", "surface": str(number), "lemma": "x"}
                for number in range(1, 4)
            ],
            "target_tokens": [{"id": "t001", "text": "word", "added": False}],
        }
        response = {
            "ref": "GEN.1.1",
            "alignments": [unit(3)],
            "review_notes": [],
        }

        rendered = "\n".join(render_response(packet, response))

        self.assertIn("large-source-unit review candidates", rendered)
        self.assertIn("heuristic, not evidence", rendered)
        self.assertIn("?large-source-unit:3", rendered)

    def test_report_is_explicit_that_the_candidate_is_not_an_error(self):
        candidates = large_source_unit_candidates(
            [{"ref": "GEN.1.1", "alignments": [unit(3)]}]
        )

        markdown = large_source_units_markdown(candidates)

        self.assertIn("GEN.1.1 unit 1", markdown)
        self.assertIn("3 source morphemes", markdown)
        self.assertIn("Size alone is not an error", markdown)
        self.assertEqual(
            large_source_units_markdown([]),
            "- Large-source-unit review candidates (3+ source morphemes): none",
        )
