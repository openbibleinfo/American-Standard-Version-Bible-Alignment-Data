"""Experimental general Jev controller. No verse routes, seeds, or gold reads.

Every model decision is a bounded choice. Navigation is linguistic judgment by
Jev, not a lexical/morphological router. This candidate is not the selected
historical controller and has not demonstrated live quality equivalence.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import sys

from article_repairs import repair_articles
from unit_notes import required_notes
from run_probe import MODEL
from note_batching import prepare_note_jobs, question_context
from ready_batching import batch_ready, identical_batches
from repair_frontiers import RepairPrechecks
from headwords import headword_question, ownership_question, expression_anchor_question, repair_owner_question
from policy_runtime import Catalog
from request_runner import Runner, compact

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'src'))
from alignment_research.alignment import validate_response

# Punctuation that ends a clause for the paraphrase check.
CLAUSE_BOUNDARY = re.compile(r'[,;:.!?\u2014\u2013]')
MAX_PARAPHRASE_WORDS = 8
MAX_SOURCE_EXPRESSION = 4

ESCAPES = {
    'ADDED': 'No source lexical or grammatical contribution licenses this occurrence.',
    'SHARED_PHRASE': 'This occurrence jointly realizes more than one source member.',
    'UNRESOLVED': 'No defensible allocation from the supplied evidence.',
}


class GeneralRunner(Runner):
    strategy_prefix = 'general-policy-'


class GuidanceContextUnavailable(ValueError):
    pass


# Frontiers whose common answer is "nothing additional": the next frontier is
# predictable under that hypothesis and is checked byte-for-byte on arrival.
SPECULATIVE_LABELS = ('construction-guidance', 'additional-families')


class HypothesisBackend:
    """Replay recorded answers, answer one frontier by hypothesis, then stop.

    Used only inside a throwaway engine clone to discover the next ready
    frontier. It never fabricates an answer the real engine consumes.
    """
    def __init__(self, recorded, hypothesis):
        self.recorded, self.hypothesis, self.next_calls = recorded, hypothesis, []

    def ask_many(self, calls):
        responses = {}
        for name, qs, extra in calls:
            known = self.recorded.get(name, self.hypothesis.get(name))
            if known is None:
                if responses:
                    raise RuntimeError('Mixed recorded and unseen requests; no speculation.')
                self.next_calls = [(n, deepcopy(q), deepcopy(e)) for n, q, e in calls]
                raise PreparationComplete()
            if set(known) != set(qs):
                raise RuntimeError('Replay diverged; no speculation.')
            responses[name] = deepcopy(known)
        return responses


def choice(instructions, criteria):
    return {'type': 'choice', 'instructions': instructions, 'criteria': criteria}


class Engine:
    def __init__(self, state, packet, backend, catalog=None):
        self.state, self.packet, self.backend = deepcopy(state), packet, backend
        self._initial_state = deepcopy(state)
        self.catalog = catalog or Catalog()
        self.speculate = False  # Entry points enable it; tests and audits run plain.
        self.language = state['language']
        self.source, self.target = state['source_tokens'], state['target_tokens']
        self.trace, self.pending = [], []
        self.ownership_policies = {}
        # English words of a paraphrase, with the source members the phrase renders.
        self.paraphrase_members = {}
        # Word-by-word owners the paraphrase check replaced, restored if its unit is rejected.
        self.paraphrase_prior = {}
        self.ownership_evidence = {}
        self.counter = 0
        self.evidence_checks = {}
        self.evidence_failures = {}
        self.validity_receipts = []
        self.repair_precheck_stats = {}
        self.unit_policies = {}
        self.subject_scopes = {}
        self.required_context = deepcopy(packet.get('runtime_evidence', {}))
        if packet.get('source_projection'):
            self.required_context.setdefault('source_projection', deepcopy(packet['source_projection']))

    def payload_evidence(self, extra):
        """Project declared supplemental fields; full originals remain available locally."""
        paths = {field for policy in extra.get('policies', {}).values()
                 for req in policy.get('evidence_requirements', []) for field in req['required_fields']}
        # Fixed source projection is factual input, not a choice of another reading.
        if 'source_projection' in self.required_context:
            paths.add('source_projection')
        result = {}
        for path in sorted(paths):
            parts = path.split('.')
            if parts[:2] == ['source_tokens', '*']:
                ids = extra.get('evidence_subject_source_ids', list(self.source))
                for sid in ids:
                    self.copy_evidence_path(result, self.required_context, ['source_tokens', sid, *parts[2:]])
            else:
                self.copy_evidence_path(result, self.required_context, parts)
        if 'source_tokens' in result:
            result['source_tokens'] = {sid: {**deepcopy(self.source[sid]), **result['source_tokens'].get(sid, {})}
                                       for sid in self.source}
        return result

    @staticmethod
    def copy_evidence_path(out, available, parts):
        node = available
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return
            node = node[part]
        dest = out
        for part in parts[:-1]:
            dest = dest.setdefault(part, {})
        dest[parts[-1]] = deepcopy(node)

    def make_calls(self, label, questions, extra=None, project_evidence=True):
        """One shared context; separate questions. Split only for payload bounds."""
        if not questions:
            return []
        extra = deepcopy(extra or {})
        # Article eligibility keeps its full local receipt, while the current
        # policy pack already supplies the identical clauses to this request.
        article = extra.get('article_evidence', {})
        guidance = article.pop('eligibility_guidance', None)
        if guidance is not None:
            prior_pack = guidance.get('policies', {})
            declared = extra.setdefault('policies', {})
            for pid, policy in prior_pack.items():
                if pid in declared and declared[pid] != policy:
                    raise GuidanceContextUnavailable('Article rule receipt conflicts with the current complete pack.')
                declared[pid] = deepcopy(policy)
            article['eligibility_policy_ids'] = list(prior_pack)
            article['eligibility_pack_sha256'] = hashlib.sha256(compact(prior_pack)).hexdigest()
        supplement = self.payload_evidence(extra or {}) if project_evidence and label != 'ownership' else {}
        state = {**self.state, **supplement, **(extra or {})}
        state_bytes = len(compact(state))
        # Exact compact payload length of {'model', 'state', 'questions'},
        # accumulated per question instead of re-serializing the whole state.
        envelope = len(compact({'model': MODEL, 'state': {}, 'questions': {}})) - 2 + state_bytes
        batches, batch, batch_bytes = [], {}, 0
        for qid, q in questions.items():
            question_bytes = len(compact(q))
            # One question may fill a request alone, up to the same bound as a batch.
            if envelope + len(compact(qid)) + 1 + question_bytes + 4096 > 60000:
                raise GuidanceContextUnavailable('A complete question exceeds the context bound; no guidance truncated.')
            entry = len(compact(qid)) + 1 + question_bytes + (1 if batch else 0)
            if batch and (len(batch) + 1 > 300 or envelope + batch_bytes + entry + 4096 > 60000):
                batches.append(batch)
                batch, batch_bytes = {}, 0
                entry -= 1
            batch[qid] = q
            batch_bytes += entry
        if batch:
            batches.append(batch)
        self.counter += 1
        return [(f'{self.counter:02d}-{label}-{i:02d}', b,
                 {**supplement, **(extra or {})})
                for i, b in enumerate(batches, 1)]

    def ask(self, label, questions, extra=None):
        return self.dispatch(self.make_calls(label, questions, extra))

    def dispatch(self, calls, scope_questions=False):
        # Inspect logical epochs per question, including pre-scoped note calls.
        # Independent dispatch never combines different owner maps/partitions.
        calls = identical_batches(self, calls)
        if not calls:
            return {}
        if scope_questions:
            calls = batch_ready(self, calls)
        # ask_many retains distinct questions, full frozen context and cost receipts.
        continuation = self.speculation(calls) if self.speculate else None
        if continuation is not None and getattr(self.backend, 'supports_speculation', False):
            responses = self.backend.ask_many(calls, speculate=continuation)
        else:
            responses = self.backend.ask_many(calls)
        answers = {}
        for name, qs, extra in calls:
            answer = responses.get(name)
            if answer is None:
                raise ValueError('Execution backend did not return answers.')
            if set(answer) != set(qs):
                raise ValueError('Missing or extra answers; do not infer defaults.')
            for qid, q in qs.items():
                if answer[qid].get('choice') not in q['criteria']:
                    raise ValueError('Illegal model choice.')
            answers.update(answer)
            self.trace.append({'request_name': name, 'question_ids': list(qs), 'answers': deepcopy(answer),
                               'policies': list((extra or {}).get('policies', {})),
                               'question_policies': {qid: list(question_context(q, extra).get('policies', {}))
                                                     for qid, q in qs.items()}})
        return answers

    def speculation(self, calls):
        """Return a continuation predicting the next frontier, or None.

        The hypothesis is that every question in this frontier answers NO (or
        its single allowed option). A fresh clone replays the recorded answers
        so far plus that hypothesis and stops at its next frontier. The runner
        consumes a predicted request only when the real frontier later prepares
        the identical bytes under the identical name; otherwise it is discarded
        and its charge recorded. No answer reaches this engine from the clone.
        """
        if not calls or not all(any(label in name for label in SPECULATIVE_LABELS) for name, _, _ in calls):
            return None
        hypothesis = {}
        for name, qs, _ in calls:
            hypothesis[name] = {}
            for qid, q in qs.items():
                criteria = q.get('criteria', {})
                if q.get('type') != 'choice' or ('NO' not in criteria and len(criteria) != 1):
                    return None
                hypothesis[name][qid] = {'choice': 'NO' if 'NO' in criteria else next(iter(criteria)),
                                         'probabilities': {}, 'confidence': None, 'speculative_hypothesis': True}
        recorded = {t['request_name']: t['answers'] for t in self.trace if 'request_name' in t}
        initial, packet, catalog, kind = self._initial_state, self.packet, self.catalog, type(self)

        def continuation():
            backend = HypothesisBackend(recorded, hypothesis)
            clone = kind(initial, packet, backend, catalog)
            try:
                clone.run()
            except PreparationComplete:
                return backend.next_calls
            except Exception:
                return []
            return []
        return continuation

    def owner_questions(self, subjects=None):
        if not self.source:
            return {}
        subjects = list(self.target) if subjects is None else subjects
        candidates = {sid: f"{sid}: {s['surface']}; {s.get('gloss')}; {s.get('morph')}"
                      for sid, s in self.source.items()}
        return {f'assign_{tid}': choice(
            f"Which {self.language.title()} token licenses English {tid} '{self.target[tid]}' "
            'in this particular occurrence? Include lexical and grammatical helpers. '
            'A repeated source word must match its own occurrence.',
            {**candidates, 'ADDED': 'No lexical or grammatical licensing.',
             'SHARED_PHRASE': 'Needs several source tokens.',
             'UNRESOLVED': 'No defensible allocation.'}) for tid in subjects}

    def owners(self, policy_pack=None, subjects=None, prior=None):
        if not self.source:
            return {tid: 'ADDED' for tid in (subjects if subjects is not None else self.target)}
        questions = self.owner_questions(subjects)
        extra = {'policies': policy_pack or {}}
        if prior is not None:
            extra['tentative_owners'] = prior
        return {qid.removeprefix('assign_'): a['choice']
                for qid, a in self.ask('ownership', questions, extra).items()}

    def select(self, stage, subjects, prior):
        questions = {f'family_{key}': self.catalog.family_question(self.language, stage, description)
                     for key, description in subjects.items()}
        extra = {'tentative_owners': prior, 'policies': self.catalog.pack(self.catalog.base(self.language), self.language)}
        families = self.ask(f'{stage}-families', questions, extra)
        selected, leaves = {}, {}
        for key, description in subjects.items():
            fid = families[f'family_{key}']['choice']
            if fid == 'GENERAL':
                selected[key] = None
            elif fid == 'NEEDS_GUIDANCE':
                selected[key] = None
                self.pending.append({'subject': key, 'stage': stage, 'reason': 'missing_policy_family'})
            else:
                leaves[f'policy_{key}'] = self.catalog.leaf_question(self.language, stage, fid, description)
        answers = self.ask(f'{stage}-policies', leaves, extra)
        for qid, a in answers.items():
            key = qid.removeprefix('policy_')
            selected[key] = None if a['choice'] in {'GENERAL', 'NEEDS_GUIDANCE'} else a['choice']
            if a['choice'] == 'NEEDS_GUIDANCE':
                self.pending.append({'subject': key, 'stage': stage, 'reason': 'missing_policy'})
        return selected

    def select_many(self, stage, subjects, prior, primary=True, known=None):
        """Independent bounded relevance decisions, one family then one card.

        Never send the catalog or every applicability in one question/context.
        Selection is a judgment about this subject, not an owner sweep.
        """
        selected = {key: list((known or {}).get(key, self.ownership_policies.get(key, []))) for key in subjects}
        if primary:
            first = self.select(stage, subjects, prior)
            for key, pid in first.items():
                if pid:
                    selected[key].append(pid)
        base_ids = self.catalog.base(self.language)
        families = self.catalog.families(self.language, stage)
        contexts = {key: {'policies': self.catalog.pack(base_ids + selected[key], self.language),
                          'tentative_owners': prior} for key in subjects}
        calls, routes, candidates = [], {}, {}
        for key, description in subjects.items():
            qs = {}
            for fid, family in families.items():
                remaining = [pid for pid in family['policies'] if pid not in contexts[key]['policies']]
                if not remaining:
                    continue
                qid = f'need_{stage}_{key}_{fid}'
                routes[qid] = (key, fid); candidates[qid] = remaining
                titles = '; '.join(self.catalog.policies[pid]['title'] for pid in remaining)
                qs[qid] = choice(
                    f'For {stage} of {description}, is an additional policy from family {fid} needed '
                    f'beyond the complete guidance already supplied? Remaining card topics: {titles}. '
                    'Do not request a family merely because it describes the unit or guidance already present. '
                    'Other families may independently be necessary. This does not change ownership.',
                    {'NO': 'Supplied guidance suffices or these remaining cards are irrelevant.',
                     'YES': 'At least one remaining card is needed.',
                     'UNRESOLVED': 'Cannot establish necessary additional guidance.'})
            calls.extend(self.make_calls(f'{stage}-additional-families', qs, contexts[key]))
        family_answers = self.dispatch(calls, scope_questions=True)
        calls, card_routes = [], {}
        for key in subjects:
            qs = {}
            for qid, (subject, fid) in routes.items():
                if subject != key:
                    continue
                answer = family_answers[qid]['choice']
                if answer == 'UNRESOLVED':
                    self.pending.append({'subject': key, 'stage': stage, 'reason': 'unresolved_guidance_family', 'family': fid})
                if answer != 'YES':
                    continue
                for pid in candidates[qid]:
                    cqid = f'apply_{stage}_{key}_{pid}';card_routes[cqid] = (key, pid)
                    qs[cqid] = choice(
                        f'Is this additional policy actually necessary for {stage} of {subjects[key]}? '
                        f'Applicability: {self.catalog.policies[pid]["applicability"]}. '
                        'Choose NO if its actual conditions do not apply or supplied guidance already suffices.',
                        {'NO': 'Not needed.', 'YES': 'Necessary additional policy.',
                         'UNRESOLVED': 'Cannot establish applicability.'})
            calls.extend(self.make_calls(f'{stage}-additional-policies', qs, contexts[key]))
        answers = self.dispatch(calls, scope_questions=True)
        for qid, answer in answers.items():
            key, pid = card_routes[qid]
            if answer['choice'] == 'YES':
                selected[key].append(pid)
            elif answer['choice'] == 'UNRESOLVED':
                self.pending.append({'subject': key, 'stage': stage, 'reason': 'unresolved_policy_applicability', 'policy': pid})
        calls, gaps = [], {}
        for qid, (key, fid) in routes.items():
            if family_answers[qid]['choice'] != 'YES' or set(selected[key]) & set(candidates[qid]):
                continue
            if any(answers.get(f'apply_{stage}_{key}_{pid}', {}).get('choice') == 'UNRESOLVED' for pid in candidates[qid]):
                continue
            gqid = f'gap_{stage}_{key}_{fid}';gaps[gqid] = (key, fid)
            calls.extend(self.make_calls('guidance-gap-confirmation', {gqid: choice(
                f'For {stage} of {subjects[key]}, the broad family {fid} diagnostic said YES, '
                'but every remaining candidate card was judged unnecessary. Does a genuine uncovered '
                'condition still require guidance absent from the supplied complete pack? '
                'A coarse family false positive is not a missing rule.',
                {'NO': 'Supplied guidance suffices; the broad diagnostic was overinclusive.',
                 'YES': 'A genuine uncovered condition remains.', 'UNRESOLVED': 'Cannot resolve the discrepancy.'})},
                {**contexts[key], 'evaluated_policy_answers': {pid: answers.get(f'apply_{stage}_{key}_{pid}') for pid in candidates[qid]}}))
        for qid, answer in self.dispatch(calls, scope_questions=True).items():
            if answer['choice'] != 'NO':
                key, fid = gaps[qid]
                self.pending.append({'subject': key, 'stage': stage, 'reason': 'missing_policy' if answer['choice'] == 'YES' else 'unresolved_guidance_family', 'family': fid})
        return selected

    def retrieval_pending(self, subject, since=0):
        reasons = {'missing_policy', 'missing_policy_family', 'unresolved_guidance_family',
                   'unresolved_policy_applicability', 'unresolved_construction_guidance'}
        return any(p.get('subject') == subject and p.get('reason') in reasons for p in self.pending[since:])

    def evidence_ready(self, subject, stage, pack, source_ids=None):
        return self.evidence_ready_many([(subject, stage, pack, source_ids)])[(subject, stage)]

    def evidence_ready_many(self, frames):
        """Condition judgments form an independent frontier; presence is mechanical."""
        base_available = {**self.state, **self.required_context}
        readiness, decisions, calls = {}, {}, []
        def record_pending(frame, pid, requirement, entry):
            reasons = {'conditional_evidence_unavailable', 'unresolved_evidence_condition'}
            self.pending = [p for p in self.pending if not (
                p.get('subject') == frame[0] and p.get('stage') == frame[1]
                and p.get('policy') == pid and p.get('requirement') == requirement['id']
                and p.get('reason') in reasons)]
            if entry is not None:
                self.pending.append(deepcopy(entry))
        for specification in frames:
            subject, stage, pack, source_ids = specification[:4]
            decision_context = specification[4] if len(specification) > 4 else {}
            available = {**base_available, **decision_context}
            source_ids = list(self.source) if source_ids is None else list(source_ids)
            frame = (subject, stage)
            readiness[frame] = True
            for pid, policy in pack.items():
                for requirement in policy.get('evidence_requirements', []):
                    scope = deepcopy(self.subject_scopes.get(subject, {
                        'source': source_ids, 'target': [subject] if subject in self.target else []}))
                    extra = {'policies': self.catalog.pack([pid], self.language),
                             'evidence_subject_source_ids': source_ids,
                             'evidence_subject_membership': scope,
                             'required_evidence_fields': requirement['required_fields'],
                             **self.payload_evidence({'policies': pack, 'evidence_subject_source_ids': source_ids}), **decision_context}
                    identity = (subject, stage, pid, compact(requirement),
                                compact({'state': {**self.state, **extra}, 'scope': scope}))
                    if identity in self.evidence_checks:
                        readiness[frame] = self.evidence_checks[identity] and readiness[frame]
                        record_pending(frame, pid, requirement, self.evidence_failures.get(identity))
                        continue
                    qid = f'evidence_{subject}_{pid}_{requirement["id"]}'
                    if qid in decisions:
                        raise ValueError('Duplicate conditional evidence identity in one frontier.')
                    decisions[qid] = (frame, identity, pid, requirement, source_ids, available, scope)
                    questions = {qid: choice(
                        f'For {stage} of {subject}, does this evidence condition apply? '
                        f'{requirement["condition"]} Evaluate the actual construction and decision. '
                        'Absence of a field does not establish that its condition applies.',
                        {'NO': 'The condition does not apply.', 'YES': 'The condition applies.',
                         'UNRESOLVED': 'Cannot establish whether the condition applies.'})}
                    calls.extend(self.make_calls('conditional-evidence', questions, extra))
        answers = self.dispatch(calls, scope_questions=True)
        for qid, (frame, identity, pid, requirement, source_ids, available, scope) in decisions.items():
            answer = answers[qid]['choice']
            missing = [field for field in requirement['required_fields']
                       if not self.field_present(available, field, source_ids)]
            outcome = not (answer == 'UNRESOLVED' or (answer == 'YES' and missing))
            self.evidence_checks[identity] = outcome
            readiness[frame] = outcome and readiness[frame]
            entry = None
            if not outcome:
                entry = {'subject': frame[0], 'stage': frame[1], 'policy': pid,
                                     'requirement': requirement['id'], 'missing_fields': missing,
                                     'membership': deepcopy(scope),
                                     'reason': 'conditional_evidence_unavailable' if answer == 'YES' else 'unresolved_evidence_condition'}
                self.evidence_failures[identity] = entry
            record_pending(frame, pid, requirement, entry)
        return readiness

    @staticmethod
    def field_present(state, path, source_ids):
        import re
        parts = path.split('.')
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*|\*', part) for part in parts):
            raise ValueError('Invalid evidence field path.')
        if '*' in parts and (parts[:2] != ['source_tokens', '*'] or parts.count('*') != 1):
            raise ValueError('Evidence wildcard is supported only for relevant source_tokens.*.')
        def present(value, remainder):
            if not remainder:
                return value is not None and value != '' and value != [] and value != {}
            key, *tail = remainder
            if key == '*':
                return bool(source_ids) and isinstance(value, dict) and all(sid in value and present(value[sid], tail) for sid in source_ids)
            return isinstance(value, dict) and key in value and present(value[key], tail)
        return present(state, parts)

    def expression_evidence(self, tids, owners, pack):
        questions = {f'expression_{tid}': expression_anchor_question(tid, self.target) for tid in tids}
        answers = self.ask('repair-expression', questions, {'policies': pack, 'tentative_owners': owners})
        for tid in tids:
            answer = answers[f'expression_{tid}']
            self.ownership_evidence.setdefault(tid, {})['expression_evidence'] = self.expression_record(tid, answer, owners, pack)

    @staticmethod
    def expression_record(tid, answer, owners, pack):
        return {'target_occurrence': tid, 'proposed_anchor': answer['choice'],
                'anchor_answer': deepcopy(answer),
                'tentative_anchor_owner': owners.get(answer['choice']), 'hypothesis_only': True,
                'policy_ids': list(pack), 'policy_pack_sha256': hashlib.sha256(compact(pack)).hexdigest()}

    def scoped_evidence(self, tids, records=None):
        """Return only these occurrence hypotheses; rule bodies live in policies."""
        records = self.ownership_evidence if records is None else records
        evidence = {tid: deepcopy(records[tid]) for tid in tids if tid in records}
        for record in evidence.values():
            if 'eligibility_guidance' in record:
                guidance = record.pop('eligibility_guidance')
                pack = guidance.get('policies', {})
                record['eligibility_policy_ids'] = list(pack)
                record['eligibility_pack_sha256'] = hashlib.sha256(compact(pack)).hexdigest()
            expression = record.get('expression_evidence', {})
            if 'policies_supplied' in expression:
                pack = expression.pop('policies_supplied')
                expression['policy_ids'] = list(pack)
                expression['policy_pack_sha256'] = hashlib.sha256(compact(pack)).hexdigest()
        return evidence

    def prepare_unit_guidance(self, groups, owners):
        # A bounded construction diagnostic can inspect every fixed proposal;
        # NO never reopens ownership or membership. Cardinality is not a linguistic router.
        subjects = {u['id']: f"fixed proposed unit {u}" for u in groups}
        diagnostic = self.ask('construction-guidance', {
            f'construction_{uid}': choice(
                f'Does {description} require specialized construction guidance beyond '
                'the supplied base principles? This is a diagnostic, not a reassignment or a membership review.',
                {'NO': 'Base guidance suffices.', 'YES': 'Specialized guidance is necessary.',
                 'UNRESOLVED': 'Cannot establish necessary guidance.'}) for uid, description in subjects.items()},
            {'policies': self.catalog.pack(self.catalog.base(self.language), self.language), 'tentative_owners': owners})
        needed = {}
        for uid, description in subjects.items():
            value = diagnostic[f'construction_{uid}']['choice']
            if value == 'YES':
                needed[uid] = description
            elif value == 'UNRESOLVED':
                self.pending.append({'subject': uid, 'stage': 'membership', 'reason': 'unresolved_construction_guidance'})
        selected = self.select_many('membership', needed, owners) if needed else {}
        for group in groups:
            ids = selected.get(group['id'], [])
            if group['id'] in needed and not ids and not self.retrieval_pending(group['id']):
                self.pending.append({'subject': group['id'], 'stage': 'membership', 'reason': 'missing_policy'})
            self.unit_policies[(tuple(group['source']), tuple(group['target']))] = ids
            for tid in group['target']:
                self.ownership_policies[tid] = sorted(set(self.ownership_policies.get(tid, []) + ids))

    def repair_escapes(self, owners):
        # Preserve the established first pass. Retrieve guidance only for
        # additions and discovery abstentions, not every successful owner.
        subjects = {tid: f"English occurrence {tid} '{self.target[tid]}'"
                    for tid, owner in owners.items() if owner in {'ADDED', 'UNRESOLVED'}}
        if not subjects:
            return dict(owners)
        selected = self.select_many('membership', subjects, owners)
        updated = dict(owners)
        for tid, pids in selected.items():
            for pid in pids:
                self.trace.append({'subject': tid, 'selected_policy': pid, 'stage': 'membership'})
            pack = self.catalog.pack(self.catalog.base(self.language) + pids, self.language)
            self.ownership_policies[tid] = list(pack)
            if self.retrieval_pending(tid):
                continue
            if not self.evidence_ready(tid, 'membership', pack):
                continue
            self.expression_evidence([tid], updated, pack)
            q = ownership_question(tid, self.target, self.language.title(), self.source_options(), ESCAPES)
            q['instructions'] += (' Test the addition explicitly against grammatical licensing: '
                'subject person/number, tense, mood, aspect, voice, case, and construction can '
                'realize English without a separate dictionary word. Preserve a genuine addition '
                'when none of the source choices licenses it.')
            answer = self.ask('residual-owner', {f'owner_{tid}': q},
                              {'policies': pack, 'tentative_owners': updated,
                               'expression_evidence': self.scoped_evidence([tid]).get(tid, {})})
            updated[tid] = answer[f'owner_{tid}']['choice']
        return updated

    def source_options(self):
        return {sid: f"{sid}: {s.get('surface', '')}; lemma {s.get('lemma', '')}; "
                f"gloss {s.get('gloss', '')}; morph {s.get('morph', '')}" for sid, s in self.source.items()}

    def repair_determiners(self, owners, subjects=None, retrieve=True):
        ids = (['greek.article_realization', 'greek.anarthrous_determiner']
               if self.language == 'greek' else ['hebrew.morpheme_licensing', 'hebrew.supplied_determiner'])
        pack = self.catalog.pack(self.catalog.base(self.language) + ids, self.language)
        tids = [tid for tid, word in self.target.items() if word.casefold() in {'the', 'a', 'an'} and (subjects is None or tid in subjects)]
        pending_start = len(self.pending)
        selected = self.select_many('membership', {tid: f"determiner {tid} '{self.target[tid]}' in its actual expression" for tid in tids}, owners, primary=False) if tids and retrieve else {tid: [] for tid in tids}
        if any(self.retrieval_pending(tid, pending_start) for tid in tids):
            return dict(owners)
        packs = {tid: self.catalog.pack(self.catalog.base(self.language) + ids + self.ownership_policies.get(tid, []) + selected[tid], self.language) for tid in tids}
        outcomes = self.evidence_ready_many([(tid, 'article eligibility', packs[tid], None,
            {'tentative_owners': deepcopy(owners), 'prior_owner_evidence': self.scoped_evidence([tid])}) for tid in tids])
        if not all(outcomes.values()):
            return dict(owners)
        adapter = HelperRunner(self)
        updated = repair_articles(adapter, owners, self.language.title(), policy_pack=pack, policy_packs=packs, subjects=tids)
        for tid in tids:
            self.ownership_policies[tid] = list(packs[tid])
        return updated

    def prepare_with_article_refresh(self, groups, owners):
        """Recheck previous article exclusions only when their complete pack grows."""
        owners = dict(owners)
        for _ in range(2):
            self.prepare_unit_guidance(groups, owners)
            changed = []
            for tid, record in self.ownership_evidence.items():
                previous = record.get('eligibility_guidance', {}).get('policies')
                if previous is None or self.target[tid].casefold() not in {'the', 'a', 'an'}:
                    continue
                current = self.catalog.pack(self.ownership_policies.get(tid, []), self.language)
                if previous != current:
                    changed.append(tid)
            if not changed:
                return groups, owners
            updated = self.repair_determiners(owners, subjects=changed, retrieve=False)
            if updated == owners:
                # The current complete pack was supplied to the repeated gates.
                return groups, owners
            owners = updated
            groups = self.memberships(owners)
        self.pending.append({'reason': 'article_guidance_refresh_limit', 'target': changed})
        return groups, owners

    def repair_member_question(self, group, tid):
        sid = group['source'][0]
        return choice(
            f"Evaluate tentative membership for {self.language.title()} {sid}: {self.source[sid]}. "
            f"Is English {tid} '{self.target[tid]}' nevertheless a valid member of this "
            f"source token's rendering in the complete verse? Other proposed members were {group['target']}. "
            'Test this occurrence’s lexical or grammatical contribution, including licensed helpers, '
            'not dictionary identity alone. Evaluate each occurrence separately: a wrong other '
            'member does not invalidate this one. Identify the actual English expression this '
            'occurrence belongs to and compare the source expression. A matching gloss in '
            'another expression is not sufficient. Keep repeated words and expressions distinct. '
            'Do not decide status or display head.',
            {'YES': 'This particular membership is defensible.', 'NO': 'This occurrence belongs elsewhere or has no licensing here.',
             'UNRESOLVED': 'Cannot establish this particular membership.'})

    def repair_rejected(self, owners):
        updated = dict(owners)
        entries = list(self.pending)
        prechecks = RepairPrechecks(self)
        for position, pending in enumerate(entries):
            if pending['reason'] != 'invalid_or_unresolved_membership':
                continue
            group = pending['membership']
            if self.restore_paraphrase(group, updated):
                self.pending.remove(pending)
                continue
            # Preserve shared-source uncertainty; do not turn a joint source
            # expression into single-token decisions or a connected-component fix.
            if not group['target']:
                updated = self.repair_source_disposition(group, updated)
                continue
            if len(group['source']) != 1:
                updated = self.repair_joint(group, updated)
                continue
            sid = group['source'][0]
            confirmed = True
            for member_position, tid in enumerate(group['target']):
                pack = self.catalog.pack(self.catalog.base(self.language) + self.ownership_policies.get(tid, []), self.language)
                q = self.repair_member_question(group, tid)
                if not self.evidence_ready(tid, 'membership', pack, [sid]):
                    confirmed = False
                    continue
                answer = prechecks.take(group, tid, pack, updated,
                    entries[position:], member_position)
                if answer is None:
                    self.expression_evidence([tid], updated, pack)
                    answer = self.ask('repair-member', {f'member_{tid}': q},
                                      {'policies': pack, 'tentative_owners': updated,
                                       'prior_owner_evidence': self.scoped_evidence([tid]).get(tid, {})})[f'member_{tid}']['choice']
                if answer == 'YES':
                    self.validity_receipts.append({'source': [sid], 'target': [tid], 'answer': {'choice': 'YES'}, 'kind': 'per_member', 'policies': deepcopy(pack)})
                    continue
                if answer == 'UNRESOLVED':
                    self.pending.append({'subject': tid, 'stage': 'membership', 'reason': 'unresolved_member', 'source': sid})
                    confirmed = False
                    continue
                pending_before_selection = len(self.pending)
                selected = self.select_many('membership', {tid: f"Rejected English occurrence {tid} '{self.target[tid]}'"}, updated)
                pids = selected[tid]
                prior_pack = pack
                pack = self.catalog.pack(list(pack) + pids, self.language)
                self.ownership_policies[tid] = list(pack)
                if any(p.get('subject') == tid and p.get('reason') in {'missing_policy', 'missing_policy_family', 'unresolved_guidance_family', 'unresolved_policy_applicability'}
                       for p in self.pending[pending_before_selection:]):
                    # The new guidance is unavailable: keep the tentative edge
                    # and its pending rejection rather than make exclusion final.
                    confirmed = False
                    continue
                if not self.evidence_ready(tid, 'membership', pack, [sid]):
                    confirmed = False
                    continue
                if pack != prior_pack:
                    # The earlier NO did not see this guidance. Re-evaluate the
                    # same membership before removing the original source option.
                    answer = self.ask('guided-repair-member', {f'member_{tid}': q},
                                      {'policies': pack, 'tentative_owners': updated,
                                       'prior_owner_evidence': self.scoped_evidence([tid]).get(tid, {})})[f'member_{tid}']['choice']
                    if answer == 'YES':
                        self.validity_receipts.append({'source': [sid], 'target': [tid], 'answer': {'choice': 'YES'}, 'kind': 'per_member', 'policies': deepcopy(pack)})
                        continue
                    if answer == 'UNRESOLVED':
                        self.pending.append({'subject': tid, 'stage': 'membership', 'reason': 'unresolved_member', 'source': sid})
                        confirmed = False
                        continue
                options = {key: value for key, value in self.source_options().items() if key != sid}
                q = repair_owner_question(tid, self.target, self.language.title(), options, ESCAPES)
                q['instructions'] += ' The previous source was explicitly rejected for this occurrence; choose another defensible owner or a listed escape. Do not change other members.'
                updated[tid] = self.ask('repair-reassign', {f'owner_{tid}': q},
                                       {'policies': pack, 'tentative_owners': updated,
                                        'rejected_source_for_this_occurrence': sid,
                                        'prior_owner_evidence': self.scoped_evidence([tid]).get(tid, {})})[f'owner_{tid}']['choice']
                confirmed &= updated[tid] != 'UNRESOLVED'
            if confirmed:
                self.pending.remove(pending)
        prechecks.close()
        return updated

    def restore_paraphrase(self, group, owners):
        """Undo a rejected paraphrase: its words, and words it displaced, get their earlier owners.

        A paraphrase fails per-word contribution tests by definition, so the
        joint repair would strip every word of its source.
        """
        phrase = [tid for tid in group['target'] if tid in self.paraphrase_members]
        if not phrase:
            return False
        claimed = set().union(*(self.paraphrase_members.pop(tid) for tid in phrase))
        kept = set().union(*self.paraphrase_members.values())
        for tid, prior in self.paraphrase_prior.items():
            if tid in phrase or (tid not in self.paraphrase_members and prior in claimed - kept):
                owners[tid] = prior
        return True

    def repair_source_disposition(self, group, owners):
        sid = group['source'][0]
        subject = f"source-only occurrence {sid}: {self.source[sid]}; all English occurrences remain candidates"
        selected = self.select_many('membership', {sid: subject}, owners)
        pack = self.catalog.pack(self.catalog.base(self.language) + selected[sid], self.language)
        if self.retrieval_pending(sid):
            return dict(owners)
        if not self.evidence_ready(sid, 'source-only realization', pack, [sid]):
            return dict(owners)
        answer = self.ask('source-realization', {f'realize_{sid}': choice(
            f'Which exact English occurrence provides a possible lexical or grammatical realization of {subject}? '
            'Already owned occurrences remain candidates: current owners are hypotheses, '
            'but repeated words in another expression do not count. Nominate one occurrence '
            'to test, not an owner command. Use SHARED when only a jointly realized expression is defensible.',
            {**{tid: f'{tid}: {word}' for tid, word in self.target.items()},
             'UNREPRESENTED': 'No independent English realization is licensed.',
             'SHARED': 'A shared expression requires contributor diagnosis.',
             'UNRESOLVED': 'Cannot establish a realization.'})},
            {'policies': pack, 'tentative_owners': owners})[f'realize_{sid}']['choice']
        updated = dict(owners)
        if answer in self.target:
            self.expression_evidence([answer], updated, pack)
            q = repair_owner_question(answer, self.target, self.language.title(), self.source_options(), ESCAPES)
            reply = self.ask('source-disposition-owner', {f'owner_{answer}': q},
                             {'policies': pack, 'tentative_owners': owners,
                              'source_realization_hypothesis': {'source': sid, 'target': answer},
                              'expression_evidence': self.scoped_evidence([answer]).get(answer, {})})[f'owner_{answer}']['choice']
            updated[answer] = reply
            self.ownership_policies[answer] = list(pack)
        elif answer == 'UNREPRESENTED':
            self.pending = [p for p in self.pending if not (p.get('reason') == 'invalid_or_unresolved_membership' and p.get('membership') == group)]
            self.validity_receipts.append({'source': [sid], 'target': [], 'kind': 'source_disposition',
                                           'answer': {'choice': 'YES', 'provider_evaluated': False, 'reason': 'explicit UNREPRESENTED source disposition'},
                                           'disposition_choice': answer, 'policies': deepcopy(pack)})
        elif answer == 'SHARED':
            questions = {f'joint_candidate_{sid}_{tid}': choice(
                f'Does English {tid} {self.target[tid]!r} jointly realize source {sid} '
                'with other source contributors in its actual expression? Current owners are hypotheses.',
                {'NO': 'Not a shared realization of this source.', 'YES': 'Shared realization includes this source.',
                 'UNRESOLVED': 'Cannot establish contribution.'}) for tid in self.target}
            replies = self.ask('source-shared-realization', questions, {'policies': pack, 'tentative_owners': owners})
            for tid in self.target:
                if replies[f'joint_candidate_{sid}_{tid}']['choice'] == 'YES':
                    updated[tid] = 'SHARED_PHRASE'
                    self.ownership_policies[tid] = list(pack)
        return updated

    def repair_joint(self, group, owners):
        key = group['id']
        selected = self.select_many('membership', {key: f"rejected joint proposal {group}"}, owners)
        old_ids = self.catalog.base(self.language) + self.unit_policies.get((tuple(group['source']), tuple(group['target'])), [])
        for tid in group['target']:
            old_ids.extend(self.ownership_policies.get(tid, []))
        pack = self.catalog.pack(old_ids + selected[key], self.language)
        if self.retrieval_pending(key):
            return dict(owners)
        if not self.evidence_ready(key, 'joint membership repair', pack, group['source']):
            return dict(owners)
        self.expression_evidence(group['target'], owners, pack)
        # Edge decisions preserve valid contributors instead of discarding a
        # rejected connected component en bloc. A new grouping still gets review.
        qs = {f'joint_member_{tid}_{sid}': choice(
            f'Within rejected joint proposal {group}, does source {sid} genuinely contribute '
            f'to English {tid} {self.target[tid]!r} in this actual expression? Other members '
            'can be wrong without invalidating this contribution. Do not decide status or display head.',
            {'YES': 'This source contribution is valid.', 'NO': 'This particular source contribution is invalid.',
             'UNRESOLVED': 'Cannot establish this contribution.'})
              for tid in group['target'] for sid in group['source']}
        replies = self.ask('joint-contributors', qs,
                           {'policies': pack, 'tentative_owners': owners,
                            'expression_evidence': self.scoped_evidence(group['target'])})
        updated = dict(owners)
        for tid in group['target']:
            values = {sid: replies[f'joint_member_{tid}_{sid}']['choice'] for sid in group['source']}
            if 'UNRESOLVED' in values.values():
                self.pending.append({'subject': tid, 'stage': 'membership', 'reason': 'unresolved_joint_contribution'})
                continue
            rejected = [sid for sid, verdict in values.items() if verdict == 'NO']
            self.ownership_policies[tid] = list(pack)
            if not rejected:
                continue
            options = {sid: option for sid, option in self.source_options().items() if sid not in rejected}
            q = repair_owner_question(tid, self.target, self.language.title(), options, ESCAPES)
            updated[tid] = self.ask('joint-owner', {f'owner_{tid}': q},
                                    {'policies': pack, 'tentative_owners': updated,
                                     'rejected_contributors': rejected,
                                     'expression_evidence': self.scoped_evidence([tid]).get(tid, {})})[f'owner_{tid}']['choice']
        return updated

    def strained_clauses(self, owners):
        """Clauses where word-by-word ownership shows strain, and the strained words.

        Strain is mechanical: one source owning non-adjacent English words, or
        two adjacent English words that no source owns.
        """
        order = list(self.target)
        spans = self.state.get('target_word_spans', {})
        clauses, current = [], []
        for tid in order:
            current.append(tid)
            if CLAUSE_BOUNDARY.search(spans.get(tid, {}).get('after', '')):
                clauses.append(current)
                current = []
        if current:
            clauses.append(current)
        position = {tid: i for i, tid in enumerate(order)}
        places = {}
        for tid in order:
            if owners.get(tid) in self.source:
                places.setdefault(owners[tid], []).append(position[tid])
        strained = {order[p] for found in places.values() if found[-1] - found[0] + 1 > len(found) for p in found}
        for a, b in zip(order, order[1:]):
            if owners.get(a) not in self.source and owners.get(b) not in self.source:
                strained.update((a, b))
        return [c for c in clauses if len(c) > 1 and strained & set(c)], strained

    def repair_paraphrases(self, owners):
        """Let a strained clause name a phrase that renders its source only as a whole.

        Whether a clause has such a phrase is asked first; the phrase and the
        source expression it renders are then forced choices, so neither is
        split against a NONE or UNRESOLVED option. Their units still pass the
        ordinary membership-validity check.
        A chosen phrase's words become SHARED_PHRASE; the ordinary contributor,
        membership, status and head decisions then build its unit.
        """
        clauses, strained = self.strained_clauses(owners)
        pack = self.catalog.pack([self.catalog.role('unit.noncompositional', self.language)], self.language)
        extra = {'policies': pack, 'tentative_owners': owners}
        phrases, presence, options = {}, {}, {}
        for clause in clauses:
            found = {}
            for i in range(len(clause)):
                for j in range(i + 1, min(len(clause), i + MAX_PARAPHRASE_WORDS)):
                    span = clause[i:j + 1]
                    if strained & set(span):
                        key = f'{span[0]}_{span[-1]}'
                        phrases[key] = span
                        found[key] = f"{span[0]}-{span[-1]}: {' '.join(self.target[t] for t in span)}"
            if not found:
                continue
            name = f'{clause[0]}_{clause[-1]}'
            options[name] = found
            words = ' '.join(self.target[t] for t in clause)
            presence[f'paraphrase_{name}'] = choice(
                f'Does the English clause {clause[0]}-{clause[-1]} {words!r} contain a phrase of two or more '
                f'words that renders its {self.language.title()} source only as a whole: an idiom or paraphrase '
                'whose words have no defensible individual correspondences? Ordinary grammatical helpers, '
                'supplied words, and phrases whose words each answer to a source word do not qualify. '
                'The tentative owners are hypotheses.',
                {'NO': 'Every word has a defensible individual correspondence or is a genuine addition.',
                 'YES': 'A phrase in this clause renders its source only as a whole.',
                 'UNRESOLVED': 'Cannot establish whether such a phrase is present.'})
        if not presence:
            return owners
        answers = self.ask('paraphrase-check', presence, extra)
        spans = {f'paraphrase_phrase_{name}': choice(
            f'Which phrase in the English clause {name.replace("_", "-")} renders its {self.language.title()} '
            'source only as a whole? Choose the smallest phrase whose words have no defensible individual '
            'correspondences.',
            options[name])
            for name in options if answers[f'paraphrase_{name}']['choice'] == 'YES'}
        if not spans:
            return owners
        chosen = [phrases[a['choice']] for a in self.ask('paraphrase-phrase', spans, extra).values()
                  if a['choice'] in phrases]
        if not chosen:
            return owners
        # A paraphrase renders a contiguous source expression; choosing that
        # expression as a whole avoids per-word contributor tests, which a
        # paraphrase cannot pass, and stray members from other clauses.
        order = list(self.source)
        expressions = {f'{order[i]}_{order[j]}': order[i:j + 1]
                       for i in range(len(order)) for j in range(i, min(len(order), i + MAX_SOURCE_EXPRESSION))}
        questions = {f'paraphrase_source_{span[0]}_{span[-1]}': choice(
            f"Which contiguous source expression does the English phrase {span[0]}-{span[-1]} "
            f"{' '.join(self.target[t] for t in span)!r} render as a whole? Include only actual contributors.",
            {key: f"{key.replace('_', '-')}: {' '.join(self.source[s]['surface'] for s in members)}"
             for key, members in expressions.items()})
            for span in chosen}
        replies = self.ask('paraphrase-source', questions, extra)
        updated = dict(owners)
        for span in chosen:
            members = expressions[replies[f'paraphrase_source_{span[0]}_{span[-1]}']['choice']]
            for tid in span:
                self.paraphrase_prior[tid] = owners[tid]
                updated[tid] = 'SHARED_PHRASE'
                self.ownership_policies[tid] = list(pack)
                self.paraphrase_members[tid] = set(members)
        # Words elsewhere that a paraphrase's source expression owned word by
        # word are asked again, with the paraphrase stated, so they join its
        # unit only if they still belong to that expression.
        claimed = {sid for members in self.paraphrase_members.values() for sid in members}
        displaced = [tid for tid in self.target if tid not in self.paraphrase_members and updated.get(tid) in claimed]
        if displaced:
            paraphrases = [{'target': span, 'source': sorted(self.paraphrase_members[span[0]])}
                           for span in chosen if span[0] in self.paraphrase_members]
            replies = self.ask('paraphrase-owners', {
                f'owner_{tid}': ownership_question(tid, self.target, self.language.title(), self.source_options(), ESCAPES)
                for tid in displaced}, {**extra, 'tentative_owners': updated, 'paraphrase_units': paraphrases})
            for tid in displaced:
                self.paraphrase_prior[tid] = updated[tid]
                updated[tid] = replies[f'owner_{tid}']['choice']
        return updated

    def shared_contributors(self, owners):
        subjects = {tid: f"joint source rendering of English {tid} {self.target[tid]!r}"
                    for tid, owner in owners.items() if owner == 'SHARED_PHRASE' and tid not in self.paraphrase_members}
        if not subjects:
            return {}
        selected = self.select_many('membership', subjects, owners, primary=False)
        planned, frames = [], []
        for tid in subjects:
            ids = self.catalog.base(self.language) + self.ownership_policies.get(tid, []) + selected[tid]
            ids += [self.catalog.role(pid, self.language) for pid in
                    ['membership.independent_units', 'unit.noncompositional']]
            pack = self.catalog.pack(ids, self.language)
            self.ownership_policies[tid] = list(pack)
            if not self.retrieval_pending(tid):
                self.expression_evidence([tid], owners, pack)
            for sid in self.source:
                subject = f'{tid}_{sid}'
                self.subject_scopes[subject] = {'source': [sid], 'target': [tid], 'kind': 'candidate_contributor'}
                frames.append((subject, 'shared contribution', pack, [sid],
                               {'tentative_owners': owners}))
                planned.append((tid, sid, subject, pack))
        readiness = self.evidence_ready_many(frames)
        answers, grouped = {}, {}
        for tid, sid, subject, pack in planned:
            qid = f'share_{tid}_{sid}'
            if self.retrieval_pending(tid) or not readiness[(subject, 'shared contribution')]:
                answers[qid] = {'choice': 'UNRESOLVED', 'provider_evaluated': False,
                                'reason': 'required contributor guidance or evidence unavailable'}
                continue
            extra = {'tentative_owners': owners, 'policies': pack,
                     'prior_owner_evidence': self.scoped_evidence([tid])}
            identity = compact(extra)
            if identity not in grouped:
                grouped[identity] = ({}, extra)
            grouped[identity][0][qid] = choice(
                f'Does source {sid} contribute to the joint source expression realized by '
                f"English {tid} '{self.target[tid]}'? Include only actual contributors, "
                'not a neighboring unrepresented article or unrelated word. Apply the supplied '
                'licensing guidance before rejecting a contributor; anchor evidence is a hypothesis.',
                {'YES': 'Contributes to this shared rendering.', 'NO': 'Does not contribute.',
                 'UNRESOLVED': 'Cannot determine contribution.'})
        calls = []
        for qs, extra in grouped.values():
            calls.extend(self.make_calls('shared-members', qs, extra))
        answers.update(self.dispatch(calls))
        return answers

    def memberships(self, owners):
        answers = self.shared_contributors(owners)
        allocations = {}
        for tid, owner in owners.items():
            if owner in self.source:
                allocations[tid] = {owner}
            elif owner == 'ADDED':
                allocations[tid] = set()
            elif tid in self.paraphrase_members:
                allocations[tid] = set(self.paraphrase_members[tid])
            elif owner == 'SHARED_PHRASE':
                row = {sid: answers[f'share_{tid}_{sid}']['choice'] for sid in self.source}
                members = {sid for sid, value in row.items() if value == 'YES'}
                if 'UNRESOLVED' in row.values() or len(members) < 2:
                    self.pending.append({'target': tid, 'reason': 'incomplete_shared_membership'})
                else:
                    allocations[tid] = members
            else:
                self.pending.append({'target': tid, 'reason': 'unresolved_owner'})
        # Connected components are proposed partitions, not accepted judgments.
        # Every proposed component gets a separate membership-validity decision.
        components = []
        for tid, src in allocations.items():
            if not src:
                components.append({'source': set(), 'target': {tid}})
                continue
            intersect = [c for c in components if c['source'] & src]
            merged = {'source': set(src), 'target': {tid}}
            for component in intersect:
                merged['source'].update(component['source'])
                merged['target'].update(component['target'])
                components.remove(component)
            components.append(merged)
        if self.language == 'hebrew':
            target_order = list(self.target)
            pairs = [(a, b) for a, b in zip(target_order, target_order[1:])
                     if owners.get(a) == owners.get(b) == 'ADDED']
            joins = self.ask('addition-boundaries', {
                f'join_{a}_{b}': choice(
                    f'Do adjacent unsupported English {a} and {b} jointly do one office '
                    'within a contiguous added expression? Decide from the actual clause, '
                    'not adjacency alone.',
                    {'YES': 'Part of the same added expression.', 'NO': 'Different offices.',
                     'UNRESOLVED': 'Cannot establish their grouping.'}) for a, b in pairs},
                {'tentative_owners': owners, 'policies': self.catalog.pack(['hebrew.added_expression'], self.language)})
            for a, b in pairs:
                verdict = joins[f'join_{a}_{b}']['choice']
                if verdict == 'UNRESOLVED':
                    self.pending.append({'target': [a, b], 'reason': 'unresolved_added_group'})
                elif verdict == 'YES':
                    left = next(c for c in components if not c['source'] and a in c['target'])
                    right = next(c for c in components if not c['source'] and b in c['target'])
                    if left is not right:
                        left['target'].update(right['target'])
                        components.remove(right)
        represented = set().union(*(c['source'] for c in components)) if components else set()
        components += [{'source': {sid}, 'target': set()} for sid in self.source if sid not in represented]
        source_order, target_order = list(self.source), list(self.target)
        return [{'id': f'u{i:03d}', 'source': [sid for sid in source_order if sid in c['source']],
                 'target': [tid for tid in target_order if tid in c['target']]}
                for i, c in enumerate(components, 1)]

    def finalize(self, groups, owners, check_membership=True):
        grouped_calls, local, evidence_blocked, unit_packs = {}, {}, set(), {}
        self.subject_scopes.update({u['id']: {'source': list(u['source']), 'target': list(u['target'])} for u in groups})
        stage_selections = {}
        known = {}
        for u in groups:
            ids = self.catalog.base(self.language) + self.unit_policies.get((tuple(u['source']), tuple(u['target'])), [])
            for tid in u['target']:
                ids.extend(self.ownership_policies.get(tid, []))
            if u['source'] and u['target']:
                ids.append(self.catalog.role('unit.noncompositional', self.language))
            if len(u['target']) > 1:
                ids.extend(self.catalog.role(pid, self.language) for pid in ['head.target_anchor', 'head.lexical_predicate'])
            if len(u['source']) > 1:
                ids.append(self.catalog.role('head.source_multiple', self.language))
            if self.language == 'hebrew':
                if len(u['source']) > 1:
                    ids.append('hebrew.source_head_fusion')
                if not u['source']:
                    ids.append('hebrew.added_expression')
            known[u['id']] = ids
        try:
            stage_selections['unit'] = self.select_many('unit', {
                u['id']: f"fixed unit {u}: membership, status, display heads and required explanations"
                for u in groups}, owners, primary=False, known=known)
        except GuidanceContextUnavailable as error:
            self.pending.append({'units': [u['id'] for u in groups], 'reason': 'complete_policy_context_exceeds_bound', 'detail': str(error)})
            return []
        planned = []
        for u in groups:
            uid, src, ts = u['id'], u['source'], u['target']
            policies = self.catalog.base(self.language)
            if src and ts:
                policies.append(self.catalog.role('unit.noncompositional', self.language))
            if len(ts) > 1:
                policies.extend(self.catalog.role(pid, self.language) for pid in ['head.target_anchor', 'head.lexical_predicate'])
            if len(src) > 1:
                policies.append(self.catalog.role('head.source_multiple', self.language))
            policies.extend(self.unit_policies.get((tuple(src), tuple(ts)), []))
            # Membership review and heads retain the actual licensing evidence
            # already supplied to the words in this unit.
            for tid in ts:
                policies.extend(self.ownership_policies.get(tid, []))
            if self.language == 'hebrew':
                if len(src) > 1:
                    policies.append('hebrew.source_head_fusion')
                if not src:
                    policies.append('hebrew.added_expression')
            for selections in stage_selections.values():
                policies.extend(selections.get(uid, []))
            pack = self.catalog.pack(policies, self.language)
            unit_packs[uid] = deepcopy(pack)
            planned.append((u, pack))
        try:
            evidence_outcomes = self.evidence_ready_many([
                (u['id'], 'final membership/head', pack, u['source']) for u, pack in planned])
        except GuidanceContextUnavailable as error:
            self.pending.append({'units': [u['id'] for u in groups], 'reason': 'complete_policy_context_exceeds_bound', 'detail': str(error)})
            return []
        null_heads = self.null_target_heads(planned, owners, evidence_outcomes)
        for u, pack in planned:
            uid, src, ts = u['id'], u['source'], u['target']
            if not evidence_outcomes[(uid, 'final membership/head')]:
                evidence_blocked.add(uid)
                continue
            questions = {f'valid_{uid}': choice(
                f'Is the complete proposed membership {u} defensible in this verse? '
                'Check all members jointly. Check genuine omissions and additions, not just shape. '
                'Reject a connected-component proposal if it merged separable contributions. '
                'Missing necessary policy guidance is UNRESOLVED.',
                {'YES': 'Membership is defensible.', 'NO': 'Membership is wrong.',
                 'UNRESOLVED': 'Evidence or necessary guidance is incomplete.'})}
            uncertain = any(p.get('subject') in [uid, *ts] or
                            any(tid in ts for tid in (p.get('target') if isinstance(p.get('target'), list) else [p.get('target')]))
                            for p in self.pending if p.get('reason') not in {'invalid_or_unresolved_membership'})
            if uncertain:
                local[f'valid_{uid}'] = {'choice': 'UNRESOLVED', 'provider_evaluated': False, 'reason': 'retained unresolved evidence or guidance'}
                questions.pop(f'valid_{uid}')
            elif len(src) == len(ts) == 1 and check_membership and not self.unit_policies.get((tuple(src), tuple(ts))):
                local[f'valid_{uid}'] = {'choice': 'YES', 'provider_evaluated': False, 'reason': 'explicit singleton owner judgment'}
                questions.pop(f'valid_{uid}')
            elif src and not ts:
                questions[f'valid_{uid}']['instructions'] = (
                    f"Does {self.language.title()} {src[0]} '{self.source[src[0]].get('surface', '')}' "
                    'have no independent English realization in this complete verse? This is a '
                    'source-only disposition. A legitimately unrendered function word is permitted. '
                    'Choose NO if a missing realization or shared phrase is needed.')
            if src and ts:
                questions[f'status_{uid}'] = choice(
                    f'Classify fixed unit {u}; do not change or discard membership in this decision. '
                    'Analytical phrases and licensed helpers remain aligned. '
                    'Reserve noncompositional for a genuinely indivisible rendering.',
                    {'aligned': 'Recoverable lexical or grammatical correspondence.',
                     'noncompositional': 'Indivisible expression.'})
            else:
                local[f'status_{uid}'] = {'choice': 'unrepresented' if src else 'added', 'provider_evaluated': False, 'probabilities': {}}
            for side, ids, values in [('source', src, self.source), ('target', ts, self.target)]:
                key = f'{side}_head_{uid}'
                if side == 'target' and uid in null_heads:
                    local[key] = {'choice': None, 'provider_evaluated': False,
                                  'reason': 'explicit declared omission/boundary disposition'}
                elif len(ids) > 1:
                    questions[key] = choice(
                        f'Choose the {side} display head within fixed unit {u}. '
                        'Choose one actual listed member; do not reconsider membership. '
                        'Apply the supplied lexical-head policies. A noncompositional source fallback '
                        'is permitted only when no unique lexical or syntactic head is defensible.',
                        {item: f'{item}: {values[item]}' for item in ids})
                    if side == 'target' and len(src) == 1:
                        questions[key] = headword_question(ids, self.target, self.language.title(), src[0], self.source[src[0]])
                else:
                    local[key] = {'choice': ids[0] if ids else None, 'provider_evaluated': False, 'probabilities': {}}
            scoped_evidence = self.scoped_evidence(ts)
            key = compact({'policies': pack, 'prior_owner_evidence': scoped_evidence})
            if key not in grouped_calls:
                grouped_calls[key] = ({}, {'proposed_units': groups, 'tentative_owners': owners, 'policies': pack,
                                                 'prior_owner_evidence': scoped_evidence})
            grouped_calls[key][0].update(questions)
        calls = []
        context_blocked = set(evidence_blocked)
        for i, (questions, extra) in enumerate(grouped_calls.values(), 1):
            # Use the same bounded splitter as every other ready frontier.
            try:
                calls.extend(self.make_calls(f'final-pack-{i:02d}', questions, extra))
            except ValueError:
                unit_ids = {qid.rsplit('_', 1)[1] for qid in questions}
                context_blocked.update(unit_ids)
                self.pending.append({'units': sorted(unit_ids), 'reason': 'complete_policy_context_exceeds_bound',
                                     'policies': list(extra['policies'])})
        calls = batch_ready(self, calls)
        results = self.backend.ask_many(calls)
        final_answers = {}
        for name, questions, extra in calls:
            answer = results.get(name)
            if answer is None or set(answer) != set(questions):
                raise ValueError('Incomplete final decision frontier.')
            for qid, q in questions.items():
                if answer[qid].get('choice') not in q['criteria']:
                    raise ValueError('Illegal final decision.')
            final_answers.update(answer)
            self.trace.append({'request_name': name, 'question_ids': list(questions), 'answers': deepcopy(answer), 'policies': list(extra['policies']),
                               'question_policies': {qid: list(question_context(q, extra).get('policies', {}))
                                                     for qid, q in questions.items()}})
        notes = self.notes_for_units(groups, {**local, **final_answers}, unit_packs, context_blocked)
        units = []
        for u in groups:
            if u['id'] in context_blocked:
                continue
            uid = u['id'];a = {**local, **final_answers}
            self.validity_receipts.append({'source': list(u['source']), 'target': list(u['target']),
                                           'answer': deepcopy(a[f'valid_{uid}']),
                                           'policies': deepcopy(unit_packs[uid]),
                                           'evidence': self.scoped_evidence(u['target'])})
            if a[f'valid_{uid}']['choice'] != 'YES':
                self.pending.append({'unit': uid, 'reason': 'invalid_or_unresolved_membership', 'membership': u})
            units.append({'source': u['source'], 'target': u['target'],
                          'source_head': a[f'source_head_{uid}']['choice'],
                          'target_head': a[f'target_head_{uid}']['choice'],
                          'status': a[f'status_{uid}']['choice'],
                          'note': notes.get(uid, '')})
        return units

    def notes_for_units(self, groups, answers, packs, blocked):
        notes = self.explain_alternatives(groups, answers, packs, blocked)
        mandatory = required_notes(self, groups, answers, packs, blocked)
        for uid, note in mandatory.items():
            notes[uid] = ' '.join(filter(None, [notes.get(uid, ''), note]))
        return notes

    def null_target_heads(self, planned, owners, readiness):
        calls, subjects = [], {}
        for u, pack in planned:
            uid = u['id']
            if u['source'] or not u['target'] or not readiness[(uid, 'final membership/head')]:
                continue
            for pid, policy in pack.items():
                condition = policy.get('null_target_head_condition')
                if not condition:
                    continue
                qid = f'null_head_{uid}_{pid}'
                subjects[qid] = uid
                calls.extend(self.make_calls('added-head-disposition', {qid: choice(
                    f'Does the declared prepared omission/boundary exception require a null '
                    f'target display head for this fixed unit {u}? Condition: {condition} '
                    'Do not change membership. Ordinary additions retain their normal anchor choice.',
                    {'NO': 'The special null-head condition does not apply.',
                     'YES': 'The declared exception requires a null target head.',
                     'UNRESOLVED': 'Cannot establish the declared exception.'})},
                    {'policies': pack, 'fixed_unit': u, 'selected_status': 'added',
                     'tentative_owners': owners, 'prior_owner_evidence': self.scoped_evidence(u['target'])}))
        replies = self.dispatch(calls)
        null = set()
        for qid, reply in replies.items():
            if reply['choice'] == 'YES':
                null.add(subjects[qid])
            elif reply['choice'] == 'UNRESOLVED':
                self.pending.append({'unit': subjects[qid], 'reason': 'unresolved_added_head_disposition'})
        return null

    def explain_alternatives(self, groups, answers, packs, blocked):
        """Tie notes depend on actual fixed heads, never on confidence thresholds."""
        calls, candidates_by_unit, contexts = [], {}, {}
        for u in groups:
            uid = u['id']
            if uid in blocked:
                continue
            pack = packs[uid]
            if not any('explanation' in self.catalog.policies.get(pid, {}).get('decision_types', []) for pid in pack):
                continue
            heads = {side: answers[f'{side}_head_{uid}']['choice'] for side in ('source', 'target')}
            candidates = {f'{side}:{item}': f'{side} member {item}: {values[item]}'
                          for side, values in [('source', self.source), ('target', self.target)]
                          for item in u[side] if item != heads[side]}
            if not candidates:
                continue
            context = {'policies': pack, 'fixed_unit': u, 'selected_heads': heads,
                       'selected_status': answers[f'status_{uid}']['choice'],
                       'prior_owner_evidence': self.scoped_evidence(u['target'])}
            contexts[uid], candidates_by_unit[uid] = context, candidates
            questions = {f'tie_{uid}': choice(
                f'With the actual selected display heads {heads} for fixed unit {u}, '
                'does a genuinely competing display-head reading require an explanatory note under '
                'the supplied rules? Decide from actual lexical/syntactic evidence, not confidence scores. '
                'Do not change membership, status, or the selected head.',
                {'NO': 'No genuine competing reading needs recording.',
                 'YES': 'A genuine competing reading needs recording.',
                 'UNRESOLVED': 'Cannot establish whether an explanation is required.'})}
            prepared, failed = prepare_note_jobs(self, 'head-tie-diagnostic', [(uid, questions, context)])
            if failed:
                self.pending.append({'unit': uid, 'reason': 'unresolved_head_explanation', 'detail': failed[uid]})
                contexts.pop(uid)
            calls.extend(prepared)
        ties = self.dispatch(calls)
        explanation_calls = []
        ready_frames = [(uid, 'head explanation', context['policies'], context['fixed_unit']['source'],
                         {**context, 'tie_diagnostic_answer': ties[f'tie_{uid}']})
                        for uid, context in contexts.items() if ties[f'tie_{uid}']['choice'] == 'YES']
        evidence_outcomes = self.evidence_ready_many(ready_frames)
        for uid, context in contexts.items():
            value = ties[f'tie_{uid}']['choice']
            if value == 'UNRESOLVED':
                self.pending.append({'unit': uid, 'reason': 'unresolved_head_explanation'})
            if value != 'YES' or not evidence_outcomes[(uid, 'head explanation')]:
                continue
            questions = {
                f'alternative_{uid}': choice(
                    f'Choose the actual competing member for fixed unit {context["fixed_unit"]}, '
                    f'given selected heads {context["selected_heads"]}. This records an alternative, '
                    'not a replacement head. Only offered actual members may be recorded.',
                    {**candidates_by_unit[uid], 'UNRESOLVED': 'No defensible competing member can be identified.'})}
            prepared, failed = prepare_note_jobs(self, 'head-alternative', [(uid, questions,
                {**context, 'tie_diagnostic_answer': ties[f'tie_{uid}']})])
            if failed:
                self.pending.append({'unit': uid, 'reason': 'unresolved_head_explanation', 'detail': failed[uid]})
            explanation_calls.extend(prepared)
        explained = self.dispatch(explanation_calls)
        reason_frames, reason_contexts = [], {}
        for uid, context in contexts.items():
            reply = explained.get(f'alternative_{uid}')
            if reply is None or reply['choice'] == 'UNRESOLVED':
                continue
            actual = {**context, 'selected_alternative': reply['choice'],
                      'alternative_answer': reply, 'tie_diagnostic_answer': ties[f'tie_{uid}']}
            reason_contexts[uid] = actual
            reason_frames.append((uid, 'head alternative reason', context['policies'],
                                  context['fixed_unit']['source'], actual))
        ready = self.evidence_ready_many(reason_frames)
        reason_calls = []
        for uid, actual in reason_contexts.items():
            if not ready[(uid, 'head alternative reason')]:
                continue
            questions = {f'alternative_reason_{uid}': choice(
                f'Which actual reason supports competing member {actual["selected_alternative"]} '
                f'against selected heads {actual["selected_heads"]} in fixed unit {actual["fixed_unit"]}? '
                'Use the supplied evidence for this returned competitor. Do not infer a tie from confidence.',
                {'LEXICAL_CONTENT': 'These members carry comparable lexical content.',
                 'SYNTACTIC_GOVERNOR': 'These syntactic-head analyses remain defensible.',
                 'DISPLAY_FALLBACK': 'This competing display fallback is defensible.',
                 'INSUFFICIENT_EVIDENCE': 'Available evidence does not distinguish these competitors.',
                 'UNRESOLVED': 'Cannot establish a defensible reason for this competitor.'})}
            prepared, failed = prepare_note_jobs(self, 'head-alternative-reason', [(uid, questions, actual)])
            if failed:
                self.pending.append({'unit': uid, 'reason': 'unresolved_head_explanation', 'detail': failed[uid]})
            reason_calls.extend(prepared)
        explained.update(self.dispatch(reason_calls))
        notes = {}
        for uid in contexts:
            if f'alternative_{uid}' not in explained:
                continue
            alternative = explained[f'alternative_{uid}']['choice']
            if f'alternative_reason_{uid}' not in explained:
                self.pending.append({'unit': uid, 'reason': 'unresolved_head_explanation'})
                continue
            reason = explained[f'alternative_reason_{uid}']['choice']
            if 'UNRESOLVED' in {alternative, reason}:
                self.pending.append({'unit': uid, 'reason': 'unresolved_head_explanation'})
                continue
            notes[uid] = f'Competing display head {alternative}; reason: {reason.lower().replace("_", " ") }.'
        return notes

    def run(self):
        try:
            return self._run()
        except GuidanceContextUnavailable as error:
            self.pending.append({'reason': 'complete_policy_context_exceeds_bound', 'detail': str(error)})
            proposal = {'ref': self.state['ref'], 'alignments': [], 'review_notes': []}
            return {'status': 'needs_review', 'proposal': proposal, 'pending': self.pending,
                    'validation_errors': validate_response(self.packet, proposal),
                    'initial_owners': getattr(self, 'initial_owners', {}),
                    'final_owners': getattr(self, 'current_owners', {}),
                    'decision_trace': self.trace, 'proposed_memberships': [],
                    'owner_scores': {}, 'validity_receipts': self.validity_receipts,
                    'repair_precheck_stats': deepcopy(self.repair_precheck_stats),
                    'score_semantics': 'Incomplete request; no new model scores inferred.',
                    'catalog_sha256': self.catalog.sha256, 'production_accepted': False}

    def _run(self):
        if not self.source:
            omission = self.required_context.get('evidence', {}).get('source_omission', {})
            if self.language != 'hebrew' or omission.get('resolution') != 'no_replacement' or not omission.get('reason_code'):
                raise ValueError('Empty source requires declared Hebrew source-omission resolution and provenance.')
            # The upstream declared omission fixes the whole verse disposition;
            # there are no competing source owners or head choices to query.
            unit = {'source': [], 'target': list(self.target), 'source_head': None,
                    'target_head': None, 'status': 'added',
                    'note': f"Prepared source-edition omission at {self.state['ref']}; resolution: no_replacement; reason: {omission['reason_code']}."}
            proposal = {'ref': self.state['ref'], 'alignments': [unit], 'review_notes': []}
            errors = validate_response(self.packet, proposal)
            owners = {tid: 'ADDED' for tid in self.target}
            return {'status': 'needs_review' if errors else 'complete_draft', 'proposal': proposal,
                    'pending': [], 'validation_errors': errors, 'initial_owners': owners,
                    'final_owners': owners, 'decision_trace': [{'kind': 'declared_source_omission',
                    'provider_evaluated': False, 'policy': 'hebrew.source_omission', 'evidence': omission}],
                    'proposed_memberships': [{'id': 'u001', 'source': [], 'target': list(self.target)}],
                    'owner_scores': {}, 'validity_receipts': [],
                    'score_semantics': 'Declared source omission: no source candidates or model probabilities.',
                    'catalog_sha256': self.catalog.sha256, 'production_accepted': False}
        initial = self.owners()
        self.initial_owners = deepcopy(initial)
        self.current_owners = deepcopy(initial)
        owners = self.repair_escapes(initial)
        owners = self.repair_determiners(owners)
        owners = self.repair_paraphrases(owners)
        self.current_owners = deepcopy(owners)
        groups = self.memberships(owners)
        groups, owners = self.prepare_with_article_refresh(groups, owners)
        self.current_owners = deepcopy(owners)
        units = self.finalize(groups, owners)
        repaired = self.repair_rejected(owners)
        self.current_owners = deepcopy(repaired)
        if repaired != owners:
            old_memberships = {(tuple(u['source']), tuple(u['target'])): u for u in units}
            groups = self.memberships(repaired)
            groups, repaired = self.prepare_with_article_refresh(groups, repaired)
            current = {(tuple(g['source']), tuple(g['target'])) for g in groups}
            self.pending = [p for p in self.pending
                            if p['reason'] != 'invalid_or_unresolved_membership' or
                            (tuple(p['membership']['source']), tuple(p['membership']['target'])) in current]
            untouched, changed = [], []
            for group in groups:
                prior = old_memberships.get((tuple(group['source']), tuple(group['target'])))
                if prior is not None:
                    untouched.append(prior)
                else:
                    changed.append(group)
            units = untouched + self.finalize(changed, repaired, check_membership=False)
        owners = repaired
        proposal = {'ref': self.state['ref'], 'alignments': units, 'review_notes': []}
        errors = validate_response(self.packet, proposal)
        scores = {}
        for record in self.trace:
            for qid, answer in record.get('answers', {}).items():
                if qid.startswith(('assign_', 'owner_')):
                    scores[qid.removeprefix('assign_').removeprefix('owner_')] = deepcopy(answer.get('probabilities', {}))
        return {'status': 'complete_draft' if not self.pending and not errors else 'needs_review',
                'proposal': proposal, 'pending': self.pending, 'validation_errors': errors,
                'initial_owners': initial, 'final_owners': owners, 'decision_trace': self.trace,
                'proposed_memberships': groups,
                'owner_scores': scores, 'validity_receipts': self.validity_receipts,
                'repair_precheck_stats': deepcopy(self.repair_precheck_stats),
                'score_semantics': 'Choice scores for each English occurrence over source candidates and escapes. Reverse display transposes these measured values; they are not calibrated accuracy or a separately normalized reverse distribution.',
                'catalog_sha256': self.catalog.sha256, 'production_accepted': False}


class HelperRunner:
    """Reuse established helpers while retaining bounded transport and trace."""
    def __init__(self, engine):
        self.engine, self.state = engine, engine.state

    def ask(self, name, questions, extra=None):
        return self.engine.ask(name, questions, extra)

    def ask_many(self, calls):
        # Each complete call retains its original evidence. The ready calls
        # are independent; splitting cannot merge their different contexts.
        ready = []
        original_evidence = {}
        names = {}
        for name, qs, extra in calls:
            if (extra or {}).get('article_evidence'):
                for qid in qs:
                    if qid.startswith('owner_'):
                        original_evidence[qid] = deepcopy(extra['article_evidence'])
            split = self.engine.make_calls(name, qs, extra)
            names[name] = [part[0] for part in split]
            ready.extend(split)
        answers = self.engine.backend.ask_many(ready)
        result = {}
        for name, qs, extra in ready:
            reply = answers.get(name)
            if reply is None or set(reply) != set(qs) or any(reply[q]['choice'] not in qs[q]['criteria'] for q in qs):
                raise ValueError('Incomplete helper decision frontier.')
            if (extra or {}).get('article_evidence'):
                for qid in qs:
                    if qid.startswith('owner_'):
                        self.engine.ownership_evidence[qid.removeprefix('owner_')] = deepcopy(original_evidence[qid])
            self.engine.trace.append({'request_name': name, 'question_ids': list(qs), 'answers': deepcopy(reply),
                                      'policies': list((extra or {}).get('policies', {}))})
        for name, parts in names.items():
            result[name] = {qid: answer for part in parts for qid, answer in answers[part].items()}
        return result


class PreparationComplete(Exception):
    pass


class RecordingBackend:
    """Captures one ready frontier without fabricating subsequent answers."""
    def __init__(self):
        self.calls = []

    def ask_many(self, calls):
        self.calls.extend(deepcopy(calls))
        raise PreparationComplete()
