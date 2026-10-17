"""Repack an explicitly independent frontier, retaining focused local guidance.

This module does no linguistic routing and never collects later dependencies.
Only clauses identical in every question are shared. Remaining complete clauses
and evidence belong to the individual question, never a global rule union.
"""
from copy import deepcopy
import json

from note_batching import MARKER, question_context
from request_runner import compact

SCOPE = (
    'Answer each Choice independently. Use the complete shared policies plus only the '
    'additional policies and evidence in that question\'s JSON instructions. Local fields '
    'override common fields. Do not borrow rules, evidence or answers from other questions. '
    'The complete verse remains shared. These answers do not authorize changes to other occurrences.')


def decision_epoch(question, extra):
    context = question_context(question, extra)
    return compact({field: context[field] for field in
                    ('tentative_owners', 'fixed_partition', 'proposed_units') if field in context})


def identical_batches(engine, calls):
    groups, seen = {}, set()
    for name, qs, extra in calls:
        if seen & set(qs):
            raise ValueError('Duplicate independent question identity.')
        seen.update(qs)
        shared_key = compact(extra or {})
        for qid, q in qs.items():
            # Scope can already live inside the question. Inspect every entry,
            # including when dispatch regroups previously scoped note calls.
            key = (shared_key, decision_epoch(q, extra))
            if key not in groups:
                groups[key] = (name, {}, extra)
            groups[key][1][qid] = q
    return [call for name, qs, extra in groups.values()
            for call in engine.make_calls(name + '-ready', qs, extra, project_evidence=False)]


def batch_ready(engine, calls):
    """Choose fewer bounded requests, otherwise retain the original layout.

    Inputs have already received their exact supplemental projection. Nothing
    here calls a model, truncates guidance or evaluates alignment confidence.
    """
    # Different owner maps or fixed partitions are different decision epochs.
    # They cannot acquire a shared snapshot through repacking.
    epochs, seen = {}, set()
    for call in calls:
        _, qs, extra = call
        if not qs:
            continue
        if seen & set(qs):
            raise ValueError('Duplicate independent question identity.')
        seen.update(qs)
        for qid, q in qs.items():
            epochs.setdefault(decision_epoch(q, extra), []).append((call[0], {qid: q}, extra))
    return [call for group in epochs.values() for call in _batch_epoch(engine, group)]


def _batch_epoch(engine, calls):
    original = identical_batches(engine, calls)
    if len(original) < 2:
        return original
    records = []
    for _, qs, extra in original:
        for qid, question in qs.items():
            context = question_context(question, extra)
            q = deepcopy(question)
            if MARKER in q['instructions']:
                q['instructions'] = q['instructions'].rsplit(MARKER, 1)[0]
            records.append((qid, q, context))
    contexts = [context for _, _, context in records]
    shared = {k: deepcopy(v) for k, v in contexts[0].items()
              if k != 'policies' and all(k in c and c[k] == v for c in contexts[1:])}
    if all(isinstance(c.get('policies'), dict) for c in contexts):
        shared['policies'] = {pid: deepcopy(policy) for pid, policy in contexts[0]['policies'].items()
                              if all(c['policies'].get(pid) == policy for c in contexts[1:])}
    shared['question_evidence_scope'] = SCOPE
    questions = {}
    for qid, q, context in records:
        local = {k: deepcopy(v) for k, v in context.items() if k not in shared}
        if 'policies' in shared:
            local['policies'] = {pid: deepcopy(policy) for pid, policy in context['policies'].items()
                                 if pid not in shared['policies']}
        q['instructions'] += MARKER + json.dumps(local, ensure_ascii=False, separators=(',', ':'))
        questions[qid] = q
    try:
        candidate = engine.make_calls('scoped-frontier', questions, shared, project_evidence=False)
    except ValueError:
        # The escaped local guidance can exceed the individual-question bound.
        # Retain the already valid complete requests, with no truncation.
        return original
    return candidate if len(candidate) < len(original) else original
