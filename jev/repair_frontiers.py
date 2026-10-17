"""Speculative read-only salvage frontiers with exact-context invalidation.

Owner changes are still made in original traversal order, after actual answers
and any guided recheck. A wave does not commit future expression records or
membership receipts. Its answers can be consumed only at their original turn
with an unchanged owner map, question, complete pack and source/evidence state.
"""
from copy import deepcopy

from headwords import expression_anchor_question
from request_runner import compact

# Limits wasted speculative questions when an earlier repair changes owners.
# This is a task queue bound, never a source/English candidate window.
MAX_PRECHECKS = 8


class RepairPrechecks:
    def __init__(self, engine):
        self.engine = engine
        self.ready = {}
        self.stats = engine.repair_precheck_stats
        for key in ('waves', 'speculated', 'consumed', 'discarded', 'freshness_rebuilds',
                    'expression_requests', 'membership_requests'):
            self.stats.setdefault(key, 0)

    def identity(self, group, tid, pack, owners):
        engine = self.engine
        return compact({'owners': owners, 'pack': pack,
                        'question': engine.repair_member_question(group, tid),
                        'prior_record': engine.ownership_evidence.get(tid, {}),
                        'state': engine.state, 'required_context': engine.required_context})

    def take(self, group, tid, pack, owners, entries, member_position):
        identity = self.identity(group, tid, pack, owners)
        current = self.ready.get(tid)
        if current is None or current['identity'] != identity:
            if self.ready:
                self.stats['discarded'] += len(self.ready)
                self.stats['freshness_rebuilds'] += 1
                self.ready.clear()
            tasks = self.tasks(entries, member_position)
            # One task has no batching benefit; preserve the old request path.
            if len(tasks) < 2:
                return None
            self.prepare(tasks, owners)
            current = self.ready.get(tid)
        if current is None or current['identity'] != identity:
            raise ValueError('Repair precheck does not match its actual consumption context.')
        self.ready.pop(tid)
        self.engine.ownership_evidence[tid] = deepcopy(current['record'])
        self.stats['consumed'] += 1
        return current['answer']['choice']

    def tasks(self, entries, member_position):
        engine, tasks = self.engine, []
        for i, entry in enumerate(entries):
            if entry['reason'] != 'invalid_or_unresolved_membership':
                continue
            group = entry['membership']
            # Joint/source-only repairs may mutate unrelated owners. They are
            # traversal barriers, not lexical or verse-specific routing.
            if len(group['source']) != 1 or not group['target']:
                break
            tids = group['target'][member_position:] if i == 0 else group['target']
            for tid in tids:
                pack = engine.catalog.pack(engine.catalog.base(engine.language) +
                                           engine.ownership_policies.get(tid, []), engine.language)
                # Conditional evidence judgments retain their ordinary sequence
                # and pending side effects; do not speculate through them.
                if any(p.get('evidence_requirements') for p in pack.values()):
                    return tasks
                tasks.append((group, tid, pack))
                if len(tasks) == MAX_PRECHECKS:
                    return tasks
        return tasks

    def prepare(self, tasks, owners):
        engine = self.engine
        identities = {tid: self.identity(group, tid, pack, owners) for group, tid, pack in tasks}
        originals = {tid: deepcopy(engine.ownership_evidence.get(tid, {})) for _, tid, _ in tasks}
        calls = []
        for _, tid, pack in tasks:
            calls.extend(engine.make_calls('repair-expression',
                {f'expression_{tid}': expression_anchor_question(tid, engine.target)},
                {'policies': pack, 'tentative_owners': owners}))
        start = len(engine.trace)
        anchors = engine.dispatch(calls, scope_questions=True)
        self.stats['expression_requests'] += len(engine.trace) - start
        records, calls = {}, []
        for group, tid, pack in tasks:
            reply = anchors[f'expression_{tid}']
            anchor = reply['choice']
            # Same hypothesis fields as expression_evidence, kept local until
            # the original turn consumes this exact current-context answer.
            record = originals[tid]
            record['expression_evidence'] = engine.expression_record(tid, reply, owners, pack)
            records[tid] = record
            calls.extend(engine.make_calls('repair-member',
                {f'member_{tid}': engine.repair_member_question(group, tid)},
                {'policies': pack, 'tentative_owners': owners,
                 'prior_owner_evidence': engine.scoped_evidence([tid], {tid: record}).get(tid, {})}))
        start = len(engine.trace)
        answers = engine.dispatch(calls, scope_questions=True)
        self.stats['membership_requests'] += len(engine.trace) - start
        self.stats['waves'] += 1
        self.stats['speculated'] += len(tasks)
        self.ready = {tid: {'identity': identities[tid], 'record': records[tid],
                            'answer': deepcopy(answers[f'member_{tid}'])} for _, tid, _ in tasks}

    def close(self):
        self.stats['discarded'] += len(self.ready)
        self.ready.clear()
