"""Repair rejected members and test additions with bounded Jev decisions.

The scheduler uses saved unresolved units, additions, and question provenance.
It never uses the reference alignment or chooses a linguistic attachment.
"""
from copy import deepcopy
from request_runner import ask_independent
from common import ROOT,registry,read
from headwords import headword_question,fixed_status_question,assemble_fixed_unit,append_non_source_units,ownership_question


def extract_owner_evidence(request,language,origin):
    core=set(range(1,19)) if language=='Greek' else set(range(1,14))|{23,24}
    supplied=request['state'].get('full_rules',{})
    primary=request['state'].get('selected_governing_rule')
    ids={int(n) for n in supplied if str(n).isdigit() and int(n) not in core}
    if str(primary).isdigit():ids.add(int(primary))
    for n in ([7,9] if language=='Greek' else [11]):
        if str(n) in supplied:ids.add(n)
    return {'rule_ids':sorted(ids),'owner_response':origin,
            'prior_evidence':{k:request['state'][k] for k in ['article_evidence','expression_evidence','local_evidence'] if k in request['state']}}


def owner_rule_evidence(sample):
    """Complete rules and expression evidence previously supplied to ownership."""
    evidence={}
    for tid,path in sample['provenance']['owner_responses'].items():
        request=read(ROOT/'runs'/path.replace('response.json','request.json'))
        evidence[tid]=extract_owner_evidence(request,sample['language'].title(),path)
    return evidence


def finish_repairs(run,units,owners,pending,language):
    """Shared final stage for either language's adaptive controller."""
    rejected={item['source'] for item in pending if item.get('reason') in {'unresolved_membership','no_explicit_unrepresented_verdict'} and 'source' in item}
    evidence=read(run.output/'ownership-evidence.json') if (run.output/'ownership-evidence.json').exists() else {}
    repaired,new_owners,new_pending=refine(run,units,owners,rejected,language,evidence,
        carry=True,check_additions=True,check_status=True,stage='final-repair')
    unresolved=[]
    for item in pending:
        if item.get('source') in rejected or item.get('reason') in {'ADDED','UNRESOLVED','SHARED_PHRASE'}:continue
        unresolved.append(item)
    return repaired,new_owners,unresolved+new_pending


def rules_for(language,tids,evidence,carry):
    rules=registry(ROOT.parent/'config'/f'alignment-instructions-{language.lower()}.md')['rules']
    ids={7} if language=='Greek' else {11}
    if carry:
        for tid in tids:ids.update(evidence.get(tid,{}).get('rule_ids',[]))
    return {str(n):rules[n] for n in sorted(ids)}


