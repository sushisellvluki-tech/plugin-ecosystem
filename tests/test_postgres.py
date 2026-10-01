"""Integration suite: requires disposable PostgreSQL, never run against production."""
import asyncio
from copy import deepcopy
import os
from types import SimpleNamespace
import unittest
from uuid import uuid4
import psycopg

from core import Core, Registry
from core.db import PostgresStore
from core.db.migrate import migrate
from core.runtime import failure

ADMIN=os.environ.get('TEST_DATABASE_ADMIN_URL')
APP=os.environ.get('TEST_DATABASE_URL')
SCHEMA={'type':'object','properties':{'value':{'type':'string'}},'required':['value'],'additionalProperties':False}
OUT={'type':'object','properties':{'id':{'type':'string'}},'required':['id'],'additionalProperties':False}


@unittest.skipUnless(ADMIN and APP,'Set TEST_DATABASE_ADMIN_URL and TEST_DATABASE_URL for PostgreSQL integration tests')
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Explicit guard: this test suite provisions fixtures in a throwaway database.
        if os.environ.get('TEST_DATABASE_DISPOSABLE')!='yes':
            raise RuntimeError('Integration tests require explicit disposable database opt-in')
        migrate(ADMIN,'ecosystem_app')
        migrate(ADMIN,'ecosystem_app')

    def setUp(self):
        self.org='test-'+uuid4().hex
        self.other='test-'+uuid4().hex
        with psycopg.connect(ADMIN) as c:
            for org in (self.org,self.other):
                c.execute('INSERT INTO ecosystem.organizations VALUES (%s,%s)',(org,org))
                for actor in ('alice','bob'):
                    c.execute('INSERT INTO ecosystem.memberships(organization_id,actor_id,scopes) VALUES (%s,%s,%s)',(org,actor,['demo:write','demo:read','demo:outbox']))
                c.execute('INSERT INTO ecosystem.organization_domains VALUES (%s,%s,true)',(org,'demo'))
        self.context={'organization_id':self.org,'actor_id':'alice','idempotency_key':'key-1'}
        self.store=PostgresStore(APP)
        self.calls=0
        async def handler(name,args,context):
            self.calls+=1
            await asyncio.sleep(0.03)
            row=await context['repository'].create_item('note',args)
            return {'ok':True,'data':{'id':row['id']},'error':None,'warnings':[]}
        self.plugin,self.core=self.make_core(handler)

    def make_core(self,handler,read_only=False):
        spec={'name':'demo__create','description':'Test write','input_schema':deepcopy(SCHEMA),'output_schema':deepcopy(OUT),
              'required_scopes':['demo:read' if read_only else 'demo:write'],'read_only':read_only,
              'idempotent':True,'destructive':False,'open_world':False}
        plugin=SimpleNamespace(DOMAIN='demo',CONTRACT_VERSION='0.1',tools=lambda:[spec],handle=handler)
        registry=Registry();registry.register(plugin)
        return plugin,Core(registry,None,persistence=self.store,timeout=10)

    async def call(self,value='x',context=None):
        return await self.core.dispatch(self.plugin,'demo__create',{'value':value},context or self.context)

    def counts(self):
        with psycopg.connect(ADMIN) as c:
            return {t:c.execute('SELECT count(*) FROM ecosystem.'+t+' WHERE organization_id=%s',(self.org,)).fetchone()[0]
                    for t in ('items','events','outbox','idempotency','audit_log')}

    async def test_atomic_write_replay_and_restart(self):
        first=await self.call();self.assertTrue(first['ok'],first)
        self.assertEqual(self.counts(),dict(items=1,events=1,outbox=1,idempotency=1,audit_log=1))
        self.core.persistence=PostgresStore(APP)  # Fresh connection/store sees durable result.
        self.assertEqual(await self.call(),first)
        self.assertEqual(self.calls,1)
        self.assertEqual(self.counts()['audit_log'],2)

    async def test_concurrent_same_key_one_effect(self):
        results=await asyncio.gather(*(self.call() for _ in range(6)))
        self.assertTrue(results[0]['ok'],results)
        self.assertTrue(all(r==results[0] for r in results))
        self.assertEqual(self.calls,1)
        self.assertEqual(self.counts()['items'],1)

    async def test_key_conflict_and_actor_binding(self):
        self.assertTrue((await self.call())['ok'])
        self.assertEqual((await self.call('different'))['error']['code'],'CONFLICT')
        self.assertEqual((await self.call(context={**self.context,'actor_id':'bob'}))['error']['code'],'CONFLICT')
        self.assertEqual(self.calls,1)

    async def test_tenant_key_isolation(self):
        a=await self.call()
        b=await self.call(context={**self.context,'organization_id':self.other})
        self.assertTrue(a['ok'] and b['ok'],(a,b));self.assertNotEqual(a['data']['id'],b['data']['id'])
        async with self.store.session(self.context,'demo',['demo:read'],read_only=True) as (_,_,repo):
            self.assertIsNone(await repo.get_item(b['data']['id']))
            self.assertEqual((await repo.get_item(a['data']['id']))['id'],a['data']['id'])

    async def test_rollback_error_and_invalid_result(self):
        async def bad(name,args,ctx):
            await ctx['repository'].create_item('note',args)
            return failure('CONFLICT','Test rejection')
        for invalid in (False,True):
            async def handler(name,args,ctx):
                result=await bad(name,args,ctx)
                return {'invalid':'SECRET'} if invalid else result
            p,c=self.make_core(handler)
            result=await c.dispatch(p,'demo__create',{'value':'x'},self.context)
            self.assertFalse(result['ok'])
            self.assertEqual(self.counts(),dict(items=0,events=0,outbox=0,idempotency=0,audit_log=0))
        self.assertTrue((await self.call())['ok'])  # Rolled-back reservation is reusable.

    async def test_timeout_rolls_back(self):
        async def slow(name,args,ctx):
            await ctx['repository'].create_item('note',args)
            await asyncio.sleep(2)
        p,c=self.make_core(slow);c.timeout=0.2
        result=await c.dispatch(p,'demo__create',{'value':'x'},self.context)
        self.assertEqual(result['error']['code'],'DEPENDENCY_UNAVAILABLE')
        self.assertEqual(self.counts()['items'],0)

    async def test_revocation_applies_to_replay_and_catalog(self):
        self.assertTrue((await self.call())['ok'])
        self.assertEqual(len(await self.core.catalog_async(self.context,'mcp')),1)
        with psycopg.connect(ADMIN) as c:
            c.execute('UPDATE ecosystem.memberships SET active=false WHERE organization_id=%s AND actor_id=%s',(self.org,'alice'))
        self.assertEqual((await self.call())['error']['code'],'FORBIDDEN')
        self.assertEqual(await self.core.catalog_async(self.context),[])

    async def test_forged_scopes_and_missing_key(self):
        result=await self.call(context={**self.context,'idempotency_key':None})
        self.assertEqual(result['error']['code'],'INVALID_ARGUMENT')
        with psycopg.connect(ADMIN) as c:
            c.execute("UPDATE ecosystem.memberships SET scopes='{}' WHERE organization_id=%s",(self.org,))
        result=await self.call(context={**self.context,'scopes':['demo:write']})
        self.assertEqual(result['error']['code'],'FORBIDDEN')

    async def test_read_only_cannot_write(self):
        async def write(name,args,ctx):
            await ctx['repository'].create_item('note',args)
            return {'ok':True,'data':{'id':'bad'},'error':None,'warnings':[]}
        p,c=self.make_core(write,read_only=True)
        result=await c.dispatch(p,'demo__create',{'value':'x'},self.context)
        self.assertFalse(result['ok']);self.assertEqual(self.counts()['items'],0)

    async def test_rls_domain_fk_and_immutable_events(self):
        a=await self.call();self.assertTrue(a['ok'],a)
        # Unscoped application SQL sees no rows even without explicit WHERE clauses.
        with psycopg.connect(APP) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM ecosystem.items').fetchone()[0],0)
        async with self.store.session(self.context,'demo',['demo:read']) as (c,_,_):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                async with c.transaction():
                    await c.execute('UPDATE ecosystem.events SET kind=%s',('tampered',))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                async with c.transaction():
                    await c.execute('INSERT INTO ecosystem.items(organization_id,id,domain,kind,created_by) VALUES (%s,%s,%s,%s,%s)',(self.other,str(uuid4()),'demo','note','alice'))
        # Composite FK forbids event in one organization referencing another item.
        with psycopg.connect(ADMIN) as c:
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                c.execute('INSERT INTO ecosystem.events(organization_id,id,domain,kind,item_id,item_revision,actor_id,request_id) VALUES (%s,%s,%s,%s,%s,1,%s,%s)',
                    (self.other,str(uuid4()),'demo','bad',a['data']['id'],'alice',str(uuid4())))

    async def test_optimistic_revision(self):
        result=await self.call();self.assertTrue(result['ok'],result)
        item=result['data']['id']
        async with self.store.session(self.context,'demo',['demo:write']) as (_,_,repo):
            changed=await repo.update_item(item,1,{'value':'new'})
            self.assertEqual(changed['revision'],2)
        from core.db.store import Rejected
        with self.assertRaises(Rejected):
            async with self.store.session(self.context,'demo',['demo:write']) as (_,_,repo):
                await repo.update_item(item,1,{'value':'stale'})
        self.assertEqual(self.counts()['events'],2)

    async def test_outbox_leases_and_stale_ack(self):
        self.assertTrue((await self.call())['ok'])
        claims=await asyncio.gather(self.store.claim_outbox(self.context,'demo'),self.store.claim_outbox(self.context,'demo'))
        self.assertEqual(sum(map(len,claims)),1)
        lease=next(x[0] for x in claims if x)
        with psycopg.connect(ADMIN) as c:
            c.execute("UPDATE ecosystem.outbox SET lease_until=now()-interval '1 second' WHERE organization_id=%s",(self.org,))
        new=(await self.store.claim_outbox(self.context,'demo'))[0]
        self.assertNotEqual(lease['lease_token'],new['lease_token'])
        self.assertFalse(await self.store.finish_outbox(self.context,'demo',lease['id'],lease['lease_token']))
        self.assertTrue(await self.store.finish_outbox(self.context,'demo',new['id'],new['lease_token']))
        self.assertEqual(await self.store.claim_outbox(self.context,'demo'),[])

    async def test_outbox_retry_and_dead_letter(self):
        self.assertTrue((await self.call())['ok'])
        row=(await self.store.claim_outbox(self.context,'demo',max_attempts=1))[0]
        self.assertTrue(await self.store.finish_outbox(self.context,'demo',row['id'],row['lease_token'],delivered=False,retry_seconds=1))
        self.assertEqual(await self.store.claim_outbox(self.context,'demo',max_attempts=1),[])
        with psycopg.connect(ADMIN) as c:
            self.assertIsNotNone(c.execute('SELECT dead_at FROM ecosystem.outbox WHERE organization_id=%s',(self.org,)).fetchone()[0])

    async def test_superuser_runtime_rejected(self):
        self.core.persistence=PostgresStore(ADMIN)
        result=await self.call()
        self.assertEqual(result['error']['code'],'INTERNAL_ERROR')
        self.assertEqual(self.calls,0)

    async def test_migration_checksum(self):
        with psycopg.connect(ADMIN) as c:
            c.execute("UPDATE public.ecosystem_migrations SET sha256='tampered'")
        try:
            with self.assertRaisesRegex(ValueError,'checksum'):
                migrate(ADMIN,'ecosystem_app')
        finally:
            import hashlib
            from pathlib import Path
            digest=hashlib.sha256((Path(__file__).parents[1]/'core/db/migrations/001_initial.sql').read_bytes()).hexdigest()
            with psycopg.connect(ADMIN) as c:
                c.execute('UPDATE public.ecosystem_migrations SET sha256=%s',(digest,))

    async def test_http_authenticated_write_and_replay(self):
        import hashlib
        import time
        import httpx2
        from server.app import create_app
        from server.auth import TokenVerifier
        key = {'sha256': hashlib.sha256(b'ephemeral-test-token').hexdigest(),
               'expires_at': time.time()+60, 'organization_id': self.org,
               'sub': 'alice', 'scope': 'demo:write'}
        app = create_app(self.core, [self.plugin], TokenVerifier([key]))
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                base_url='http://localhost', trust_env=False,
                headers={'Authorization': 'Bearer ephemeral-test-token', 'Idempotency-Key': 'http-write'}) as http:
            body = {'name': 'demo__create', 'arguments': {'value': 'through-http'}}
            first = await http.post('/demo/v1/tools/invoke', json=body)
            replay = await http.post('/demo/v1/tools/invoke', json=body)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertTrue(first.json()['ok'])
            self.assertEqual(first.json(), replay.json())
            self.assertEqual(self.counts()['items'], 1)
            with psycopg.connect(ADMIN) as c:
                c.execute('UPDATE ecosystem.memberships SET active=false WHERE organization_id=%s AND actor_id=%s', (self.org, 'alice'))
            self.assertEqual((await http.post('/demo/v1/tools/invoke', json=body)).status_code, 403)


if __name__=='__main__':
    unittest.main()
