"""Bounded article repairs: code selects questions; Jev decides attachments."""
import json

from common import ROOT, registry
from headwords import expression_anchor_question, ownership_question
from request_runner import ask_independent


def repair_articles(run, owners, language, stage='article-repair', policy_pack=None, policy_packs=None, subjects=None):
    source=run.state['source_tokens'];target=run.state['target_tokens']
    tids=[t for t,word in target.items() if word.casefold() in {'the','a','an'} and (subjects is None or t in subjects)]
    if not tids:return dict(owners)
    anchor_questions={}
    order=list(target)
    for tid in tids:
        q=expression_anchor_question(tid,target)
        ordered_english=' | '.join(f'{t}: {target[t]}' for t in order)
        q['instructions']+=f' Full English verse in order: [{ordered_english}]. Identify the nominal expression determined by {tid}, distinguishing repeated nouns by occurrence. Consider the entire verse, including discontinuous expressions; source and English positions need not correspond.'
        anchor_questions[f'anchor_{tid}']=q
    if policy_pack is None:
        rules=registry(ROOT.parent/'config'/f'alignment-instructions-{language.lower()}.md')['rules']
        numbers=[11,15] if language=='Hebrew' else [7,8]
        guidance={'full_rules':{str(n):rules[n] for n in numbers}}
    else:
        guidance={'policies':policy_pack}
    guidance_by_tid={tid: ({'policies':policy_packs[tid]} if policy_packs is not None else guidance) for tid in tids}
    def focused_frontier(label, questions_by_tid, extra=None):
        packs={}
        for tid, questions in questions_by_tid.items():
            if not questions:continue
            context={**guidance_by_tid[tid],**(extra or {})}
            identity=json.dumps(context,ensure_ascii=False,sort_keys=True)
            if identity not in packs:packs[identity]=({},context)
            packs[identity][0].update(questions)
        if not packs:return {}
        if len(packs)==1:
            questions,context=next(iter(packs.values()))
            return run.ask(stage+'-'+label,questions,context)
        calls=[(stage+'-'+label+f'-pack-{i}',questions,context)
               for i,(questions,context) in enumerate(packs.values(),1)]
        replies=ask_independent(run,calls)
        return {qid:answer for name,_,_ in calls for qid,answer in replies[name].items()}
    anchors=focused_frontier('anchors',{tid:{f'anchor_{tid}':anchor_questions[f'anchor_{tid}']} for tid in tids})
    if anchors is None:return dict(owners)
    options={s:f"{s}: {v.get('surface','')}; lemma {v.get('lemma','')}; gloss {v.get('gloss','')}; morph {v.get('morph','')}" for s,v in source.items()}
    escapes={'ADDED':'No source lexeme or grammar licenses this article.',
             'SHARED_PHRASE':'Several source morphemes jointly license this rendering.',
             'UNRESOLVED':'Cannot defend an ownership choice.'}
    context=run.state.get('source_word_context',{})
    groups={sid:word for word in context.get('words',[]) for sid in word['members']}
    article_sources=[sid for sid,v in source.items() if v.get('lemma') in {'הַ','ὁ'} or v.get('morph')=='Td']
    gates={}
    for tid in tids:
        for sid in article_sources:
            host=groups.get(sid)
            host_label=str(host) if host else str(source[sid])
            if language=='Greek':
                host_label+='; full ordered source verse ['+' | '.join(f"{s}: {v.get('surface','')}; {v.get('morph','')}" for s,v in source.items())+']'
            anchor=anchors[f'anchor_{tid}']['choice']
            gates[f'eligible_{tid}_{sid}']={'type':'choice','instructions':
                f"Does source article {sid}, in source expression {host_label}, determine the source expression rendered by English article {tid} '{target[tid]}' with proposed English anchor {anchor} '{target.get(anchor,'unknown')}'? "
                'Test whether these are the same expression in this verse, not whether both articles mean the. '
                'An article attached to a different source noun cannot license this occurrence. The English anchor is a hypothesis to check.',
                'criteria':{'YES':'Same corresponding expression; article is eligible.',
                            'NO':'Different expression; this article is not eligible.',
                            'UNRESOLVED':'Cannot establish which expression it determines.'}}
    eligibility=focused_frontier('eligibility',{tid:{f'eligible_{tid}_{sid}':gates[f'eligible_{tid}_{sid}'] for sid in article_sources} for tid in tids},{'tentative_owners':owners}) if gates else {}
    updated=dict(owners)
    owner_calls=[]
    for tid in tids:
        anchor=anchors[f'anchor_{tid}']['choice']
        eligible_options={s:v for s,v in options.items() if eligibility.get(f'eligible_{tid}_{s}',{}).get('choice')!='NO'}
        q=ownership_question(tid,target,language,eligible_options,escapes)
        q['instructions']+=(' This is a determiner attachment check. Identify the nominal expression this English article determines. '
            'A present source article takes it only if that article belongs to the corresponding source expression; '
            'a matching article in another phrase is not eligible merely because its gloss says the. '
            'If the source expression has no article, evaluate the source nominal licensing the supplied determiner. '
            'Bracketed helper words in a gloss are explanatory expansions, not proof of lexical ownership. '
            'Repeated English articles are separate occurrences; one article must not take another phrase\'s determiner. '
            'The proposed English anchor and its source owner are hypotheses, not commands.')
        evidence={'english_article':tid,'proposed_anchor':anchor,
                  'tentative_anchor_owner':owners.get(anchor),'anchor_answer':anchors[f'anchor_{tid}'],
                  'source_article_eligibility':{s:eligibility[f'eligible_{tid}_{s}'] for s in article_sources},
                  'eligibility_guidance':guidance_by_tid[tid]}
        owner_calls.append((stage+'-owner-'+tid,{f'owner_{tid}':q},
                       {**guidance_by_tid[tid],'tentative_owners':owners,'article_evidence':evidence}))
    answers=ask_independent(run,owner_calls)
    for tid in tids:
        answer=answers[stage+'-owner-'+tid]
        updated[tid]=answer[f'owner_{tid}']['choice']
    return updated
