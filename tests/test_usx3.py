import unittest
import xml.etree.ElementTree as ET

from alignment_research.usx3 import (
    USX_VERSION,
    add_milestones,
    block_text,
    chapter_texts,
    chapter_verses,
    strip_alignment,
    verify_english_text,
    verify_milestones,
)


def parse(body: str) -> ET.Element:
    return ET.fromstring(
        '<usx version="2.0"><book code="PSA" style="id">ASV</book>'
        + body
        + "</usx>"
    )


def serialize(document: ET.Element) -> str:
    """The document as written, one root block per line."""
    return "\n".join(
        line.strip() for line in
        ET.tostring(document, encoding="unicode").splitlines()
    ).strip()


class MilestoneTests(unittest.TestCase):
    def test_a_verse_starts_the_block_it_opens_and_ends_the_block_it_closes(self):
        document = add_milestones(parse(
            '<chapter number="1" style="c"/>'
            '<para style="q1"><verse number="1" style="v"/>First line,</para>'
            '<para style="q1">and its second line.</para>'
        ))
        self.assertEqual(document.attrib["version"], USX_VERSION)
        self.assertEqual(serialize(document), (
            '<usx version="3.0">\n'
            '<book code="PSA" style="id">ASV</book>\n'
            '<chapter number="1" style="c" sid="PSA 1" />\n'
            '<para style="q1"><verse number="1" style="v" sid="PSA 1:1" />'
            'First line,</para>\n'
            '<para style="q1">and its second line.<verse eid="PSA 1:1" /></para>\n'
            '<chapter eid="PSA 1" />\n'
            '</usx>'
        ))

    def test_a_verse_starts_in_the_heading_or_break_that_introduces_it(self):
        document = add_milestones(parse(
            '<chapter number="3" style="c"/>'
            '<para style="s1">Morning prayer.</para>'
            '<para style="d">A Psalm of David.</para>'
            '<para style="q1"><verse number="1" style="v"/>How are they increased!</para>'
            '<para style="b"/>'
            '<para style="qa">Aleph.</para>'
            '<para style="q1"><verse number="2" style="v"/>Many there are that say,</para>'
        ))
        self.assertEqual(serialize(document), (
            '<usx version="3.0">\n'
            '<book code="PSA" style="id">ASV</book>\n'
            '<chapter number="3" style="c" sid="PSA 3" />\n'
            '<para style="s1"><verse number="1" style="v" sid="PSA 3:1" />'
            'Morning prayer.</para>\n'
            '<para style="d">A Psalm of David.</para>\n'
            '<para style="q1">How are they increased!<verse eid="PSA 3:1" /></para>\n'
            '<para style="b"><verse number="2" style="v" sid="PSA 3:2" /></para>\n'
            '<para style="qa">Aleph.</para>\n'
            '<para style="q1">Many there are that say,<verse eid="PSA 3:2" /></para>\n'
            '<chapter eid="PSA 3" />\n'
            '</usx>'
        ))

    def test_a_verse_that_is_its_own_block_is_not_absorbed_by_the_next(self):
        # The original-language projection numbers a superscription with
        # x-ref instead of a verse marker; verse 1 must not swallow it.
        document = add_milestones(parse(
            '<chapter number="3" style="c"/>'
            '<para style="d" x-ref="PSA.3.0">A Psalm of David.</para>'
            '<para style="p"><verse number="1" style="v"/>How are they increased!</para>'
        ))
        self.assertEqual(serialize(document), (
            '<usx version="3.0">\n'
            '<book code="PSA" style="id">ASV</book>\n'
            '<chapter number="3" style="c" sid="PSA 3" />\n'
            '<para style="d" x-ref="PSA.3.0">A Psalm of David.</para>\n'
            '<para style="p"><verse number="1" style="v" sid="PSA 3:1" />'
            'How are they increased!<verse eid="PSA 3:1" /></para>\n'
            '<chapter eid="PSA 3" />\n'
            '</usx>'
        ))

    def test_verses_sharing_a_paragraph_keep_their_milestones_in_order(self):
        document = add_milestones(parse(
            '<chapter number="1" style="c"/>'
            '<para style="p"><verse number="1" style="v"/>One. '
            '<verse number="2" style="v"/>Two. '
            '<verse number="3" style="v"/>Three.</para>'
        ))
        para = document[2]
        self.assertEqual(
            [
                (child.tag, child.attrib.get("sid") or child.attrib.get("eid"))
                for child in para
            ],
            [
                ("verse", "PSA 1:1"),
                ("verse", "PSA 1:1"),
                ("verse", "PSA 1:2"),
                ("verse", "PSA 1:2"),
                ("verse", "PSA 1:3"),
                ("verse", "PSA 1:3"),
            ],
        )
        self.assertEqual(block_text(para), "One. Two. Three.")
        self.assertEqual(verify_milestones(document), 3)

    def test_a_verse_start_precedes_the_footnote_that_stood_before_it(self):
        document = add_milestones(parse(
            '<chapter number="1" style="c"/>'
            '<para style="q1"><note caller="-" style="f">'
            '<char style="ft">Or, Happy</char></note>'
            '<verse number="1" style="v"/>Blessed is the man,</para>'
        ))
        para = document[2]
        self.assertEqual(para[0].attrib["sid"], "PSA 1:1")
        self.assertEqual(para[1].tag, "note")
        self.assertEqual(block_text(para), "Or, HappyBlessed is the man,")

    def test_a_chapter_ends_after_its_last_verse_and_before_the_next(self):
        document = add_milestones(parse(
            '<chapter number="1" style="c"/>'
            '<para style="p"><verse number="1" style="v"/>One.</para>'
            '<chapter number="2" style="c"/>'
            '<para style="s1">A heading.</para>'
            '<para style="p"><verse number="1" style="v"/>Two.</para>'
        ))
        self.assertEqual(
            [
                element.attrib.get("sid") or element.attrib.get("eid")
                for element in document if element.tag == "chapter"
            ],
            ["PSA 1", "PSA 1", "PSA 2", "PSA 2"],
        )
        self.assertEqual(block_text(document[2]), "One.")
        self.assertEqual(document[2][-1].attrib["eid"], "PSA 1:1")
        self.assertEqual(document[5][0].attrib["sid"], "PSA 2:1")
        self.assertEqual(verify_milestones(document), 2)

    def test_milestone_verification_rejects_an_unbalanced_document(self):
        document = add_milestones(parse(
            '<chapter number="1" style="c"/>'
            '<para style="p"><verse number="1" style="v"/>One.</para>'
        ))
        para = document[2]
        para.remove(para[-1])
        with self.assertRaises(ValueError):
            verify_milestones(document)


