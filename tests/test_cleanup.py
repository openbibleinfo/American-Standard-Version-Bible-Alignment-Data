"""A chapter reset cannot reach neighboring or shared repository state."""

import tempfile
import unittest
from pathlib import Path

from alignment_research.cleanup import clean_chapter


class CleanChapterTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, text: str = "x") -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_removes_only_selected_chapter_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = [
                "config/DAN.2-variant-decisions.json",
                "config/DAN.2-verse-divisions.json",
                "config/DAN.2-unsourced-verses.json",
                "data/alignments/DAN.2/verses/01.json",
                "data/alignments/DAN.2.jsonl",
                "data/alignments/DAN.2-run.json",
                "data/driver/DAN.2/packet.json",
                "data/batch/DAN.2/review.json",
                "data/output/DAN.2/verses/01.json",
                "data/output/reverse-interlinear/DAN.2.html",
                "needs-alignment/DAN.2.md",
                "reports/DAN.2-validation.md",
                "reports/DAN.2-any-future-report.json",
            ]
            retained = [
                "config/alignment-instructions-hebrew.md",
                "config/sources.lock.json",
                "data/normalized/DAN.2/source-tokens.jsonl",
                "data/vendor/source.xml",
                "data/alignments/DAN.20/verses/01.json",
                "data/output/reverse-interlinear/DAN.20.html",
                "data/output/reverse-interlinear/index.html",
                "reports/DAN.20-validation.md",
            ]
            for relative in selected + retained:
                self._write(root, relative)

            removed = clean_chapter(root, "DAN.2", "DAN.2")

            self.assertEqual(len(removed), 13)
            for relative in selected:
                self.assertFalse((root / relative).exists(), relative)
            for relative in retained:
                self.assertTrue((root / relative).exists(), relative)

            self.assertEqual(clean_chapter(root, "DAN.2", "DAN.2"), [])

    def test_requires_exact_canonical_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = self._write(root, "data/alignments/DAN.2.jsonl")
            for ref, confirmation in (
                ("dan.2", "DAN.2"),
                ("DAN.2", "DAN.3"),
                ("DAN.999", "DAN.999"),
                ("DAN.2-3", "DAN.2-3"),
                ("../../DAN.2", "../../DAN.2"),
            ):
                with self.subTest(ref=ref, confirmation=confirmation):
                    with self.assertRaises(ValueError):
                        clean_chapter(root, ref, confirmation)
                    self.assertTrue(marker.exists())
