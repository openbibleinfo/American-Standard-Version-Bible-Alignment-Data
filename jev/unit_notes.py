"""Bounded required unit notes; this module never repairs accepted memberships.

Policies declare note_requirements: [{id, kind, condition, record_paths}].
Kinds are allocation_alternative, lexical_evidence, variant_provenance,
textual_divergence, source_omission, verse_boundary, annotation_set_aside,
and lexeme_mismatch. No rule-text keyword routing is performed.

Metadata is read from state plus required_context. Variant records need unit_id,
selected_reading/operational_reading, remaining_readings/compatible_readings and
reason_code/fallback_reason/absence_reason, or an actual selected/underdetermined
status with fallback_used. A separate bounded absence explanation never invents
an upstream documentary reason. Reading IDs are preserved verbatim;
reading objects can contain id/text/source_ids only. Hebrew textual_decision
also supports an explicitly declared no_variant status; actual fixed occurrences
and a bounded divergence reason explain this branch.
source_omission needs resolution and origin_ref for parallel replacement;
verse_boundary needs other_ref. External evidence.textual_variants uses the same
variant schema. Lexical evidence records need id, kind (direct/neighbor/analogy/
absence), and reference/lexeme; absence records declare direct and neighboring
support, and an explicit analogy_id when both are absent. An annotation needs
id plus actual text or a field/value pair, never an arbitrary prewritten note.
Multiple records are independently judged; malformed or ambiguous mandatory
provenance keeps the unit pending. Exact captured fields and bounded reasons
are serialized as JSON inside the unit note, with no generated prose.
"""
from copy import deepcopy
import json

from note_batching import prepare_note_jobs


KINDS = {'allocation_alternative', 'lexical_evidence', 'variant_provenance',
         'textual_divergence', 'source_omission', 'verse_boundary',
         'annotation_set_aside', 'lexeme_mismatch'}
REASONS = {
    'LEXICAL_CONTRIBUTION': 'Actual lexeme contribution supports the reading.',
    'GRAMMATICAL_LICENSE': 'An actual grammatical construction licenses it.',
    'AVAILABLE_OWNER': 'An occurrence remains available for this role.',
    'SPENT_OWNER': 'A putative owner is already realized in another role.',
    'INDEPENDENT_REALIZATION': 'The source/target contributions are recoverable separately.',
    'JOINT_REALIZATION': 'The rendering jointly realizes the source contributors.',
    'SOURCE_GOVERNOR': 'The actual source governor owns this rendering.',
    'PREPARED_READING': 'The fixed prepared reading explains this disposition.',
    'EXPLICIT_ANALOGY': 'Explicitly supplied analogy supports the reading.',
    'CONTEXTUAL_ROLES': 'Actual clause roles support the selected allocation.',
    'UNRESOLVED': 'Cannot establish a defensible reason from supplied evidence.'}


def _question(instructions, criteria):
    return {'type': 'choice', 'instructions': instructions, 'criteria': criteria}


def _pending(engine, uid, reason, **details):
    engine.pending.append({'unit': uid, 'stage': 'required note', 'reason': reason, **details})


def _value(answers, qid):
    return answers.get(qid, {}).get('choice', 'UNRESOLVED')


def _dispatch(engine, label, jobs):
    """Batch this ready frontier by complete rules and common fixed partition."""
    calls, failures = prepare_note_jobs(engine, label, jobs)
    for uid, error in failures.items():
        _pending(engine, uid, 'required_note_context_unavailable', detail=error)
    return engine.dispatch(calls), set(failures)


def _reading(value):
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict) and isinstance(value.get('id'), str) and value['id'].strip():
        return {k: deepcopy(value[k]) for k in ('id', 'text', 'source_ids') if k in value}
    raise ValueError('Missing or malformed reading identity.')


def _records_at(available, path, source_ids):
    parts = path.split('.')
    if '*' in parts:
        if parts[:2] != ['source_tokens', '*'] or parts.count('*') != 1:
            raise ValueError('Unsupported note metadata wildcard.')
        result = []
        for sid in source_ids:
            result.extend(_records_at(available, '.'.join(['source_tokens', sid, *parts[2:]]), []))
        return result
    node = available
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return []
        node = node[part]
    if isinstance(node, list):
        return [(f'{path}[{i}]', item) for i, item in enumerate(node)]
    return [(path, node)] if node is not None else []


