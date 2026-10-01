"""Disposable PostgreSQL tests of the real meetings plugin and publication barrier."""
import asyncio
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import socket
import threading
import time
import unittest
from uuid import uuid4
import psycopg
from core import Core, Registry
from core.db import PostgresStore
from core.db.migrate import migrate
from plugins.meetings import plugin
from plugins.meetings.checks import evidence_errors, consistency_errors

ADMIN = os.environ.get('TEST_DATABASE_ADMIN_URL')
APP = os.environ.get('TEST_DATABASE_URL')
TEXT = 'Иван сдаст отчёт в пятницу.'


def claim(value='пятница'):
    return {'source_index': 0, 'start': 0, 'quote': TEXT, 'text': TEXT,
            'subject': 'Отчёт Иван', 'predicate': 'срок', 'value': value}


class CheckTests(unittest.TestCase):
    def test_quotes_offsets_coverage_and_conflicts(self):
        entries = [{'text':TEXT,'status':'accepted'}]
        self.assertEqual(evidence_errors([claim()],entries),[])
        bad = claim(); bad['start'] = 1
        self.assertTrue(evidence_errors([bad],entries))
        bad = claim(); bad['quote'] = 'Выдуманная цитата'
        self.assertTrue(evidence_errors([bad],entries))
        self.assertTrue(evidence_errors([claim()],entries*2))
        self.assertTrue(consistency_errors([claim(),claim('понедельник')],[]))
        self.assertTrue(consistency_errors([claim()], [{'claims':[claim('понедельник')]}]))

    def test_registered_exports_use_one_contract(self):
        registry = Registry(); registry.register(plugin)
        self.assertEqual(len(registry._entries),9)
        self.assertEqual(len(Core.export_specs(plugin.tools(),'mcp')),9)


