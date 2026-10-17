"""Bounded concurrency for complete, explicitly independent Jev requests.

Preparation/accounting run on the coordinator thread. Workers receive the
unchanged payload and only dispatch/read their own durable request directory.
No batching, prompt rewriting, alignment snapshots, or automatic retries.
"""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime,timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
from threading import BoundedSemaphore,Event

from translation_context import with_translation_context
from run_probe import MODEL, JevClient, execute_request
from run_store import FileStore

ROOT=Path(__file__).resolve().parent


def limit(value,label,maximum):
    if isinstance(value,bool):raise ValueError(f'{label} must be an integer from 1 to {maximum}')
    try:result=int(value)
    except (ValueError,TypeError):raise ValueError(f'{label} must be an integer from 1 to {maximum}') from None
    if str(result)!=str(value) or not 1<=result<=maximum:
        raise ValueError(f'{label} must be an integer from 1 to {maximum}')
    return result


# Shared by all verse runners in this Python process. A production web server
# needs a shared admission budget across PHP workers as well (see the docs).
PROCESS_LIMIT=limit(os.environ.get('JEV_PROCESS_MAX_IN_FLIGHT','8'),'JEV_PROCESS_MAX_IN_FLIGHT',32)
PROCESS_SLOTS=BoundedSemaphore(PROCESS_LIMIT)


def read(path):return json.loads(path.read_text())
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def compact(value):return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()


@dataclass(frozen=True)
class PreparedRequest:
    name: str
    directory: Path
    payload: dict
    body: bytes = b''


class IndependentRequestFailure(RuntimeError):
    def __init__(self,failures,completed):
        super().__init__('Independent request group failed; later work blocked; no automatic retry.')
        self.failures,self.completed=failures,completed


def ask_independent(run,calls):
    """Call sites declare independence; simple/fake backends retain serial use."""
    calls=list(calls)
    if not calls:return {}
    if hasattr(run,'ask_many'):return run.ask_many(calls)
    return {name:run.ask(name,questions,extra) for name,questions,extra in calls}


