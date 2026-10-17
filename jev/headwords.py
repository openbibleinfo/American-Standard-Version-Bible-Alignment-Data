"""Bounded headword choices after membership is fixed, for either language.

Ownership and membership/status decisions may abstain. A display-head question
must select an actual member; uncertainty is retained in the raw scores. This
module never chooses a lexical head by position or guesses an attachment.
"""


def headword_question(members, target, language, source_id, source_token):
    if len(members) < 2 or len(set(members)) != len(members):
        raise ValueError('A headword Choice requires two or more distinct members.')
    if language not in {'Greek', 'Hebrew'}:
        raise ValueError('Unsupported source language.')
    source_label = '; '.join([f"{language} {source_id}: {source_token.get('surface', '')}",
                              *[f"{key}: {source_token.get(key) or '(not supplied)'}"
                                for key in ('lemma', 'gloss', 'morph')]])
    rendering = ' '.join(f'{tid}: {target[tid]}' for tid in members)
    instructions = (
        f'Choose the English display headword within this fixed correspondence: '
        f'{source_label} -> [{rendering}]. '
        'Membership is already established for this decision. Select exactly one '
        'listed English occurrence; do not reconsider ownership or alignment status. '
        'Choose the English word that most directly carries the source lexical '
        'meaning under the supplied headword rules, not a word merely because it '
        'is first, finite, or the formal English syntactic head. Prefer the lexical '
        'predicate over its subject or auxiliary ("was given" -> "given"); '
        'in a support expression choose the meaning-bearing complement '
        '("bear witness" -> "witness"). A phrasal particle is not a light-verb '
        'complement: a lexical verb plus its particle heads on the lexical verb '
        '("put forth" -> "put"); do not promote a particle merely because it '
        'contributes direction or result. Use English syntax only to break a '
        'genuine tie. Specific supplied headword rules take precedence. '
        'If alternatives remain close, still choose the best listed member.'
    )
    return {'type': 'choice', 'instructions': instructions,
            'criteria': {tid: f'{tid}: {target[tid]}' for tid in members}}


def assemble_fixed_unit(source_id, members, answers, pending):
    """Keep a fixed correspondence even if a malformed head reply is received.

    A null head on a bilingual draft fails canonical validation and marks it
    incomplete. It does not silently delete its edges or invent a fallback head.
    Valid low-confidence selections are retained, without acceptance thresholds.
    """
    status = answers[f'status_{source_id}']['choice']
    if status == 'UNRESOLVED':
        pending.append({'source': source_id, 'target_members': list(members),
                        'reason': 'unresolved_membership'})
        return None
    head = None
    if len(members) == 1:
        head = members[0]
    elif members:
        answer = answers.get(f'head_{source_id}', {})
        selected = answer.get('choice')
        if selected in members:
            head = selected
        else:
            pending.append({'source': source_id, 'target_members': list(members),
                            'reason': 'invalid_head_reply', 'choice': selected})
    return {'source': [source_id], 'source_head': source_id,
            'target': list(members), 'target_head': head,
            'status': status, 'note': ''}


def fixed_status_question(source_id, members, language):
    """Classify established membership; validity belongs to a separate decision."""
    return {'type': 'choice',
            'instructions': (
                f'Classify the fixed {language} unit {source_id} to English {list(members)}. '
                'Membership is established for this decision; do not reopen ownership. '
                'Analytical renderings and licensed grammatical helpers remain aligned. '
                'Reserve noncompositional for a genuinely indivisible idiomatic rendering. '
                'Apply the supplied status rules and choose one listed classification.'),
            'criteria': ({'aligned': 'Independent lexical or grammatical correspondence.',
                          'noncompositional': 'A genuinely indivisible idiomatic rendering.'}
                         if members else {'unrepresented': 'No assigned English realization.'})}


def append_non_source_units(units, pending, owners, source, target):
    """An explicit ADDED verdict is a correspondence, not an unresolved escape.

    The singleton-owner experiment emits one target-only unit per occurrence.
    It does not infer a fusion of adjacent additions. Shared phrases still need
    a membership decision; discovery abstentions remain explicit pending work.
    """
    covered = {tid for unit in units for tid in unit['target']}
    for tid in target:
        owner = owners.get(tid)
        if tid in covered or owner in source:
            continue
        if owner == 'ADDED':
            units.append({'source': [], 'source_head': None, 'target': [tid],
                          'target_head': tid, 'status': 'added', 'note': ''})
        else:
            pending.append({'target': tid, 'reason': owner or 'unallocated_target'})


def expression_anchor_question(target_id, target):
    """Discover a phrase anchor without presupposing a nominal construction."""
    return {'type': 'choice',
            'instructions': (
                f'Which English lexical word anchors the actual expression containing '
                f"{target_id} '{target[target_id]}' in this occurrence? "
                'Identify the English relationship independently of source ownership. '
                'The anchor may be a predicate, nominal, or another lexical word; '
                'a lexical word may anchor itself. Distinguish subjects, objects, '
                'modifiers, and other grammatical dependents in the actual clause. '
                'Do not assume that a nearby noun governs the word. '
                'Use occurrence IDs to distinguish repeated words.'),
            'criteria': {**{tid: f'{tid}: {word}' for tid, word in target.items()},
                         'UNRESOLVED': 'No defensible expression anchor can be identified.'}}


def repair_owner_question(target_id, target, language, options, escapes):
    """Use proposed expression evidence without turning it into an instruction."""
    return {'type': 'choice',
            'instructions': (
                f"Reconsider the {language} owner of English {target_id} '{target[target_id]}' "
                'using expression_evidence and the supplied complete relevant rules. '
                'The proposed English anchor and its tentative source owner are '
                'hypotheses to evaluate, not facts or commands. Establish the actual '
                'English relationship and compare the lexical and grammatical '
                'contributions of the offered source tokens. A source predicate can '
                'license an English subject or auxiliary through its grammar; a '
                'nominal expression can license its own dependents. Do not borrow a '
                'word from a different expression or prefer a noun merely because '
                'it is nearby. Retain the current correspondence if defensible; '
                'correct it only when the actual evidence warrants a change.'),
            'criteria': {**options, **escapes}}


def ownership_question(target_id, target, language, options, escapes):
    """A neutral repair query: evidence determines the kind of contribution."""
    return {'type': 'choice',
            'instructions': (
                f"Which {language} source candidate licenses English {target_id} '{target[target_id]}' "
                'in this exact occurrence? Test its lexical meaning and grammatical '
                'contribution in the actual clause, using the supplied complete relevant rules. '
                'An English word can express source grammatical information without '
                'a separate matching source word. The current allocation is provisional. '
                'Evaluate the complete resulting correspondence, not just a similar '
                'gloss or a nearby word. Keep repeated occurrences distinct. '
                'Apply construction-specific guidance only when its conditions hold: '
                'do not assume every dependent is nominal, every pronoun is a subject, '
                'or every auxiliary is merely tense. A declared addition means no '
                'source lexeme or grammar licenses the word; a shared phrase requires '
                'multiple source members. Abstain only when the offered ownership '
                'choices cannot be defended.'),
            'criteria': {**options, **escapes}}