def _normalize_record(kind, location, raw):
    if not isinstance(raw, dict):
        raise ValueError('Required note record is not an object.')
    out = {'location': location}
    for key in ('id', 'unit_id', 'status', 'resolution', 'origin_ref', 'other_ref',
                'source_ids', 'target_ids', 'base_source_ids', 'projected_source_ids',
                'reason_code', 'fallback_reason', 'absence_reason', 'fallback_used', 'origin', 'kind',
                'reference', 'lexeme', 'field', 'value', 'text', 'direct_support',
                'neighboring_support', 'analogy_support', 'analogy_id', 'annotation_id'):
        if key in raw:
            out[key] = deepcopy(raw[key])
    for key in ('selected_reading', 'operational_reading', 'source_reading', 'target_reading'):
        if key in raw and raw[key] is not None:
            out[key] = _reading(raw[key])
    for key in ('remaining_readings', 'compatible_readings'):
        if key in raw:
            if not isinstance(raw[key], list) or not raw[key]:
                raise ValueError('Remaining readings must be a nonempty list.')
            out[key] = [_reading(v) for v in raw[key]]
    # Provider source annotations have no independent annotation IDs; their exact
    # path is their durable identifier. Do not treat an arbitrary note as evidence.
    if kind == 'annotation_set_aside' and '.annotations' in location and 'id' not in out:
        out['id'] = location
        out['fields'] = {k: v for k, v in raw.items()
                         if isinstance(v, (str, bool, int, float)) and k != 'note'}
    for key in ('id', 'unit_id', 'origin_ref', 'other_ref', 'reason_code',
                'fallback_reason', 'absence_reason', 'reference', 'lexeme', 'text'):
        if key in out and (not isinstance(out[key], str) or not out[key].strip()):
            raise ValueError(f'Malformed metadata field {key}.')
    return out


def _validate_record(kind, record):
    def has(*keys):
        return any(key in record and record[key] not in (None, '', [], {}) for key in keys)
    if kind in {'variant_provenance', 'textual_divergence'}:
        if kind == 'textual_divergence' and record.get('status') in {'no_variant', 'unmarked_no_variant', 'none'}:
            # The bounded reason plus captured fixed occurrences explains an
            # explicitly unmarked slot without inventing an alternate source reading.
            pass
        elif not (has('unit_id') and has('selected_reading', 'operational_reading') and
                  has('remaining_readings', 'compatible_readings') and
                  (has('reason_code', 'fallback_reason', 'absence_reason') or
                   (record.get('status') in {'selected', 'underdetermined'} and isinstance(record.get('fallback_used'), bool)))):
            raise ValueError('Variant lacks unit, selected/remaining readings or recorded reason.')
    elif kind == 'source_omission':
        if record.get('resolution') not in {'parallel_source', 'parallel_replacement', 'no_replacement', 'omitted_without_replacement'}:
            raise ValueError('Unknown or missing omission resolution.')
        if record['resolution'] in {'parallel_source', 'parallel_replacement'} and not has('origin_ref'):
            raise ValueError('Parallel replacement lacks origin reference.')
    elif kind == 'verse_boundary':
        if not has('other_ref'):
            raise ValueError('Boundary note lacks counterpart reference.')
    elif kind == 'annotation_set_aside':
        if not (has('id', 'annotation_id') and (has('text', 'fields') or ('field' in record and 'value' in record))):
            raise ValueError('Set-aside annotation lacks identity or actual evidence.')
    elif kind == 'lexical_evidence':
        if not (has('id') and record.get('kind') in {'direct', 'neighbor', 'analogy', 'absence'}):
            raise ValueError('Attestation/analogy lacks identity or declared kind.')
        if record['kind'] == 'absence':
            if record.get('direct_support') not in {'absent', 'present'} or record.get('neighboring_support') not in {'absent', 'present'}:
                raise ValueError('Missing direct/neighboring attestation status.')
            if record['direct_support'] == record['neighboring_support'] == 'absent' and not has('analogy_id') and record.get('analogy_support') != 'absent':
                raise ValueError('Absent direct/neighboring support requires actual analogy identity or explicit absence.')
        elif not has('reference', 'lexeme'):
            raise ValueError('Attestation/analogy lacks reference or lexical identity.')


