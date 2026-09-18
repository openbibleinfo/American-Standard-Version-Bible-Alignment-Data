import json
import tempfile
import unittest
from pathlib import Path

from alignment_research.alignment import (
    cached_preparation, expand_candidate, preparation_inputs_sha256,
    project_source_tokens, reading_token_refs,
)
from alignment_research.greek_boundaries import (
    MAPPING_PATH, chapter_moves, restate_selected_spans, validate_projection_coverage,
)
from alignment_research.scope import chapter_scope


def token(index, ref="ACT.19.40", surface=None):
    return {"id": f"SBLGNT.{ref}.{index:03d}", "ref": ref, "surface": surface or str(index)}


def move(first, last, target="ACT.19.41"):
    return {"source_ref": "ACT.19.40", "target_ref": target,
            "first_source_id": first["id"], "last_source_id": last["id"],
            "surface": " ".join(str(n) for n in range(int(first["surface"]), int(last["surface"]) + 1))}


class GreekBoundaryTests(unittest.TestCase):
    def test_apparatus_embedded_verses_preserve_ids_and_provenance(self):
        unit = {"id": "SBLGNT.ROM.16.24.v021", "ref": "ROM.16.24",
                "base_span": {"start": 0, "end": 1}, "base_refs": ["ROM.16.24"],
                "base_source_ids": ["base"]}
        candidate = {"id": "r01", "text": "25 Τῷ δὲ δυναμένῳ, 26 φανερωθέντος δὲ νῦν, 27 μόνῳ σοφῷ θεῷ"}
        expanded = expand_candidate(unit, candidate, [], {})
        self.assertEqual([t["ref"] for t in expanded], [f"ROM.16.{v}" for v in (25, 26, 27) for _ in range(3)])
        self.assertEqual(expanded[3]["id"], unit["id"] + ".r01.004")
        self.assertTrue(all(t["source_ref"] == "ROM.16.24" for t in expanded))
        unit["candidates"] = [{**candidate, "expanded_tokens": expanded}]
        _, applied = project_source_tokens([{"id": "base"}], [unit], {
            unit["id"]: {"unit_id": unit["id"], "selected_reading": "r01", "status": "selected"}})
        self.assertEqual(applied[0]["refs"], ["ROM.16.24", "ROM.16.25", "ROM.16.26", "ROM.16.27"])

    def test_reference_only_mark_ending_adds_no_words(self):
        self.assertEqual(reading_token_refs("+ 9–20", "MRK.16.8"), [])

    def test_nonconsecutive_embedded_verse_labels_fail(self):
        with self.assertRaisesRegex(ValueError, "nonconsecutive"):
            reading_token_refs("25 Τῷ 27 μόνῳ", "ROM.16.24")

    def test_partial_selected_alternative_moves_without_swallowing_remainder(self):
        tokens = [token(n) for n in range(1, 4)]
        for n, t in enumerate(tokens, 1):
            t["id"] = f"SBLGNT.ACT.19.40.v001.r01.{n:03d}"
        decision = {"unit_id": "v001", "ref": "ACT.19.40", "refs": ["ACT.19.40"],
                    "projected_source_ids": [t["id"] for t in tokens]}
        moved, applied = restate_selected_spans(tokens, [decision], [move(tokens[0], tokens[1])])
        by_id = {t["id"]: t for t in moved}
        self.assertEqual(by_id[tokens[0]["id"]]["ref"], "ACT.19.41")
        self.assertEqual(by_id[tokens[0]["id"]]["source_ref"], "ACT.19.40")
        self.assertEqual(by_id[tokens[2]["id"]], tokens[2])
        self.assertEqual(applied[0]["refs"], ["ACT.19.40", "ACT.19.41"])
        self.assertEqual([t["id"] for t in moved], [tokens[2]["id"], tokens[0]["id"], tokens[1]["id"]])

    def test_moves_do_not_cascade_and_preserve_order(self):
        tokens = [token(1), token(2, "ACT.19.41")]
        second = {"source_ref": "ACT.19.41", "target_ref": "ACT.19.42",
                  "first_source_id": tokens[1]["id"], "last_source_id": tokens[1]["id"], "surface": "2"}
        moved, _ = restate_selected_spans(tokens, [], [move(tokens[0], tokens[0]), second])
        self.assertEqual([t["ref"] for t in moved], ["ACT.19.41", "ACT.19.42"])

    def test_anchor_drift_and_overlap_fail(self):
        tokens = [token(1), token(2)]
        entry = move(*tokens)
        with self.assertRaisesRegex(ValueError, "expected"):
            restate_selected_spans(tokens, [], [{**entry, "surface": "wrong"}])
        with self.assertRaisesRegex(ValueError, "anchor missing"):
            restate_selected_spans(tokens[:1], [], [entry])
        with self.assertRaisesRegex(ValueError, "overlapping"):
            restate_selected_spans(tokens, [], [entry, entry])

    def test_coverage_rejects_orphan_duplicate_and_wrong_verse(self):
        source = [token(1)]
        packet = {"ref": "ACT.19.40", "source_tokens": [{"project_id": source[0]["id"]}]}
        validate_projection_coverage(source, [packet])
        for packets in ([], [packet, packet], [{**packet, "ref": "ACT.19.41"}]):
            with self.assertRaises(ValueError):
                validate_projection_coverage(source, packets)

    def test_cached_preparation_cannot_hide_an_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "data/output/ACT.19"
            (output / "verses").mkdir(parents=True)
            (output / "prepare-stamp.json").write_text(json.dumps({"inputs_sha256": "stamp"}))
            (output / "alignment-prep.jsonl").write_text("")
            (output / "projected-source-tokens.jsonl").write_text(json.dumps(token(1)) + "\n")
            with self.assertRaisesRegex(ValueError, "coverage"):
                cached_preparation(root, chapter_scope("ACT.19"), "stamp")

    def test_cached_preparation_rebuilds_missing_stale_or_malformed_isolated_packet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "data/output/ACT.19"
            (output / "verses").mkdir(parents=True)
            source = token(1)
            packet = {"ref": "ACT.19.40", "source_tokens": [{"project_id": source["id"]}]}
            (output / "prepare-stamp.json").write_text(json.dumps({"inputs_sha256": "stamp"}))
            (output / "alignment-prep.jsonl").write_text(json.dumps(packet) + "\n")
            (output / "projected-source-tokens.jsonl").write_text(json.dumps(source) + "\n")
            isolated = output / "verses/40.json"
            scope = chapter_scope("ACT.19")
            self.assertIsNone(cached_preparation(root, scope, "stamp"))
            for content in (json.dumps({**packet, "source_tokens": []}), "{broken"):
                isolated.write_text(content)
                self.assertIsNone(cached_preparation(root, scope, "stamp"))
            # Whitespace/key ordering changes do not make an otherwise equal
            # packet stale; the comparison is its exact JSON value.
            isolated.write_text(json.dumps(packet, indent=2, sort_keys=True))
            self.assertEqual(cached_preparation(root, scope, "stamp"), [packet])

    def test_mapping_hash_is_chapter_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / MAPPING_PATH
            path.parent.mkdir()
            path.write_text(json.dumps({"version": 1, "moves": []}))
            scope = chapter_scope("ACT.19")
            before = preparation_inputs_sha256(root, scope)
            other = {**move(token(1), token(1)), "source_ref": "JHN.1.1", "target_ref": "JHN.1.2"}
            path.write_text(json.dumps({"version": 1, "moves": [other]}))
            self.assertEqual(preparation_inputs_sha256(root, scope), before)
            path.write_text(json.dumps({"version": 1, "moves": [move(token(1), token(1))]}))
            self.assertNotEqual(preparation_inputs_sha256(root, scope), before)
            self.assertEqual(len(chapter_moves(root, "ACT.19")), 1)


if __name__ == "__main__":
    unittest.main()