class ReceiptLedger:
    """Deduplicate appended receipts while scanning only the newly appended tail.

    Every reader/appender holds the existing cross-process lock. An inode
    replacement or truncation invalidates the local index and rebuilds it.
    """
    def __init__(self, path):
        self.path = path
        self.position, self.file_identity = 0, None
        self.ids, self.runs = set(), set()
        self.rows_scanned = 0

    def remember(self, record):
        if record.get('request_id'):
            self.ids.add(record['request_id'])
        self.runs.add((record.get('run_directory'), record.get('started_at')))

    def append(self, record):
        with self.path.open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            stat = os.fstat(handle.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if identity != self.file_identity or stat.st_size < self.position:
                self.position = 0
                self.ids.clear(); self.runs.clear()
                self.file_identity = identity
            handle.seek(self.position)
            for line in handle:
                if line.strip():
                    self.remember(json.loads(line))
                    self.rows_scanned += 1
            self.position = handle.tell()
            request_id = record.get('request_id')
            exists = (request_id in self.ids if request_id else
                      (record.get('run_directory'), record.get('started_at')) in self.runs)
            if not exists:
                handle.seek(0, 2)
                handle.write(json.dumps(record) + '\n')
                handle.flush()
                self.position = handle.tell()
                self.remember(record)
            return not exists


class Runner:
    cost_field = 'actual_cost_usd'
    model = MODEL
    ledger_name = 'cost-ledger.jsonl'
    # Research runs append every receipt to the ledger; None disables it.
    record_ledger = True
    planning_rate = Decimal('0.042')
    strategy_prefix = 'full-rules-'
    enforce_process_limit = True
    # Controllers may hand ask_many a continuation that predicts the next
    # ready frontier; its requests run ahead and are consumed only when the
    # real frontier later prepares byte-identical payloads under the same names.
    supports_speculation = True

    def __init__(self,output,state,execute,max_in_flight=None):
        self.output,self.state,self.execute=output,with_translation_context(state),execute
        configured=os.environ.get('JEV_MAX_IN_FLIGHT','3') if max_in_flight is None else max_in_flight
        self.max_in_flight=min(limit(configured,'JEV_MAX_IN_FLIGHT',8),PROCESS_LIMIT)
        self.receipts=[];self._seen_receipts=set();self._failed=False;self._cancelled=Event()
        self._ledger_index = None
        self.store = self.make_store()
        self.client = JevClient()
        self._pool=self._spec_pool=None
        self._speculative={};self._discarded=[]
        self.speculation_stats={'waves':0,'speculated':0,'consumed':0,'discarded':0,'failed':0}

    def make_store(self):
        return FileStore()

    def directory_key(self,directory):
        return str(directory.resolve()) if isinstance(self.store,FileStore) else str(directory)

    def _payload(self,questions,extra):
        self.state=with_translation_context(self.state)
        # The JSON round trip freezes the payload against ownership or caller
        # mutation after submission; it is exactly what the wire and the durable
        # request file carry.
        body=compact({'model':self.model,'state':{**self.state,**(extra or {})},'questions':questions})
        return body,json.loads(body)

    @staticmethod
    def _split_fixed(questions):
        # Single-option classification has no model judgment to make. Keep
        # these local answers unscored; membership validity is a separate call.
        fixed={qid:{'type':'choice','choice':next(iter(q['criteria'])),
                    'probabilities':{},'confidence':None,
                    'provider_evaluated':False,'determined_by':'single_allowed_choice'}
               for qid,q in questions.items()
               if q.get('type')=='choice' and len(q.get('criteria',{}))==1}
        return fixed,{qid:q for qid,q in questions.items() if qid not in fixed}

    def prepare_requests(self,name,questions,extra=None,directory=None):
        if not questions:raise ValueError(f'{name}: empty request')
        body,payload=self._payload(questions,extra)
        if len(body)+4096>64000:raise RuntimeError(f'{name}: aggregate bound exceeded; no rule omitted.')
        directory=(self.output/name) if directory is None else directory
        if self.store.exists(directory,'request.json') and self.store.read(directory,'request.json')!=payload:
            raise RuntimeError('Cached request differs; use a fresh experiment directory.')
        self.store.write(directory,'request.json',payload)
        return [PreparedRequest(name,directory,payload,body)]

    # Speculative frontier: requests run ahead under speculative/wave-NN/<name>.
    # Consumption moves a byte-identical request into its ordinary directory
    # before the real frontier prepares it, so the usual cached-receipt path,
    # validation, recording and failure semantics apply unchanged. Anything
    # not consumed by the very next frontier is discarded and still recorded
    # with its actual charge.
    @staticmethod
    def _await(entry):
        try:entry['future'].result()
        except Exception:pass

    def _discard(self,entry):
        self.speculation_stats['discarded']+=1;self._discarded.append(entry)

    def _consume_speculative(self,calls):
        if not self._speculative:return
        for name,questions,extra in calls:
            entry=self._speculative.pop(name,None)
            if entry is None:continue
            _,remaining=self._split_fixed(questions)
            body=self._payload(remaining,extra)[0] if remaining else None
            if body==entry['item'].body:
                self._await(entry)
                target=self.output/name
                if self.store.has_directory(target):raise RuntimeError('Speculative consumption target already exists.')
                self.store.rename(entry['item'].directory,target)
                self.speculation_stats['consumed']+=1
            else:self._discard(entry)
        for entry in list(self._speculative.values()):self._discard(entry)
        self._speculative.clear()

    def _launch_speculative(self,calls):
        calls=list(calls or [])
        if not calls or self._failed or self._cancelled.is_set():return
        planned=[]
        for name,questions,extra in calls:
            if name in self._speculative or self.store.has_directory(self.output/name):return
            _,remaining=self._split_fixed(questions)
            if remaining:planned.append((name,remaining,extra,self._payload(remaining,extra)[0]))
        if not planned or len({n for n,*_ in planned})!=len(planned):return
        known=sum((Decimal(str(r[self.cost_field])) for r in self.receipts if r.get(self.cost_field) is not None),Decimal(0))
        reserve=sum((Decimal(len(body)+4096)*self.planning_rate/Decimal(1000000) for *_,body in planned),Decimal(0))
        if known+reserve>Decimal('0.15'):return
        wave=self.speculation_stats['waves']+1
        items=[]
        try:
            for name,remaining,extra,_ in planned:
                items.extend(self.prepare_requests(name,remaining,extra,directory=self.output/'speculative'/f'wave-{wave:02d}'/name))
        except (ValueError,RuntimeError):
            return
        self.speculation_stats['waves']=wave
        if self._spec_pool is None:self._spec_pool=ThreadPoolExecutor(max_workers=self.max_in_flight)
        for item in items:
            self._speculative[item.name]={'item':item,'future':self._spec_pool.submit(self._dispatch,item)}
            self.speculation_stats['speculated']+=1

    def close(self):
        """Drain unconsumed speculative requests and record every charge."""
        for entry in list(self._speculative.values()):self._discard(entry)
        self._speculative.clear()
        for entry in self._discarded:
            if entry.get('recorded'):continue
            self._await(entry)
            directory=entry['item'].directory
            receipt=self.store.read(directory,'receipt.json') if self.store.exists(directory,'receipt.json') else None
            if receipt is not None and (receipt.get('status')!='completed' or receipt.get(self.cost_field) is None):
                self.speculation_stats['failed']+=1
            self._record(entry['item'],None);entry['recorded']=True
        if self.execute and (self.receipts or self.speculation_stats['waves']):
            self._write_progress()
        if self.execute and self.speculation_stats['waves']:
            self.store.write(self.output,'speculation.json',self.speculation_stats)
        for pool in (self._pool,self._spec_pool):
            if pool is not None:pool.shutdown(wait=True)
        self._pool=self._spec_pool=None

    def ask(self,name,questions,extra=None):
        return self.ask_many([(name,questions,extra)])[name]

    def _dispatch(self,item):
        with PROCESS_SLOTS if self.enforce_process_limit else nullcontext():
            if self._cancelled.is_set():raise RuntimeError('Request cancelled before dispatch after peer failure.')
            if not self.store.exists(item.directory,'receipt.json'):
                execute_request(item.directory, expected_questions=len(item.payload['questions']),
                                client=self.client, model=self.model, store=self.store)
            receipt=self.store.read(item.directory,'receipt.json')
            if receipt['status']!='completed' or receipt.get(self.cost_field) is None:
                raise RuntimeError('Failed or unknown-cost call; no retry.')
            answers=self.store.read(item.directory,'response.json')['answers']
            if set(answers)!=set(item.payload['questions']):raise RuntimeError('Missing or extra answers.')
            for qid,q in item.payload['questions'].items():
                if q['type']=='choice' and answers[qid].get('choice') not in q['criteria']:
                    raise RuntimeError('Illegal Choice answer.')
            return answers

    def _record(self,item,answers):
        if not self.store.exists(item.directory,'receipt.json'):return
        receipt=self.store.read(item.directory,'receipt.json');key=self.directory_key(item.directory)
        if key not in self._seen_receipts:
            self.receipts.append(receipt);self._seen_receipts.add(key)
        if self.record_ledger:
            ledger=ROOT/self.ledger_name
            # Each process reads historical rows once, then indexes only new rows
            # under the same lock, including failures and unknown-cost receipts.
            if self._ledger_index is None or self._ledger_index.path != ledger:
                self._ledger_index = ReceiptLedger(ledger)
            self._ledger_index.append({'recorded_at': datetime.now(timezone.utc).isoformat(),
                'ref': item.payload['state']['ref'], 'strategy': self.strategy_prefix + item.name,
                'run_directory': str(item.directory), **receipt})
        if answers and any(qid.startswith('owner_') for qid in answers):
            from decision_repairs import extract_owner_evidence
            name='ownership-evidence.json'
            evidence=self.store.read(self.output,name) if self.store.exists(self.output,name) else {}
            language='Hebrew' if str(item.payload['state'].get('contract','')).startswith('ot-hebrew') else 'Greek'
            for qid in answers:
                if qid.startswith('owner_'):
                    evidence[qid.removeprefix('owner_')]=extract_owner_evidence(item.payload,language,str(item.directory/'response.json'))
            self.store.write(self.output,name,evidence)
        print(item.name,len(item.payload['questions']),'questions; USD',receipt.get(self.cost_field),flush=True)

    def _write_progress(self):
        known=sum((Decimal(str(r[self.cost_field])) for r in self.receipts if r.get(self.cost_field) is not None),Decimal(0))
        self.store.write(self.output,'progress.json',{'requests':len(self.receipts),'questions':sum(r['question_count'] for r in self.receipts),
            self.cost_field:str(known),'unknown_cost_requests':sum(r.get(self.cost_field) is None for r in self.receipts),
            'max_in_flight':self.max_in_flight,
            'process_max_in_flight':PROCESS_LIMIT if self.enforce_process_limit else None})

    def ask_many(self,calls,speculate=None):
        if self._failed:raise RuntimeError('A previous request group failed; later work blocked.')
        calls=list(calls);names=[name for name,_,_ in calls]
        if len(names)!=len(set(names)):raise ValueError('Duplicate logical request names')
        if self.execute:self._consume_speculative(calls)
        prepared=[];groups=[];fixed_answers={}
        for name,questions,extra in calls:
            fixed,_=self._split_fixed(questions)
            fixed_answers[name]=fixed
            if fixed:
                self.store.write(self.output/name,'local-decisions.json',{'answers':fixed,'new_api_requests':0})
        # All normalization and durable request writes occur serially, exactly
        # as ask() would. A request remains separate even if contexts match.
        for name,questions,extra in calls:
            remaining={qid:q for qid,q in questions.items() if qid not in fixed_answers[name]}
            items=self.prepare_requests(name,remaining,extra) if remaining else []
            groups.append((name,items));prepared.extend(items)
        if len({self.directory_key(i.directory) for i in prepared})!=len(prepared):raise ValueError('Duplicate request directories')
        if not self.execute:return {name:None for name in names}
        # A known failed/ambiguous receipt must not launch fresh peer calls.
        invalid=[]
        for item in prepared:
            if self.store.exists(item.directory,'receipt.json'):
                receipt=self.store.read(item.directory,'receipt.json')
                if receipt.get('status')!='completed' or receipt.get(self.cost_field) is None:
                    invalid.append(item)
        if invalid:
            self._failed=True;self._cancelled.set()
            for item in invalid:self._record(item,None)
            self._write_progress()
            raise IndependentRequestFailure([{'name':i.name,'error':'Existing failed or unknown-cost receipt; no retry.'} for i in invalid],{})
        known=sum((Decimal(str(r[self.cost_field])) for r in self.receipts if r.get(self.cost_field) is not None),Decimal(0))
        reserve=sum((Decimal(len(compact(i.payload))+4096)*self.planning_rate/Decimal(1000000)
                     for i in prepared if not self.store.exists(i.directory,'receipt.json')),Decimal(0))
        if known+reserve>Decimal('0.15'):raise RuntimeError('Experiment spend bound reached before dispatch.')
        results={};failures=[];waiting=iter(prepared);attempted=set()
        if self._pool is None:self._pool=ThreadPoolExecutor(max_workers=self.max_in_flight)
        pool=self._pool
        running={}
        def fill():
            while len(running)<self.max_in_flight:
                item=next(waiting,None)
                if item is None:break
                running[pool.submit(self._dispatch,item)]=item;attempted.add(item.name)
        fill()
        if speculate is not None:
            # The continuation runs here, overlapping this frontier's network
            # time; it never delays submission of the real requests above.
            try:predicted=speculate()
            except Exception:predicted=[]
            self._launch_speculative(predicted)
        while running:
            done,_=wait(running,return_when=FIRST_COMPLETED)
            for future in done:
                item=running.pop(future)
                try:results[item.name]=future.result()
                except Exception as error:
                    failures.append({'name':item.name,'error':str(error)});self._cancelled.set()
            # No new pending request starts after a failure is observed;
            # finish collecting already running calls and their charges.
            if not failures:fill()
        # Merge and persist in original order, never response-completion order.
        try:
            for item in prepared:
                if item.name in attempted:self._record(item,results.get(item.name))
            # Progress is durable once per frontier, after every receipt of
            # that frontier is in the ledger, rather than once per request.
            self._write_progress()
        except Exception:
            self._failed=True;raise
        merged={}
        for name,items in groups:
            if all(i.name in results for i in items):
                model_answers={qid:a for i in items for qid,a in results[i.name].items()}
                answers={**fixed_answers[name],**model_answers}
                original=next(questions for call_name,questions,_ in calls if call_name==name)
                merged[name]={qid:answers[qid] for qid in original}
        if failures:
            self._failed=True;raise IndependentRequestFailure(failures,merged)
        return merged