def _available(engine):
    def merge(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                merge(a[k], v)
            else:
                a[k] = deepcopy(v)
        return a
    return merge(deepcopy(engine.state), getattr(engine, 'required_context', {}))


def _context(engine, unit, answers, pack, groups, requirement):
    uid = unit['id']
    context = {'policies': pack, 'fixed_unit': deepcopy(unit),
               'selected_status': _value(answers, f'status_{uid}'),
               'selected_heads': {s: _value(answers, f'{s}_head_{uid}') for s in ('source', 'target')},
               'selected_occurrences': {
                   'source': {sid: {k: deepcopy(engine.source[sid][k]) for k in ('surface', 'lemma', 'morph', 'project_id')
                                    if k in engine.source[sid]} for sid in unit['source']},
                   'target': {tid: deepcopy(engine.target[tid]) for tid in unit['target']}},
               'note_requirement': requirement,
               'fixed_partition': deepcopy(groups),
               'instruction': 'Document actual fixed decisions only. Do not change membership, status or heads.'}
    if hasattr(engine, 'scoped_evidence'):
        context['prior_owner_evidence'] = engine.scoped_evidence(unit['target'])
    return context


def required_notes(engine, groups, final_answers, unit_packs, blocked):
    """Return uid->deterministic note; failures add pending and blocked unit IDs.

    Integrate after fixed final decisions, then concatenate these notes with the
    separate display-head alternative notes. Caller must withhold any unit newly
    added to blocked. No questions here rewrite any edge, status or head.
    """
    base = engine.catalog.pack(['explanation.alternatives'], engine.language)
    available = _available(engine)
    jobs, tasks = [], {}
    for unit in groups:
        uid = unit['id']
        if uid in blocked or _value(final_answers, f'valid_{uid}') != 'YES':
            continue
        src, ts = unit['source'], unit['target']
        status = _value(final_answers, f'status_{uid}')
        valid_status = (status in {'aligned', 'noncompositional'} if src and ts else
                        status == ('unrepresented' if src else 'added'))
        valid_heads = all(
            _value(final_answers, f'{side}_head_{uid}') in ids if ids else
            _value(final_answers, f'{side}_head_{uid}') is None
            for side, ids in [('source', src), ('target', ts)]
            if not (side == 'target' and status == 'added' and
                    _value(final_answers, f'{side}_head_{uid}') is None))
        if (not (src or ts) or not valid_status or not valid_heads or
            not set(src) <= set(engine.source) or not set(ts) <= set(engine.target)):
            _pending(engine, uid, 'invalid_fixed_decision_for_required_note')
            blocked.add(uid)
            continue
        pack = {**base, **deepcopy(unit_packs.get(uid, {}))}
        requirements = []
        for pid, policy in pack.items():
            # pack preserves authored metadata; catalog fallback supports pre-merge
            # runtime pack implementations without keyword interpretation.
            authored = policy.get('note_requirements', engine.catalog.policies.get(pid, {}).get('note_requirements', []))
            for req in authored:
                if req.get('kind') not in KINDS or not req.get('id') or not req.get('condition'):
                    raise ValueError('Malformed authored note requirement.')
                key = (pid, req['id'])
                if key not in {r[0] for r in requirements}:
                    requirements.append((key, req))
        if not requirements:
            _pending(engine, uid, 'missing_required_note_configuration')
            blocked.add(uid)
            continue
        for i, ((pid, rid), req) in enumerate(requirements):
            token = f'{uid}_{i:02d}'
            context = _context(engine, unit, final_answers, pack, groups, req)
            records, record_errors = [], []
            for path in req.get('record_paths', []):
                for location, raw in _records_at(available, path, unit['source']):
                    try:
                        records.append(_normalize_record(req['kind'], location, raw))
                    except ValueError as error:
                        record_errors.append(str(error))
            context['note_evidence_records'] = records
            context['note_metadata_errors'] = record_errors
            tasks[token] = {'uid': uid, 'unit': unit, 'req': req, 'context': context,
                            'records': records, 'errors': record_errors}
            jobs.append((uid, {f'note_needed_{token}': _question(
                f'Does this actual fixed unit satisfy the authored note condition: {req["condition"]}? '
                'This includes singleton and empty-side units. Distinguish actual allocation/status alternatives '
                'from display-head ties handled separately. Unknown required evidence is UNRESOLVED, '
                'never evidence that no note is needed. YES requires an actually different membership, '
                'ownership or status when the requirement concerns allocation. Restating the selected '
                'source with the same target extent and status is not a competing allocation.',
                {'YES': 'The condition applies and its note is required.',
                 'NO': 'The condition does not apply to this fixed unit.',
                 'UNRESOLVED': 'Cannot establish whether this note is required.'})}, context))
    diagnostics, failed = _dispatch(engine, 'required-note-diagnostic', jobs)
    blocked.update(failed)
    active = {token: t for token, t in tasks.items()
              if t['uid'] not in blocked and _value(diagnostics, f'note_needed_{token}') == 'YES'}
    for token, t in tasks.items():
        if t['uid'] not in blocked and _value(diagnostics, f'note_needed_{token}') == 'UNRESOLVED':
            _pending(engine, t['uid'], 'unresolved_required_note_condition', requirement=t['req']['id'])
            blocked.add(t['uid'])
    frames = [(t['uid'], 'required note '+token, t['context']['policies'], t['unit']['source'],
               {**t['context'], 'note_condition_answer': diagnostics[f'note_needed_{token}']})
              for token, t in active.items() if t['uid'] not in blocked]
    ready = engine.evidence_ready_many(frames)
    for token, t in active.items():
        if not ready.get((t['uid'], 'required note '+token), False):
            blocked.add(t['uid'])
    # Nomination of one actual competing correspondence is an explanation decision,
    # not a new alignment pass. Its extent and contributors are determined later.
    jobs = []
    for token, t in active.items():
        if t['uid'] in blocked:
            continue
        kind, uid, context = t['req']['kind'], t['uid'], t['context']
        if kind == 'allocation_alternative':
            options = {f'source:{sid}': f'Competing source {sid}: {data}. A currently chosen source counts only with a different target extent.' for sid, data in engine.source.items()}
            options.update({'ADDED': 'Competing target-only correspondence.',
                            'SHARED': 'Competing correspondence jointly licensed by several actual sources.',
                            'UNRESOLVED': 'Cannot identify a defensible competing correspondence.'})
            if t['unit']['source'] and t['unit']['target']:
                options['STATUS_ONLY'] = 'Same membership, competing aligned/noncompositional classification.'
            jobs.append((uid, {f'note_owner_{token}': _question(
                f'For selected correspondence {t["unit"]} with status {context["selected_status"]}, '
                'identify a materially different competing correspondence. '
                'This records an alternative only; it never discards chosen valid members.', options)}, context))
        elif kind == 'lexeme_mismatch':
            jobs.append((uid, {f'note_reason_{token}': _question(
                'Which supplied-evidence reason explains why the selected target anchor is a construction anchor '
                'rather than a direct lexeme realization?', REASONS)}, context))
        else:
            if t['errors'] or not t['records']:
                _pending(engine, uid, 'required_note_provenance_missing', requirement=t['req']['id'], errors=t['errors'])
                blocked.add(uid)
                continue
            seen = {}
            for record in t['records']:
                ident = record.get('unit_id', record.get('id', record['location']))
                content = {k: v for k, v in record.items() if k != 'location'}
                if ident in seen:
                    previous = seen[ident]
                    if any(k in previous and previous[k] != value for k, value in content.items()):
                        _pending(engine, uid, 'ambiguous_required_note_provenance', record_id=ident)
                        blocked.add(uid)
                        break
                    previous.update(content)
                    previous['locations'].append(record['location'])
                else:
                    seen[ident] = {**content, 'locations': [record['location']]}
            if uid in blocked:
                continue
            t['records'] = list(seen.values())
            context = {**context, 'note_evidence_records': t['records']}
            t['context'] = context
            jobs.append((uid, {f'note_record_{token}_{i}': _question(
                'Does this exact supplied record apply to the note obligation of the actual fixed unit? '
                'Do not guess a record from proximity or select a new textual reading. Record: '+json.dumps(record, ensure_ascii=False),
                {'YES': 'This actual record applies.', 'NO': 'This record does not apply.',
                 'UNRESOLVED': 'Record applicability is ambiguous or missing necessary linkage.'})
                for i, record in enumerate(t['records'])}, context))
    nominated, failed = _dispatch(engine, 'required-note-record-or-owner', jobs)
    blocked.update(failed)
    jobs = []
    for token, t in active.items():
        uid, context, kind = t['uid'], t['context'], t['req']['kind']
        if uid in blocked:
            continue
        if kind == 'allocation_alternative':
            owner = _value(nominated, f'note_owner_{token}')
            if owner == 'UNRESOLVED':
                _pending(engine, uid, 'unresolved_required_allocation_alternative');blocked.add(uid);continue
            t['owner'] = owner
            questions = {}
            if owner == 'STATUS_ONLY':
                allowed = {s: 'Alternative '+s+' status with unchanged fixed membership.' for s in ('aligned', 'noncompositional')
                           if s != context['selected_status'] and t['unit']['source'] and t['unit']['target']}
                if not allowed:
                    _pending(engine, uid, 'invalid_required_status_alternative');blocked.add(uid);continue
                questions[f'note_status_{token}'] = _question('Which actual competing status remains defensible?', {**allowed,'UNRESOLVED':'No defensible alternative status.'})
            else:
                for tid in engine.target:
                    questions[f'note_target_{token}_{tid}'] = _question(
                        f'Does actual target {tid} belong to this one competing correspondence with proposed owner {owner}? '
                        'Choose the full extent of the same hypothesis; do not change the accepted partition.',
                        {'YES':'Part of this competing correspondence.','NO':'Outside this competing correspondence.','UNRESOLVED':'Cannot establish its extent.'})
                if owner == 'SHARED':
                    for sid in engine.source:
                        questions[f'note_source_{token}_{sid}'] = _question(
                            f'Does actual source {sid} contribute to the one competing shared correspondence?',
                            {'YES':'Contributes.','NO':'Does not contribute.','UNRESOLVED':'Cannot establish contribution.'})
            jobs.append((uid, questions, {**context,'competing_owner_answer':nominated[f'note_owner_{token}']}))
        elif kind != 'lexeme_mismatch':
            selected = []
            for i, record in enumerate(t['records']):
                value = _value(nominated, f'note_record_{token}_{i}')
                if value == 'UNRESOLVED':
                    _pending(engine, uid, 'ambiguous_required_note_record', requirement=t['req']['id']);blocked.add(uid);break
                if value == 'YES':
                    try:_validate_record(kind, record)
                    except ValueError as error:
                        _pending(engine, uid, 'required_note_provenance_malformed', detail=str(error));blocked.add(uid);break
                    if record not in selected:selected.append(record)
            if uid in blocked:continue
            if not selected:
                _pending(engine, uid, 'required_note_record_not_found', requirement=t['req']['id']);blocked.add(uid);continue
            if kind == 'lexical_evidence':
                unsupported = [record for record in selected if
                    record.get('kind') == 'absence' and
                    record.get('direct_support') == record.get('neighboring_support') == 'absent' and record.get('analogy_support') != 'absent' and
                    not any(other.get('kind') == 'analogy' and other.get('id') == record.get('analogy_id')
                            for other in selected)]
                if unsupported:
                    _pending(engine, uid, 'required_note_analogy_record_missing',
                             analogy_ids=[r.get('analogy_id') for r in unsupported])
                    blocked.add(uid)
                    continue
            t['selected_records'] = selected
            questions = {f'note_reason_{token}':_question(
                'Which actual source/target evidence reason explains the selected disposition or setting aside this annotation? '
                'Selected records are provenance, not alignment answers.',REASONS)}
            if kind in {'variant_provenance', 'textual_divergence'}:
                questions[f'note_absence_{token}'] = _question(
                    'Which actual fixed-source/target relation explains this textual note? This is a bounded '
                    'alignment explanation, not an invented documentary reason for upstream selection. '
                    'A fallback-exclusion claim requires supplied evidence of that exclusion.',
                    {'SELECTED_READING_SHORTER': 'Selected projection lacks counterpart to actual target content.',
                     'PROJECTION_LONGER': 'Actual source surplus has no target realization.',
                     'FALLBACK_EXCLUDED_BY_EVIDENCE': 'Supplied preparation evidence declares fallback excluded.',
                     'UNMARKED_LEXICAL_DIVERGENCE': 'Preparation explicitly declares no variant decision; actual lexical divergence remains.',
                     'UNRESOLVED': 'Cannot establish relation or required documentary exclusion evidence.'})
            jobs.append((uid, questions, {**context,'selected_note_records':selected}))
    extent, failed = _dispatch(engine, 'required-note-extent', jobs)
    blocked.update(failed)
    jobs = []
    alternative_tasks = []
    conflicts = []
    for token, t in active.items():
        if t['uid'] in blocked or t['req']['kind']!='allocation_alternative':continue
        owner, unit = t['owner'], t['unit']
        if owner == 'STATUS_ONLY':
            status=_value(extent,f'note_status_{token}');alternative={**deepcopy(unit),'status':status}
        else:
            target_values={tid:_value(extent,f'note_target_{token}_{tid}') for tid in engine.target}
            source_values={sid:_value(extent,f'note_source_{token}_{sid}') for sid in engine.source} if owner=='SHARED' else {}
            if 'UNRESOLVED' in [*target_values.values(),*source_values.values()]:
                _pending(engine,t['uid'],'unresolved_required_alternative_extent');blocked.add(t['uid']);continue
            ts=[tid for tid,v in target_values.items() if v=='YES']
            ss=[sid for sid,v in source_values.items() if v=='YES'] if owner=='SHARED' else ([owner.split(':',1)[1]] if owner.startswith('source:') else [])
            if (not ts and (not ss or owner=='SHARED')) or (owner=='SHARED' and len(ss)<2) or not(set(ss)&set(unit['source']) or set(ts)&set(unit['target'])):
                _pending(engine,t['uid'],'invalid_required_alternative_extent');blocked.add(t['uid']);continue
            alternative={'source':ss,'target':ts,'status':'added' if not ss else ('aligned' if ts else 'unrepresented')}
            # Shared status is a distinct dependent decision with its actual extent.
        if alternative.get('status')=='UNRESOLVED' or (alternative['source']==unit['source'] and alternative['target']==unit['target'] and alternative['status']==t['context']['selected_status']):

            if alternative.get('status') == 'UNRESOLVED':
                _pending(engine,t['uid'],'identical_or_unresolved_allocation_alternative');blocked.add(t['uid'])
            else:
                t['alternative'] = alternative
                conflicts.append((token, t))
            continue
        t['alternative']=alternative
        alternative_tasks.append((token, t))
        qs={f'note_selected_reason_{token}':_question('Why was the actual selected unit allocation chosen over this specific competing correspondence?',REASONS),
            f'note_competing_reason_{token}':_question('Why does this specific alternative correspondence remain materially defensible?',REASONS)}
        if owner=='SHARED':
            qs = {f'note_shared_status_{token}':_question('Classify the competing shared correspondence only; do not change accepted status.',{'aligned':'Contributions remain recoverable.','noncompositional':'Indivisible rendering.','UNRESOLVED':'Cannot classify this hypothesis.'})}
        else:
            qs = {}
        jobs.append((t['uid'],qs,{**t['context'],'actual_competing_correspondence':alternative}))
    classifications, failed = _dispatch(engine,'required-note-shared-status',jobs)
    blocked.update(failed)
    jobs = []
    for token, t in alternative_tasks:
        if t['uid'] in blocked:
            continue
        if t['owner'] == 'SHARED':
            status = _value(classifications, f'note_shared_status_{token}')
            if status == 'UNRESOLVED':
                _pending(engine, t['uid'], 'unresolved_competing_shared_status');blocked.add(t['uid']);continue
            t['alternative']['status'] = status
            if (t['alternative']['source'] == t['unit']['source'] and
                t['alternative']['target'] == t['unit']['target'] and
                status == t['context']['selected_status']):
                _pending(engine, t['uid'], 'identical_or_unresolved_allocation_alternative');blocked.add(t['uid']);continue
        qs = {f'note_selected_reason_{token}': _question(
            'Why was the actual selected allocation chosen over this specific competing correspondence?', REASONS),
            f'note_competing_reason_{token}': _question(
            'Why does this specific alternative remain materially defensible?', REASONS)}
        jobs.append((t['uid'], qs, {**t['context'], 'actual_competing_correspondence': t['alternative']}))
    confirmation_jobs = []
    for token, t in conflicts:
        confirmation_jobs.append((t['uid'], {f'note_conflict_{token}': _question(
            'The broad diagnostic requested a materially different allocation, but the nominated '
            'owner, full extent and status exactly reproduce the selected correspondence. '
            'Does an actual materially different alternative still require a note? Choose NO only '
            'when this was a diagnostic false positive and no such alternative remains. '
            'Do not use NO to conceal missing evidence or an unidentified genuine alternative.',
            {'NO': 'No material alternative remains; diagnostic was overinclusive.',
             'YES': 'A material alternative remains unidentified.',
             'UNRESOLVED': 'Cannot establish whether an actual alternative remains.'})},
            {**t['context'], 'rejected_identical_alternative': t['alternative']}))
    confirmations, failed = _dispatch(engine, 'required-note-conflict-confirmation', confirmation_jobs)
    blocked.update(failed)
    for token, t in conflicts:
        if t['uid'] in blocked:
            continue
        if _value(confirmations, f'note_conflict_{token}') == 'NO':
            t['skip_note'] = True
        else:
            _pending(engine, t['uid'], 'identical_or_unresolved_allocation_alternative')
            blocked.add(t['uid'])
    reasons, failed = _dispatch(engine,'required-note-specific-reasons',jobs)
    blocked.update(failed)
    output={}
    for token,t in active.items():
        uid,kind=t['uid'],t['req']['kind']
        if uid in blocked or t.get('skip_note'):continue
        entry={'requirement':t['req']['id'],'kind':kind,'selected_occurrences':deepcopy(t['context']['selected_occurrences']),'selected_unit':{**deepcopy(t['unit']),'status':t['context']['selected_status'],'heads':t['context']['selected_heads']}}
        if kind=='allocation_alternative':
            chosen=_value(reasons,f'note_selected_reason_{token}');competing=_value(reasons,f'note_competing_reason_{token}')
            shared=t['alternative']['status'] if t['owner']=='SHARED' else None
            if 'UNRESOLVED' in (chosen,competing,shared):
                _pending(engine,uid,'unresolved_required_alternative_reason');blocked.add(uid);continue
            entry.update(alternative=t['alternative'],selected_reason=chosen,alternative_reason=competing)
            if shared:entry['alternative']['status']=shared
        else:
            reason=_value(nominated if kind=='lexeme_mismatch' else extent,f'note_reason_{token}')
            if reason=='UNRESOLVED':
                _pending(engine,uid,'unresolved_required_note_reason');blocked.add(uid);continue
            if kind == 'lexical_evidence' and reason == 'EXPLICIT_ANALOGY' and not any(
                    r.get('kind') == 'analogy' for r in t['selected_records']):
                _pending(engine,uid,'required_note_analogy_record_missing');blocked.add(uid);continue
            entry['reason']=reason
            if kind!='lexeme_mismatch':entry['records']=t['selected_records']
            if kind in {'variant_provenance','textual_divergence'}:
                absence = _value(extent, f'note_absence_{token}')
                if absence == 'UNRESOLVED':
                    _pending(engine,uid,'unresolved_textual_absence_note');blocked.add(uid);continue
                if absence == 'FALLBACK_EXCLUDED_BY_EVIDENCE' and not any(
                    r.get('reason_code') == 'fallback_excluded_by_evidence' or
                    r.get('status') == 'fallback_excluded_by_evidence' for r in t['selected_records']):
                    _pending(engine,uid,'missing_documented_fallback_exclusion');blocked.add(uid);continue
                if absence == 'UNMARKED_LEXICAL_DIVERGENCE' and not any(
                    r.get('status') in {'no_variant','unmarked_no_variant','none'} for r in t['selected_records']):
                    _pending(engine,uid,'missing_declared_unmarked_slot');blocked.add(uid);continue
                entry['bounded_absence_reason'] = absence
        output.setdefault(uid,[]).append(entry)
    return {uid:'Required unit notes: '+json.dumps(entries,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            for uid,entries in output.items() if uid not in blocked}