class ProjectionTests(unittest.TestCase):
    aligned = (
        '<chapter number="1" style="c"/>'
        '<para style="q1"><verse number="1" style="v" x-unrepresented-source="כִּי"/>'
        '<char style="w" x-id="t001" x-align="a001" lemma="l" strong="H1">Blessed</char> '
        '<char style="w" x-id="t002" x-align="a001">is</char> '
        '<char style="add"><char style="w" x-id="t003" x-align="a002">he</char></char>,</para>'
    )
    plain = (
        '<chapter number="1" style="c"/>'
        '<para style="q1"><verse number="1" style="v"/>'
        'Blessed is <char style="add">he</char>,</para>'
    )

    def test_stripping_alignment_restores_the_published_text(self):
        stripped = strip_alignment(add_milestones(parse(self.aligned)))
        expected = add_milestones(parse(self.plain))
        self.assertEqual(
            ET.tostring(stripped, encoding="unicode"),
            ET.tostring(expected, encoding="unicode"),
        )

    def test_both_projections_carry_the_source_text_and_verses(self):
        source = parse(self.plain)
        expected = chapter_texts(source)
        self.assertEqual(expected["1"], [("q1", "Blessed is he,")])
        self.assertEqual(chapter_verses(source), {"1": ["1"]})
        for document in (
            add_milestones(parse(self.aligned)),
            strip_alignment(add_milestones(parse(self.aligned))),
        ):
            self.assertEqual(verify_english_text(source, document, "test", ["1"]), 1)

    def test_verification_catches_changed_text(self):
        source = parse(self.plain)
        document = add_milestones(parse(self.aligned))
        next(
            element for element in document.iter("char")
            if element.attrib.get("style") == "w"
        ).text = "Happy"
        with self.assertRaises(ValueError):
            verify_english_text(source, document, "test", ["1"])


if __name__ == "__main__":
    unittest.main()
