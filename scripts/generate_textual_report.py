#!/usr/bin/env python3
import argparse
import json

from alignment_research.textual_report import generate_textual_report


parser = argparse.ArgumentParser(
    description="Report the ASV-evidenced Greek projection against SBLGNT"
)
parser.add_argument("chapter")
args = parser.parse_args()

summary = generate_textual_report(chapter=args.chapter)
print(json.dumps({
    "chapter": summary["chapter"],
    "apparatus_units": summary["apparatus_units"],
    "asv_evidenced_base": summary["asv_evidenced_base"],
    "asv_evidenced_non_base": summary["asv_evidenced_non_base"],
    "underdetermined_base_fallback": summary["underdetermined_base_fallback"],
    "difference_refs": summary["difference_refs"],
}, ensure_ascii=False, indent=2, sort_keys=True))
