"""Retain translation punctuation separately from canonical alignment word IDs."""
import json
from pathlib import Path
import re
import unicodedata

ROOT=Path(__file__).resolve().parent.parent
WORD_RE=re.compile(r"[^\W_]+(?:[’'][^\W_]+)*",re.UNICODE)


def punctuated_context(text,target):
    matches=list(WORD_RE.finditer(text))
    if [m.group() for m in matches]!=list(target.values()):
        raise ValueError('Punctuated translation does not match the exact target words and order')
    affixes={tid:{'before':'','after':'','start':match.start(),'end':match.end()}
             for tid,match in zip(target,matches)}
    ids=list(target)
    if matches:affixes[ids[0]]['before']=text[:matches[0].start()]
    for index,match in enumerate(matches):
        end=matches[index+1].start() if index+1<len(matches) else len(text)
        gap=text[match.end():end]
        if index+1==len(matches):
            affixes[ids[index]]['after']=gap
            continue
        # Keep punctuation in its original stream order. Opening brackets and
        # quotation marks prefix the following word; closing marks and a
        # trailing possessive apostrophe suffix the preceding word. Whitespace
        # identifies an opening straight quote without turning it into a word.
        boundary=len(gap)
        for offset,char in enumerate(gap):
            opening=unicodedata.category(char) in {'Ps','Pi'}
            straight_open=(char in {'"',"'"} and offset>0 and
                           (gap[offset-1].isspace() or gap[offset-1] in ',:;—–([{'))
            if opening or straight_open:
                boundary=offset
                break
        affixes[ids[index]]['after']=gap[:boundary]
        affixes[ids[index+1]]['before']=gap[boundary:]
    return {'target_text':text,'target_word_spans':affixes,
            'target_text_guidance':'The complete translation includes punctuation and possessives. Word IDs identify lexical tokens; punctuation and inter-word apostrophes remain part of the surrounding text, not independent alignment tokens. Source and English order may differ.'}


def with_translation_context(state):
    target=state['target_tokens']
    if state.get('target_text') is not None:
        return {**state,**punctuated_context(state['target_text'],target)}
    ref=state['ref'];chapter=ref.rsplit('.',1)[0]
    path=ROOT/'data/normalized'/chapter/'asv-target-tokens.jsonl'
    row=next((json.loads(line) for line in path.read_text().splitlines() if json.loads(line)['ref']==ref),None)
    if row is None:raise ValueError(f'No punctuated translation for {ref}')
    if {t['id']:t['text'] for t in row['tokens']}!=target:
        raise ValueError('Pinned translation differs from supplied target words; provide target_text explicitly')
    return {**state,**punctuated_context(row['text'],target)}
