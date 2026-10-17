"""Shared experiment I/O and rule-text loading; no model judgment or driver.

Parsing a complete contract locally does not send it to the model. Controllers
must explicitly choose bounded rule candidates before preparing questions.
"""
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parent
CONTRACT=ROOT.parent/'config/alignment-instructions-greek.md'

def read(p): return json.loads(p.read_text())

def write(p,d): p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')

def compact(d): return json.dumps(d,ensure_ascii=False,separators=(',',':')).encode()

def registry(contract=CONTRACT):
    text=contract.read_text()
    rules={int(m[1]):m[0] for m in re.finditer(r'^(\d+)\. .+$',text,re.M)}
    if sorted(rules)!=list(range(1,max(rules)+1)): raise RuntimeError('Rule sequence incomplete.')
    return {'contract':re.search(r'`((?:nt-greek|ot-hebrew)-verse-v\d+)`',text)[1],
            'sha256':hashlib.sha256(contract.read_bytes()).hexdigest(),
            'preamble':text.split('\n1. ')[0], 'rules':rules}

from request_runner import Runner
