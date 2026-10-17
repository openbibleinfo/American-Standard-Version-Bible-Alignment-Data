"""Batch independent fixed-decision questions without merging their evidence."""
from copy import deepcopy
import json

MARKER = '\nEvidence for this question only (JSON): '


def question_context(question, shared):
    """Recover the exact logical evidence for offline audits, not model answers.

    The result is a read-only view: shared values are not copied, so callers
    that keep or change any nested value must copy it first (ready batching
    does). Merged local policies always form a new dictionary.
    """
    context = {k: v for k, v in shared.items() if k != 'question_evidence_scope'}
    if MARKER in question['instructions']:
        local = json.loads(question['instructions'].rsplit(MARKER, 1)[1])
        if 'policies' in local and 'policies' in context:
            local['policies'] = {**context['policies'], **local['policies']}
        context.update(local)
    return context


def prepare_note_jobs(engine, label, jobs):
    """Scope evidence to each focused Choice; dispatcher groups identical packs.

    The local context is copied verbatim, never inferred or reduced. Fixed
    partition, complete rules and common instructions remain shared. Only
    ready questions from this one dependency frontier can be combined.
    """
    calls, failures = [], {}
    for uid, questions, context in jobs:
        if not questions:
            continue
        # Prepare first so policy-dependent supplements and article receipts
        # are identical to the original request before changing its layout.
        try:
            original = engine.make_calls(label, questions, context)
        except ValueError as error:
            failures[uid] = str(error)
            continue
        prepared = original[0][2]
        common_keys = {'policies', 'fixed_partition', 'instruction'}
        common_keys.update(engine.payload_evidence(prepared))
        shared = {k: deepcopy(v) for k, v in prepared.items() if k in common_keys}
        local = {k: deepcopy(v) for k, v in prepared.items() if k not in shared}
        shared['question_evidence_scope'] = (
            'Each Choice has its own JSON evidence in its instructions. Use that evidence only for '
            'that question together with these complete policies and any fixed partition. '
            'Record explanations only; do not change membership, status or heads.')
        scoped = {}
        for qid, question in questions.items():
            scoped[qid] = deepcopy(question)
            scoped[qid]['instructions'] += MARKER + json.dumps(local, ensure_ascii=False, separators=(',', ':'))
        try:
            calls.extend(engine.make_calls(label, scoped, shared, project_evidence=False))
        except ValueError:
            # Scoping into the question adds JSON escaping. If it cannot fit,
            # preserve the original complete request rather than drop evidence.
            calls.extend(original)
    # Import here because the generic repacker uses the audit decoder above.
    from ready_batching import batch_ready
    return batch_ready(engine, calls), failures