def refine(run,units,owners,rejected,language,evidence,carry=False,check_additions=False,check_status=False,stage='repair'):
    source=run.state['source_tokens'];target=run.state['target_tokens'];owners=dict(owners)
    original=deepcopy(units);affected=set();pending=[]
    options={s:f"{s}: {v.get('surface','')}; lemma {v.get('lemma','')}; gloss {v.get('gloss','')}; morph {v.get('morph','')}" for s,v in source.items()}
    escapes={'ADDED':'No lexical or grammatical source licensing.',
             'SHARED_PHRASE':'Several source members are required.',
             'UNRESOLVED':'Cannot establish a defensible allocation.'}
    for sid in source:
        if sid not in rejected:continue
        ts=[t for t in target if owners.get(t)==sid];affected.add(sid)
        for tid in ts:
            q={'type':'choice','instructions':
               f"Evaluate tentative membership for {language} {sid}: {source[sid]}. Is English {tid} '{target[tid]}' nevertheless a valid member of this source token's rendering in the complete verse? "
               f"Other proposed members were {ts}. Test this occurrence's lexical or grammatical contribution, including licensed helpers, not dictionary identity alone. "
               'Evaluate each occurrence separately: a wrong other member does not invalidate this one. Identify the actual English expression this occurrence belongs to and compare the source expression. A matching gloss in another expression is not sufficient. Prior expression and article evidence are hypotheses to evaluate, not commands. Keep repeated words and expressions distinct. Do not decide status or display head.',
               'criteria':{'YES':'This particular membership is defensible.',
                           'NO':'This occurrence belongs elsewhere or has no licensing here.',
                           'UNRESOLVED':'Cannot establish this particular membership.'}}
            answer=run.ask(f'{stage}-member-{sid}-{tid}',{'member_'+tid:q},
                    {'full_rules':rules_for(language,[tid],evidence,carry),'tentative_owners':owners,
                     'licensing_provenance':evidence.get(tid,{}),
                     'prior_owner_evidence':evidence.get(tid,{}).get('prior_evidence',{}) if carry else {}})['member_'+tid]
            if answer['choice']=='YES':continue
            if answer['choice']=='UNRESOLVED':
                owners[tid]='UNRESOLVED';pending.append({'target':tid,'source':sid,'reason':'unresolved_member'});continue
            available={s:v for s,v in options.items() if s!=sid}
            q=ownership_question(tid,target,language,available,escapes)
            q['instructions']+=' The previous source was explicitly rejected for this occurrence; choose another defensible owner or a listed escape. Do not change other members.'
            reply=run.ask(f'{stage}-reassign-{tid}',{'owner_'+tid:q},
                {'full_rules':rules_for(language,[tid],evidence,carry),'tentative_owners':owners,
                 'rejected_source_for_this_occurrence':sid})['owner_'+tid]
            owners[tid]=reply['choice']
            if reply['choice'] in source:affected.add(reply['choice'])
    if check_additions:
        grammar=registry(ROOT.parent/'config'/f'alignment-instructions-{language.lower()}.md')['rules']
        for tid in target:
            if owners.get(tid)!='ADDED':continue
            q=ownership_question(tid,target,language,options,escapes)
            q['instructions']+=' Test the addition explicitly against grammatical licensing: subject person/number, tense, mood, aspect, voice, case, and construction can realize English without a separate dictionary word. ADDED requires absence of both lexical and grammatical licensing; a subjunctive may license an English modal. Preserve a genuine addition when none of the source choices licenses it.'
            ids=[7,9] if language=='Greek' else [11,17]
            reply=run.ask(f'{stage}-addition-{tid}',{'owner_'+tid:q},
               {'full_rules':{str(n):grammar[n] for n in ids},'tentative_owners':owners})['owner_'+tid]
            owners[tid]=reply['choice']
            if reply['choice'] in source:affected.add(reply['choice'])
    # Previously declared noncompositional units get a separate status-only
    # check in the rule-continuity phase. This cannot delete their members.
    if check_status:affected.update(s for u in original if u['status']=='noncompositional' for s in u['source'])
    result=[u for u in original if not any(s in affected for s in u['source']) and
            not (not u['source'] and any(owners.get(t)!='ADDED' for t in u['target']))]
    grammar=registry(ROOT.parent/'config'/f'alignment-instructions-{language.lower()}.md')['rules']
    classification_calls=[];fixed_members={};reused_heads={}
    for sid in source:
        if sid not in affected:continue
        ts=[t for t in target if owners.get(t)==sid];fixed_members[sid]=ts
        if not ts:
            q={'type':'choice','instructions':f"Does {language} {sid} '{source[sid].get('surface','')}' have no independent English realization in this complete verse? This is a source-only disposition, not a membership or headword choice. If a missing realization or shared phrase is needed, choose UNRESOLVED.",
                'criteria':{'unrepresented':'No independent English realization.', 'UNRESOLVED':'Missing realization or shared-source decision needed.'}}
            ids=[4,8] if language=='Greek' else [4,11]
            classification_calls.append((f'{stage}-source-only-{sid}',{'status_'+sid:q},
                 {'full_rules':{str(n):grammar[n] for n in ids},'tentative_owners':owners}))
        else:
            q=fixed_status_question(sid,ts,language)
            q['instructions']+=' A single source lexeme rendered analytically, including an ordinary lexical verb with its particle or licensed helpers, remains aligned; target word count and ordinary phrasal wording alone do not justify noncompositional.'
            classification_calls.append((f'{stage}-status-{sid}',{'status_'+sid:q},
                  {'full_rules':{str(n):grammar[n] for n in [2,6]},'tentative_owners':owners}))
            previous=next((u for u in original if u['source']==[sid] and u['target']==ts),None)
            if len(ts)>1:
                if previous and previous['target_head'] in ts:reused_heads['head_'+sid]={'choice':previous['target_head']}
                else:classification_calls.append((f'{stage}-head-{sid}',{'head_'+sid:headword_question(ts,target,language,sid,source[sid])},
                  {'full_rules':{str(17 if language=='Greek' else 24):grammar[17 if language=='Greek' else 24]},'tentative_owners':owners}))
    classified=ask_independent(run,classification_calls)
    # Assembly follows the original source order, after all independent replies.
    for sid,ts in fixed_members.items():
        name=f'{stage}-status-{sid}' if ts else f'{stage}-source-only-{sid}'
        answer=dict(classified[name])
        if len(ts)>1:
            if 'head_'+sid in reused_heads:answer['head_'+sid]=reused_heads['head_'+sid]
            else:answer.update(classified[f'{stage}-head-{sid}'])
        unit=assemble_fixed_unit(sid,ts,answer,pending)
        if unit is not None:result.append(unit)
    append_non_source_units(result,pending,owners,source,target)
    return result,owners,pending
