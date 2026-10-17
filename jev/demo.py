"""Align ASV John 3:16 with one real run of the service, and print it.

Usage: OPENROUTER_API_KEY=sk-or-v1-... python3 jev/demo.py

The verse's SBLGNT tokens are written out below, so nothing needs to be
fetched or built first. The run costs a cent or two. Words the model was
unsure of are marked with ?.
"""
import json
import sys

import service

TRANSLATION = ('For God so loved the world, that he gave his only begotten Son, '
               'that whosoever believeth on him should not perish, but have eternal life.')
# jev/source-bundle/JHN.3.json, verse JHN.3.16, as build_source_bundle.py writes it.
TOKENS = json.loads('''[
{"project_id": "SBLGNT.JHN.3.16.001", "surface": "Οὕτως", "lemma": "οὕτω(ς)", "gloss": "Thus", "morph": "ADV", "strong": "G3779", "transliteration": "Houtōs", "lemma_transliteration": "houtō(s)", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "adv", "domain": "061", "ln": "61.10", "mandarin": "那么", "role": "adv", "source_xml_id": "n43003016001"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.002", "surface": "γὰρ", "lemma": "γάρ", "gloss": "for", "morph": "CONJ", "strong": "G1063", "transliteration": "gar", "lemma_transliteration": "gar", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "conj", "domain": "089007", "ln": "89.23", "mandarin": "因为", "source_xml_id": "n43003016002"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.003", "surface": "ἠγάπησεν", "lemma": "ἀγαπάω", "gloss": "loved", "morph": "V-AAI-3S", "strong": "G25", "transliteration": "ēgapēsen", "lemma_transliteration": "agapaō", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "verb", "domain": "025003", "frame": "A0:n43003016005 A1:n43003016007", "ln": "25.43", "mandarin": "爱", "mood": "indicative", "number": "singular", "person": "third", "tense": "aorist", "voice": "active", "role": "v", "source_xml_id": "n43003016003"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.004", "surface": "ὁ", "lemma": "ὁ", "gloss": "-", "morph": "T-NSM", "strong": "G3588", "transliteration": "ho", "lemma_transliteration": "ho", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "nominative", "class": "det", "domain": "092004", "gender": "masculine", "ln": "92.24", "number": "singular", "source_xml_id": "n43003016004"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.005", "surface": "θεὸς", "lemma": "θεός", "gloss": "God", "morph": "N-NSM", "strong": "G2316", "transliteration": "theos", "lemma_transliteration": "theos", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "nominative", "class": "noun", "domain": "012001", "gender": "masculine", "ln": "12.1", "mandarin": "神", "number": "singular", "type": "common", "source_xml_id": "n43003016005"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.006", "surface": "τὸν", "lemma": "ὁ", "gloss": "the", "morph": "T-ASM", "strong": "G3588", "transliteration": "ton", "lemma_transliteration": "ho", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "accusative", "class": "det", "domain": "092004", "gender": "masculine", "ln": "92.24", "number": "singular", "source_xml_id": "n43003016006"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.007", "surface": "κόσμον", "lemma": "κόσμος", "gloss": "world", "morph": "N-ASM", "strong": "G2889", "transliteration": "kosmon", "lemma_transliteration": "kosmos", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "accusative", "class": "noun", "domain": "009001", "gender": "masculine", "ln": "9.23", "mandarin": "世人", "number": "singular", "type": "common", "source_xml_id": "n43003016007"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.008", "surface": "ὥστε", "lemma": "ὥστε", "gloss": "that", "morph": "CONJ", "strong": "G5620", "transliteration": "hōste", "lemma_transliteration": "hōste", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "conj", "domain": "089008", "ln": "89.52", "mandarin": "甚至", "source_xml_id": "n43003016008"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.009", "surface": "τὸν", "lemma": "ὁ", "gloss": "the", "morph": "T-ASM", "strong": "G3588", "transliteration": "ton", "lemma_transliteration": "ho", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "accusative", "class": "det", "domain": "092004", "gender": "masculine", "ln": "92.24", "number": "singular", "source_xml_id": "n43003016009"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.010", "surface": "υἱὸν", "lemma": "υἱός", "gloss": "Son", "morph": "N-ASM", "strong": "G5207", "transliteration": "huion", "lemma_transliteration": "huios", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "accusative", "class": "noun", "domain": "012001", "gender": "masculine", "ln": "12.15", "mandarin": "儿子", "number": "singular", "type": "common", "source_xml_id": "n43003016010"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.011", "surface": "τὸν", "lemma": "ὁ", "gloss": "the", "morph": "T-ASM", "strong": "G3588", "transliteration": "ton", "lemma_transliteration": "ho", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "accusative", "class": "det", "domain": "092004", "gender": "masculine", "ln": "92.24", "number": "singular", "source_xml_id": "n43003016011"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.012", "surface": "μονογενῆ", "lemma": "μονογενής", "gloss": "only begotten", "morph": "A-ASM", "strong": "G3439", "transliteration": "monogenē", "lemma_transliteration": "monogenēs", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "accusative", "class": "adj", "domain": "058007", "gender": "masculine", "ln": "58.52", "mandarin": "唯一", "number": "singular", "source_xml_id": "n43003016012"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.013", "surface": "ἔδωκεν", "lemma": "δίδωμι", "gloss": "He gave", "morph": "V-AAI-3S", "strong": "G1325", "transliteration": "edōken", "lemma_transliteration": "didōmi", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "verb", "domain": "037005", "frame": "A0:n43003016005 A1:n43003016010", "ln": "37.98", "mandarin": "赐给", "mood": "indicative", "number": "singular", "person": "third", "subjref": "n43003016005", "tense": "aorist", "voice": "active", "role": "v", "source_xml_id": "n43003016013"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.014", "surface": "ἵνα", "lemma": "ἵνα", "gloss": "so that", "morph": "CONJ", "strong": "G2443", "transliteration": "hina", "lemma_transliteration": "hina", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "conj", "domain": "089009", "ln": "89.59", "mandarin": "叫", "source_xml_id": "n43003016014"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.015", "surface": "πᾶς", "lemma": "πᾶς", "gloss": "everyone", "morph": "A-NSM", "strong": "G3956", "transliteration": "pas", "lemma_transliteration": "pas", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "nominative", "class": "adj", "domain": "059003", "gender": "masculine", "ln": "59.23", "mandarin": "一切", "number": "singular", "source_xml_id": "n43003016015"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.016", "surface": "ὁ", "lemma": "ὁ", "gloss": "-", "morph": "T-NSM", "strong": "G3588", "transliteration": "ho", "lemma_transliteration": "ho", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "nominative", "class": "det", "domain": "092004", "gender": "masculine", "ln": "92.24", "mandarin": "人", "number": "singular", "source_xml_id": "n43003016016"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.017", "surface": "πιστεύων", "lemma": "πιστεύω", "gloss": "believing", "morph": "V-PAP-NSM", "strong": "G4100", "transliteration": "pisteuōn", "lemma_transliteration": "pisteuō", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "nominative", "class": "verb", "domain": "031010", "frame": "A0:n00000000000", "gender": "masculine", "ln": "31.102", "mandarin": "信", "mood": "participle", "number": "singular", "tense": "present", "voice": "active", "role": "v", "source_xml_id": "n43003016017"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.018", "surface": "εἰς", "lemma": "εἰς", "gloss": "in", "morph": "PREP", "strong": "G1519", "transliteration": "eis", "lemma_transliteration": "eis", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "prep", "domain": "090006", "ln": "90.23", "source_xml_id": "n43003016018"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.019", "surface": "αὐτὸν", "lemma": "αὐτός", "gloss": "Him", "morph": "P-ASM", "strong": "G846", "transliteration": "auton", "lemma_transliteration": "autos", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "accusative", "class": "pron", "domain": "092004", "gender": "masculine", "ln": "92.11", "mandarin": "他", "number": "singular", "referent": "n43003016010", "type": "personal", "source_xml_id": "n43003016019"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.020", "surface": "μὴ", "lemma": "μή", "gloss": "not", "morph": "PRT-N", "strong": "G3361", "transliteration": "mē", "lemma_transliteration": "mē", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "adv", "domain": "069002", "ln": "69.3", "mandarin": "不致", "role": "adv", "source_xml_id": "n43003016020"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.021", "surface": "ἀπόληται", "lemma": "ἀπόλλυμι", "gloss": "should perish", "morph": "V-2AMS-3S", "strong": "G622", "transliteration": "apolētai", "lemma_transliteration": "apollymi", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "verb", "domain": "021006", "frame": "A0:n43003016017", "ln": "21.32", "mandarin": "灭亡", "mood": "subjunctive", "number": "singular", "person": "third", "tense": "aorist", "voice": "middle", "role": "v", "source_xml_id": "n43003016021"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.022", "surface": "ἀλλὰ", "lemma": "ἀλλά", "gloss": "but", "morph": "CONJ", "strong": "G235", "transliteration": "alla", "lemma_transliteration": "alla", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "conj", "domain": "089023", "ln": "89.125", "mandarin": "反", "source_xml_id": "n43003016022"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.023", "surface": "ἔχῃ", "lemma": "ἔχω", "gloss": "should have", "morph": "V-PAS-3S", "strong": "G2192", "transliteration": "echēi", "lemma_transliteration": "echō", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"class": "verb", "domain": "090013", "frame": "A0:n43003016017 A1:n43003016024", "ln": "90.65", "mandarin": "得", "mood": "subjunctive", "number": "singular", "person": "third", "subjref": "n43003016017", "tense": "present", "voice": "active", "role": "v", "source_xml_id": "n43003016023"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.024", "surface": "ζωὴν", "lemma": "ζωή", "gloss": "life", "morph": "N-ASF", "strong": "G2222", "transliteration": "zōēn", "lemma_transliteration": "zōē", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "accusative", "class": "noun", "domain": "023007", "gender": "feminine", "ln": "23.88", "mandarin": "生命", "number": "singular", "type": "common", "source_xml_id": "n43003016024"}, "origin": "base"},
{"project_id": "SBLGNT.JHN.3.16.025", "surface": "αἰώνιον", "lemma": "αἰώνιος", "gloss": "eternal", "morph": "A-ASF", "strong": "G166", "transliteration": "aiōnion", "lemma_transliteration": "aiōnios", "native_ref": "JHN.3.16", "oshb_word": null, "annotations": {"case": "accusative", "class": "adj", "domain": "067005", "gender": "feminine", "ln": "67.96", "mandarin": "永恒", "number": "singular", "source_xml_id": "n43003016025"}, "origin": "base"}
]''')


def main():
    result = service.align('JHN.3.16', TRANSLATION, TOKENS)
    reader, flagged = result['reader'], set(result['flagged'])
    units = reader['units']
    order = []
    for part in reader['english'][0]['parts']:
        if isinstance(part, dict) and 'unit' in part and part['unit'] not in order:
            order.append(part['unit'])
    order += [uid for uid in units if uid not in order]
    for uid in order:
        unit = units[uid]
        mark = '?' if any(key.startswith(uid + ':') for key in flagged) else ' '
        english = ' '.join(t['text'] for t in unit['english']) or '(not translated)'
        source = ' '.join(f"{t['text']} ({t['attrs'].get('x-translit', '')})" for t in unit['source']) or '(added)'
        print(f'{mark} {english:30} {source}')
    if result['verse_flagged']:
        print('? The model was unsure of the verse as a whole.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f'{type(error).__name__}: {error}')
