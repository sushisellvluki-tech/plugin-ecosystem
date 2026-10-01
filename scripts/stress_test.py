"""Bounded synthetic HTTP/MCP load against an explicitly disposable PostgreSQL DB."""
import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import socket
import threading
import time
from uuid import uuid4
import httpx2
import psycopg
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from core import Core, Registry
from core.db import PostgresStore
from core.db.migrate import migrate
from plugins.meetings import plugin
from server.app import create_app
from server.auth import TokenVerifier


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered)*fraction)-1)]*1000, 2) if ordered else 0


@asynccontextmanager
async def serving(app):
    sock = socket.socket(); sock.bind(('127.0.0.1',0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, proxy_headers=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(.01)
        require(server.started, 'Loopback server failed to start')
        yield url
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        sock.close()
        require(not thread.is_alive(), 'Loopback server failed to stop')


class Load:
    def __init__(self, admin, app_dsn, organizations, batches, concurrency):
        self.admin, self.app_dsn = admin, app_dsn
        self.organizations, self.batches, self.concurrency = organizations, batches, concurrency
        self.sem = asyncio.Semaphore(concurrency)
        self.samples, self.outcomes = [], Counter()
        self.active, self.peak = 0, 0
        self.orgs = ['stress-'+uuid4().hex for _ in range(organizations)]
        self.tokens = {(org,actor):uuid4().hex+uuid4().hex for org in self.orgs for actor in ('author','reviewer')}

    def provision(self):
        keys = []
        with psycopg.connect(self.admin) as conn:
            for org in self.orgs:
                conn.execute('INSERT INTO ecosystem.organizations VALUES (%s,%s)', (org,'Synthetic load tenant'))
                conn.execute("INSERT INTO ecosystem.organization_domains VALUES (%s,'meetings',true)",(org,))
                for actor in ('author','reviewer'):
                    scopes = ['meetings:read','meetings:write','meetings:publish'] if actor=='author' else ['meetings:read','meetings:review']
                    conn.execute('INSERT INTO ecosystem.memberships(organization_id,actor_id,scopes) VALUES (%s,%s,%s)',(org,actor,scopes))
                    keys.append({'sha256':hashlib.sha256(self.tokens[org,actor].encode()).hexdigest(),
                        'expires_at':time.time()+900,'organization_id':org,'sub':actor,'scope':' '.join(scopes)})
        registry = Registry(); registry.register(plugin)
        return create_app(Core(registry,None,persistence=PostgresStore(self.app_dsn)),[plugin],TokenVerifier(keys))

    async def measure(self, label, request):
        # Timings start after the semaphore; report wall throughput separately.
        async with self.sem:
            self.active += 1; self.peak = max(self.peak,self.active)
            start = time.perf_counter()
            try:
                return await request()
            finally:
                self.samples.append((label,time.perf_counter()-start))
                self.active -= 1

    async def http(self, client, org, action, arguments, *, actor='author', key=None, expected='OK'):
        async def request():
            response = await client.post('/meetings/v1/tools/invoke',
                headers={'Authorization':'Bearer '+self.tokens[org,actor], 'Idempotency-Key':key or uuid4().hex},
                json={'name':'meetings__'+action,'arguments':arguments})
            result = response.json()
            code = 'OK' if result.get('ok') is True else result.get('error',{}).get('code','BAD_RESPONSE')
            self.outcomes[code] += 1
            require(code == expected, f'{action}: expected {expected}, received {code}')
            status = {'OK':200,'CONFLICT':409,'NOT_FOUND':404,'FORBIDDEN':403}.get(expected)
            require(response.status_code == status, f'{action}: incorrect HTTP status {response.status_code}')
            return result['data'] if expected=='OK' else result
        return await self.measure('http:'+action,request)

    async def run(self):
        app = self.provision()
        started = time.perf_counter()
        expected_batches = self.organizations*self.batches*2
        async with serving(app) as url:
            async with httpx2.AsyncClient(base_url=url, trust_env=False, timeout=20,
                    limits=httpx2.Limits(max_connections=self.concurrency,max_keepalive_connections=self.concurrency)) as client:
                items = []
                for org in self.orgs:
                    for number in range(self.batches):
                        quote = f'Синтетическая задача {number}: срок пятница.'
                        text = quote+'\n'+('Обсуждение синтетической встречи. '*80)
                        arguments = {'source_account_id':'synthetic','sources':[{'filename':f'{number}.txt',
                            'format':'text/plain','text':text,'external_id':str(number)}]}
                        items.append({'org':org,'number':number,'quote':quote,'arguments':arguments,'key':'import-'+str(number)})
                # Three simultaneous identical requests must reserve just one batch.
                jobs = [(i,attempt) for i in range(len(items)) for attempt in range(3)]
                random.Random(42).shuffle(jobs)
                async def ingest(index, attempt):
                    item = items[index]
                    return index, await self.http(client,item['org'],'import_batch',item['arguments'],key=item['key'])
                ingested = await asyncio.gather(*(ingest(*job) for job in jobs))
                by_index = {}
                for index,result in ingested:
                    require(index not in by_index or by_index[index]==result, 'Concurrent idempotent replies differ')
                    by_index[index] = result
                for index,item in enumerate(items):
                    item['batch'] = by_index[index]
                # New request keys record a new batch occurrence, not a source version.
                duplicates = await asyncio.gather(*(self.http(client,i['org'],'import_batch',i['arguments']) for i in items))
                for item,duplicate in zip(items,duplicates):
                    require(duplicate['entries'][0]['status']=='duplicate','Unrecognized duplicate source')
                    require(duplicate['entries'][0]['source_version_id']==item['batch']['entries'][0]['source_version_id'],'Duplicate source version created')
                async def conflicts(item):
                    body = json.loads(json.dumps(item['arguments']))
                    body['sources'][0]['text'] += ' Changed.'
                    await self.http(client,item['org'],'import_batch',body,key=item['key'],expected='CONFLICT')
                await asyncio.gather(*(conflicts(i) for i in items))
                # Cross-tenant reads must return the same NOT_FOUND as absent records.
                await asyncio.gather(*(self.http(client,self.orgs[(self.orgs.index(i['org'])+1)%self.organizations],
                    'get_batch',{'batch_id':i['batch']['batch_id']},expected='NOT_FOUND') for i in items))
                # Prepare two independently reviewed candidates at the same history revision.
                selected = [i for i in items if i['number']<2]
                async def draft(item):
                    claim = {'source_index':0,'start':0,'quote':item['quote'],'text':item['quote'],
                        'subject':'Task '+str(item['number']),'predicate':'due','value':'Friday'}
                    return await self.http(client,item['org'],'submit_draft',{'batch_id':item['batch']['batch_id'],
                        'claims':[claim],'model_version':None,'prompt_version':None})
                runs = await asyncio.gather(*(draft(i) for i in selected))
                await asyncio.gather(*(self.http(client,i['org'],'review_run',{'run_id':r['run_id'],
                    'artifact_hash':r['artifact_hash'],'accuracy_pass':True,'consistency_pass':True,
                    'coverage_confirmed':True,'rationale':'Synthetic fixture attestation, not a review of real business data.'},actor='reviewer')
                    for i,r in zip(selected,runs)))
                # Conflict outcome is intentionally nondeterministic; one winner per org.
                async def publish(item,run):
                    async def request():
                        response = await client.post('/meetings/v1/tools/invoke',
                            headers={'Authorization':'Bearer '+self.tokens[item['org'],'author'],'Idempotency-Key':'publish-'+run['run_id']},
                            json={'name':'meetings__publish','arguments':{'run_id':run['run_id'],'artifact_hash':run['artifact_hash']}})
                        result=response.json();code='OK' if result.get('ok') else result.get('error',{}).get('code')
                        require((code,response.status_code) in (('OK',200),('CONFLICT',409)), 'Publication race returned unexpected error')
                        self.outcomes[code]+=1
                        return item,run,result
                    return await self.measure('http:publish',request)
                publications = await asyncio.gather(*(publish(i,r) for i,r in zip(selected,runs)))
                for org in self.orgs:
                    require(sum(result['ok'] for item,_,result in publications if item['org']==org)==1,'Publication barrier allowed zero or multiple winners')
                for item,run,result in publications:
                    if result['ok']:
                        replay=await self.http(client,item['org'],'publish',{'run_id':run['run_id'],'artifact_hash':run['artifact_hash']},key='publish-'+run['run_id'])
                        require(replay==result['data'],'Publication replay differs')
                # Read the same DB objects through independently authenticated SDK clients.
                async def mcp_org(org):
                    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+self.tokens[org,'author']},trust_env=False,timeout=20) as http:
                        async with Client(streamable_http_client(url+'/meetings/mcp',http_client=http),read_timeout_seconds=20) as mcp:
                            async def read(item):
                                async def request():
                                    result=await mcp.call_tool('meetings__get_batch',{'batch_id':item['batch']['batch_id']})
                                    require(not result.is_error,'MCP read failed')
                                    require(result.structured_content['entries'][0]['text']==item['arguments']['sources'][0]['text'],'MCP text or tenant mismatch')
                                    self.outcomes['OK']+=1
                                await self.measure('mcp:get_batch',request)
                            await asyncio.gather(*(read(i) for i in items if i['org']==org))
                await asyncio.gather(*(mcp_org(org) for org in self.orgs))
        elapsed = time.perf_counter()-started
        # Count actual effects independently of API responses, including outbox links.
        with psycopg.connect(self.admin) as conn:
            counts = {table:conn.execute('SELECT count(*) FROM meetings.'+table+' WHERE organization_id=ANY(%s)',(self.orgs,)).fetchone()[0]
                for table in ('batches','source_versions','runs','publications')}
            published_events = conn.execute("SELECT count(*) FROM ecosystem.events WHERE organization_id=ANY(%s) AND kind='meetings.published'",(self.orgs,)).fetchone()[0]
            missing_outbox = conn.execute('SELECT count(*) FROM ecosystem.events e LEFT JOIN ecosystem.outbox o ON o.organization_id=e.organization_id AND o.event_id=e.id WHERE e.organization_id=ANY(%s) AND o.id IS NULL',(self.orgs,)).fetchone()[0]
        require(counts=={'batches':expected_batches,'source_versions':len(items),'runs':self.organizations*2,'publications':self.organizations},'Database side effect counts differ')
        require(published_events==self.organizations and missing_outbox==0,'Publication event/outbox invariant failed')
        timings=[s for _,s in self.samples]
        return {'concurrency':self.concurrency,'peak_in_flight':self.peak,'organizations':self.organizations,
            'unique_inputs':len(items),'input_text_utf8_bytes':len(items[0]['arguments']['sources'][0]['text'].encode()),
            'operations':len(timings),'wall_seconds':round(elapsed,3),'operations_per_second':round(len(timings)/elapsed,2),
            'latency_ms':{'p50':percentile(timings,.5),'p95':percentile(timings,.95),'max':round(max(timings)*1000,2)},
            'outcomes':dict(self.outcomes),'database_counts':counts,'missing_outbox':missing_outbox,
            'invariants':'passed','by_operation':{label:{'count':sum(n==label for n,_ in self.samples),
                'p95_ms':percentile([t for n,t in self.samples if n==label],.95)} for label in sorted({n for n,_ in self.samples})}}


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--levels',default='1,8,32,64')
    parser.add_argument('--organizations',type=int,default=4)
    parser.add_argument('--batches',type=int,default=8)
    parser.add_argument('--report',type=Path,default=Path('stress-report.json'))
    args=parser.parse_args()
    levels=[int(v) for v in args.levels.split(',')]
    if not levels or len(levels)>5 or any(not 1<=n<=64 for n in levels) or not 2<=args.organizations<=8 or not 2<=args.batches<=16:
        parser.error('Bounds: 1-5 levels of 1-64 requests, 2-8 tenants, 2-16 batches each')
    admin,app=os.environ.get('TEST_DATABASE_ADMIN_URL'),os.environ.get('TEST_DATABASE_URL')
    if os.environ.get('TEST_DATABASE_DISPOSABLE')!='yes' or not admin or not app:
        parser.error('Explicit disposable test DB opt-in and both TEST_DATABASE URLs required')
    report={'status':'running','synthetic_only':True,'python':platform.python_version(),
        'logical_cpus':os.cpu_count(),'levels':[],
        'limitations':['Loopback and disposable PostgreSQL; not a production SLA or capacity limit',
            'No model calls, Plaud parser, WAN/TLS, proxy, soak test or process-kill fault injection',
            'Latency excludes client semaphore wait; throughput includes protocol setup and validation']}
    try:
        migrate(admin,'ecosystem_app')
        for level in levels:
            result=await Load(admin,app,args.organizations,args.batches,level).run()
            report['levels'].append(result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
        report['status']='passed'
    except Exception as exc:
        report['status']='failed'
        report['failure_type']=type(exc).__name__
        report['failure']=str(exc) if isinstance(exc,AssertionError) else 'Unexpected failure; inspect protected CI diagnostics'
    finally:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print('STRESS_RESULT '+json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
