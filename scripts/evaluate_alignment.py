#!/usr/bin/env python3
import argparse
import json

from alignment_research.evaluation import evaluate

parser = argparse.ArgumentParser(description="Evaluate the accepted alignment")
parser.add_argument("chapter")
args = parser.parse_args()

print(json.dumps(
    evaluate(chapter=args.chapter),
    indent=2, sort_keys=True,
))