@unittest.skipUnless(ADMIN and APP, 'Requires disposable PostgreSQL')
class MeetingsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('TEST_DATABASE_DISPOSABLE') != 'yes':
            raise RuntimeError('Disposable database opt-in required')
        migrate(ADMIN,'ecosystem_app')

    def setUp(self):
        self.org, self.other = 'meetings-'+uuid4().hex, 'other-'+uuid4().hex
        with psycopg.connect(ADMIN) as conn:
            for org in (self.org,self.other):
                conn.execute('INSERT INTO ecosystem.organizations VALUES (%s,%s)',(org,org))
                conn.execute("INSERT INTO ecosystem.organization_domains VALUES (%s,'meetings',true)",(org,))
                for actor, scopes in (('author',['meetings:read','meetings:write','meetings:publish','meetings:review']),
                                      ('reviewer',['meetings:read','meetings:review']),('reader',['meetings:read'])):
                    conn.execute('INSERT INTO ecosystem.memberships(organization_id,actor_id,scopes) VALUES (%s,%s,%s)',(org,actor,scopes))
        self.store = PostgresStore(APP)
        registry = Registry(); registry.register(plugin)
        self.core = Core(registry,None,persistence=self.store)

    async def call(self, action, args, *, actor='author', org=None, key=None):
        return await self.core.dispatch(plugin,'meetings__'+action,args,
            {'organization_id':org or self.org,'actor_id':actor,'idempotency_key':key or uuid4().hex})

    def data(self, result):
        self.assertTrue(result['ok'],result)
        return result['data']

    async def batch(self, text=TEXT, external_id='recording-1', **kwargs):
        return self.data(await self.call('import_batch',{'source_account_id':'manual-plaud',
            'sources':[{'filename':'meeting.txt','format':'text/plain','text':text,'external_id':external_id}]},**kwargs))

    async def draft(self, batch=None, claims=None):
        batch = batch or await self.batch()
        return self.data(await self.call('submit_draft',{'batch_id':batch['batch_id'],
            'claims':claims or [claim()],'model_version':None,'prompt_version':None}))

    async def approve(self, run, actor='reviewer'):
        return await self.call('review_run',{'run_id':run['run_id'],'artifact_hash':run['artifact_hash'],
            'accuracy_pass':True,'consistency_pass':True,'coverage_confirmed':True,
            'rationale':'Все исходники и история просмотрены; смысл, полнота и контекст подтверждены.'},actor=actor)

    async def publish(self, run, **kwargs):
        return await self.call('publish',{'run_id':run['run_id'],'artifact_hash':run['artifact_hash']},**kwargs)

    async def test_full_review_publish_and_single_event(self):
        run = await self.draft()
        self.assertEqual(run['status'],'needs_review')
        self.assertEqual((await self.publish(run))['error']['code'],'CONFLICT')
        self.assertEqual((await self.approve(run,actor='author'))['error']['code'],'FORBIDDEN')
        reviewed = self.data(await self.approve(run))
        self.assertEqual([c['verdict'] for c in reviewed['checks']],['pass']*4)
        published = self.data(await self.publish(run,key='publish'))
        self.assertEqual(published['status'],'published')
        self.assertFalse(published['stale'])
        self.data(await self.publish(run,key='publish'))
        self.data(await self.publish(run,key='another-key'))
        with psycopg.connect(ADMIN) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM ecosystem.events WHERE organization_id=%s AND kind='meetings.published'",(self.org,)).fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT count(*) FROM meetings.publications WHERE organization_id=%s',(self.org,)).fetchone()[0],1)

    async def test_import_replay_duplicate_and_source_revision(self):
        batch = await self.batch(key='ingest')
        self.assertEqual(batch,await self.batch(key='ingest'))
        duplicate = await self.batch()
        self.assertEqual(duplicate['entries'][0]['status'],'duplicate')
        self.assertEqual(batch['entries'][0]['source_version_id'],duplicate['entries'][0]['source_version_id'])
        changed = await self.batch(text=TEXT+' Уточнение.')
        self.assertNotEqual(changed['entries'][0]['source_version_id'],batch['entries'][0]['source_version_id'])
        original = self.data(await self.call('get_batch',{'batch_id':batch['batch_id']}))
        self.assertEqual(original['entries'][0]['text'],TEXT)

    async def test_rejected_entry_visible_and_blocks_draft(self):
        result = await self.call('import_batch',{'source_account_id':'manual','sources':[
            {'filename':'good.txt','format':'text/plain','text':TEXT,'external_id':None},
            {'filename':'bad.pdf','format':'application/pdf','text':'opaque','external_id':None}]})
        batch = self.data(result)
        self.assertEqual(batch['status'],'blocked_ingestion')
        self.assertEqual([e['status'] for e in batch['entries']],['accepted','rejected'])
        draft = await self.call('submit_draft',{'batch_id':batch['batch_id'],'claims':[claim()],'model_version':None,'prompt_version':None})
        self.assertEqual(draft['error']['code'],'CONFLICT')

    async def test_bad_citation_cannot_be_overridden(self):
        bad = claim();bad['quote']='Не было сказано'
        run = await self.draft(claims=[bad])
        self.assertEqual(run['checks'][1]['verdict'],'fail')
        self.assertEqual((await self.approve(run))['error']['code'],'CONFLICT')
        self.assertEqual((await self.publish(run))['error']['code'],'CONFLICT')

    async def test_source_change_and_new_draft_invalidate_prior_passes(self):
        batch = await self.batch()
        old = await self.draft(batch)
        self.data(await self.approve(old))
        new = await self.draft(batch)
        self.assertEqual((await self.publish(old))['error']['code'],'CONFLICT')
        self.data(await self.approve(new))
        await self.batch(text=TEXT+' Новый текст.')
        self.assertEqual((await self.publish(new))['error']['code'],'CONFLICT')
        self.assertTrue(self.data(await self.call('get_run',{'run_id':new['run_id']}))['stale'])

    async def test_history_change_and_conflict(self):
        first = await self.draft(await self.batch(external_id='first'))
        second = await self.draft(await self.batch(external_id='second'))
        self.data(await self.approve(first));self.data(await self.approve(second))
        self.data(await self.publish(first))
        self.assertEqual((await self.publish(second))['error']['code'],'CONFLICT')
        conflict = await self.draft(await self.batch(external_id='third'),[claim('понедельник')])
        self.assertEqual(conflict['checks'][2]['verdict'],'fail')
        self.assertEqual((await self.approve(conflict))['error']['code'],'CONFLICT')

    async def test_tenant_isolation_permissions_and_rls(self):
        batch = await self.batch()
        run = await self.draft(batch)
        for action,args in (('get_batch',{'batch_id':batch['batch_id']}),('get_run',{'run_id':run['run_id']})):
            self.assertEqual((await self.call(action,args,org=self.other))['error']['code'],'NOT_FOUND')
        self.assertEqual((await self.call('publish',{'run_id':run['run_id'],'artifact_hash':run['artifact_hash']},actor='reader'))['error']['code'],'FORBIDDEN')
        async with self.store.session({'organization_id':self.other,'actor_id':'author'},'meetings',[],read_only=True) as (conn,_,_):
            self.assertEqual(await (await conn.execute('SELECT id FROM meetings.source_versions')).fetchall(),[])
        async with self.store.session({'organization_id':self.org,'actor_id':'author'},'meetings',[]) as (conn,_,_):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                await conn.execute("UPDATE meetings.source_versions SET text_content='tampered'")

    async def test_database_rejects_cross_domain_anchor(self):
        item_id = str(uuid4())
        with psycopg.connect(ADMIN) as conn:
            conn.execute("INSERT INTO ecosystem.organization_domains VALUES (%s,'tasks',true)",(self.org,))
            conn.execute("INSERT INTO ecosystem.items(organization_id,id,domain,kind,created_by) VALUES (%s,%s,'tasks','task','author')",(self.org,item_id))
        async with self.store.session({'organization_id':self.org,'actor_id':'author'},'meetings',[]) as (conn,_,_):
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                await conn.execute("INSERT INTO meetings.batches(organization_id,id,source_account_id,manifest_hash,status) VALUES (%s,%s,'manual','hash','ingested')",(self.org,item_id))

    async def test_concurrent_import_creates_one_source_version(self):
        first, second = await asyncio.gather(self.batch(),self.batch())
        self.assertEqual(first['entries'][0]['source_version_id'],second['entries'][0]['source_version_id'])
        self.assertEqual({first['entries'][0]['status'],second['entries'][0]['status']},{'accepted','duplicate'})

    async def test_concurrent_publications_recheck_history(self):
        first = await self.draft(await self.batch(external_id='first'))
        second = await self.draft(await self.batch(external_id='second'))
        self.data(await self.approve(first));self.data(await self.approve(second))
        results = await asyncio.gather(self.publish(first),self.publish(second))
        self.assertEqual(sum(r['ok'] for r in results),1)
        self.assertEqual(next(r for r in results if not r['ok'])['error']['code'],'CONFLICT')

    async def test_cabinet_page_cursor_and_tenant_projection(self):
        batches=[await self.batch(external_id=str(i)) for i in range(3)]
        first=self.data(await self.call('list_batches',{'cursor':None,'limit':2}))
        second=self.data(await self.call('list_batches',{'cursor':first['next_cursor'],'limit':2}))
        self.assertEqual(len(first['records']),2)
        self.assertEqual(len(second['records']),1)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual({r['batch_id'] for r in first['records']+second['records']},{b['batch_id'] for b in batches})
        self.assertNotIn('text',first['records'][0])
        self.assertEqual(self.data(await self.call('list_batches',{'cursor':None,'limit':20},org=self.other))['records'],[])
        bad=await self.call('list_batches',{'cursor':'malformed','limit':20})
        self.assertEqual(bad['error']['code'],'INVALID_ARGUMENT')
        bad=await self.call('list_batches',{'cursor':None,'limit':1000})
        self.assertEqual(bad['error']['code'],'INVALID_ARGUMENT')

    async def test_http_timeout_503_rollback_and_same_key_retry(self):
        import httpx2
        from server.app import create_app
        from server.auth import TokenVerifier
        token = uuid4().hex
        verifier = TokenVerifier([{'sha256':hashlib.sha256(token.encode()).hexdigest(), 'expires_at':time.time()+60,
            'organization_id':self.org,'sub':'author','scope':'meetings:read meetings:write'}])
        app = create_app(self.core,[plugin],verifier)
        body = {'name':'meetings__import_batch','arguments':{'source_account_id':'timeout-fixture','sources':[
            {'filename':'fixture.txt','format':'text/plain','text':TEXT,'external_id':'fixture'}]}}
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url='http://localhost',trust_env=False,
                headers={'Authorization':'Bearer '+token,'Idempotency-Key':'retry-after-timeout'}) as http:
            async with await psycopg.AsyncConnection.connect(ADMIN) as lock:
                await lock.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,51477))',(self.org,))
                self.core.timeout = .25
                response = await http.post('/meetings/v1/tools/invoke',json=body)
                self.assertEqual(response.status_code,503,response.text)
                self.assertEqual(response.json()['error']['code'],'DEPENDENCY_UNAVAILABLE')
                self.assertTrue(response.json()['error']['retryable'])
                self.core.timeout = 10
                with psycopg.connect(ADMIN) as conn:
                    for table in ('items','idempotency','events','outbox'):
                        self.assertEqual(conn.execute('SELECT count(*) FROM ecosystem.'+table+' WHERE organization_id=%s',(self.org,)).fetchone()[0],0)
            first = await http.post('/meetings/v1/tools/invoke',json=body)
            again = await http.post('/meetings/v1/tools/invoke',json=body)
            self.assertEqual(first.status_code,200,first.text)
            self.assertEqual(first.json(),again.json())
            with psycopg.connect(ADMIN) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM meetings.batches WHERE organization_id=%s',(self.org,)).fetchone()[0],1)

    async def test_mcp_client_uses_real_domain_and_database(self):
        import httpx2
        import uvicorn
        from mcp.client import Client
        from mcp.client.streamable_http import streamable_http_client
        from server.app import create_app
        from server.auth import TokenVerifier
        token = uuid4().hex
        verifier = TokenVerifier([{'sha256':hashlib.sha256(token.encode()).hexdigest(), 'expires_at':time.time()+60,
            'organization_id':self.org,'sub':'author','scope':'meetings:read meetings:write'}])
        app = create_app(self.core,[plugin],verifier)
        sock = socket.socket();sock.bind(('127.0.0.1',0))
        server = uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False,proxy_headers=False))
        thread = threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            for _ in range(200):
                if server.started:break
                await asyncio.sleep(.01)
            self.assertTrue(server.started)
            async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+token,'Idempotency-Key':'mcp-import'},trust_env=False) as http:
                async with Client(streamable_http_client(f'http://127.0.0.1:{sock.getsockname()[1]}/meetings/mcp',http_client=http),read_timeout_seconds=10) as client:
                    names = [t.name for t in (await client.list_tools()).tools]
                    self.assertIn('meetings__import_batch',names)
                    self.assertNotIn('meetings__publish',names)
                    result = await client.call_tool('meetings__import_batch',{'source_account_id':'mcp','sources':[
                        {'filename':'meeting.txt','format':'text/plain','text':TEXT,'external_id':'mcp-1'}]})
                    self.assertFalse(result.is_error,result)
                    batch = result.structured_content
                    fetched = await client.call_tool('meetings__get_batch',{'batch_id':batch['batch_id']})
                    self.assertEqual(fetched.structured_content['entries'][0]['text'],TEXT)
        finally:
            server.should_exit=True
            await asyncio.to_thread(thread.join,10)
            sock.close()
            self.assertFalse(thread.is_alive())
