"""Tenant-scoped transactional execution. Plugins are trusted code, not a sandbox."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
import re
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from ..runtime import AccessPolicy, Core, failure, json_copy


class Rejected(Exception):
    def __init__(self, result):
        self.result = result


class UnitOfWork:
    def __init__(self, conn, context, domain):
        self._conn, self.context, self.domain = conn, context, domain
        self._active = True

    def check(self):
        if not self._active:
            raise RuntimeError('Unit of work already closed')

    async def get_item(self, item_id):
        self.check()
        row = await (await self._conn.execute('SELECT id::text,kind,status,revision,meta FROM ecosystem.items WHERE organization_id=%s AND domain=%s AND id=%s',
                    (self.context['organization_id'],self.domain,item_id))).fetchone()
        return row

    async def create_item(self, kind, meta):
        self.check()
        if not isinstance(kind,str) or not kind or len(kind)>100 or not isinstance(meta,dict):
            raise ValueError('Invalid item')
        meta = json_copy(meta,1048576)
        item_id = str(uuid4())
        await self._conn.execute('INSERT INTO ecosystem.items(organization_id,id,domain,kind,meta,created_by) VALUES (%s,%s,%s,%s,%s,%s)',
            (self.context['organization_id'],item_id,self.domain,kind,Jsonb(meta),self.context['actor_id']))
        await self._event(item_id,1,'created')
        return await self.get_item(item_id)

    async def update_item(self, item_id, expected_revision, meta):
        self.check()
        if type(expected_revision) is not int or expected_revision < 1 or not isinstance(meta,dict):
            raise ValueError('Invalid update')
        meta = json_copy(meta,1048576)
        row = await (await self._conn.execute('UPDATE ecosystem.items SET meta=%s,revision=revision+1,updated_at=now() WHERE organization_id=%s AND domain=%s AND id=%s AND revision=%s RETURNING revision',
                (Jsonb(meta),self.context['organization_id'],self.domain,item_id,expected_revision))).fetchone()
        if row is None:
            raise Rejected(failure('CONFLICT','Item unavailable or revision changed'))
        await self._event(item_id,row['revision'],'updated')
        return await self.get_item(item_id)

    async def _event(self, item_id, revision, kind):
        event_id = str(uuid4())
        org = self.context['organization_id']
        await self._conn.execute('INSERT INTO ecosystem.events(organization_id,id,domain,kind,item_id,item_revision,actor_id,request_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            (org,event_id,self.domain,kind,item_id,revision,self.context['actor_id'],self.context['request_id']))
        await self._conn.execute('INSERT INTO ecosystem.outbox(organization_id,id,domain,event_id) VALUES (%s,%s,%s,%s)',
            (org,str(uuid4()),self.domain,event_id))


class PostgresStore:
    def __init__(self, dsn):
        self._dsn = dsn

    @asynccontextmanager
    async def session(self, context, domain, scopes, *, read_only=False):
        if not isinstance(context,dict) or any(not isinstance(context.get(k),str) or not context[k] for k in ('organization_id','actor_id')):
            raise Rejected(failure('UNAUTHENTICATED','Authenticated identity required'))
        async with await psycopg.AsyncConnection.connect(self._dsn,connect_timeout=5,row_factory=dict_row) as conn:
            async with conn.transaction():
                if read_only:
                    await conn.execute('SET TRANSACTION READ ONLY')
                role = await (await conn.execute("SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=current_user")).fetchone()
                owner = await (await conn.execute("SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='ecosystem' AND tableowner=current_user) AS owns")).fetchone()
                if any(role.values()) or owner['owns']:
                    raise Rejected(failure('INTERNAL_ERROR','Unsafe database application role'))
                await conn.execute("SELECT set_config('app.organization_id',%s,true),set_config('app.actor_id',%s,true),set_config('app.domain',%s,true),set_config('statement_timeout','10000',true),set_config('lock_timeout','5000',true)",
                    (context['organization_id'],context['actor_id'],domain))
                lock = ''  # Authorization at transaction entry; subsequent calls see revocation.
                member = await (await conn.execute('SELECT scopes FROM ecosystem.memberships WHERE organization_id=%s AND actor_id=%s AND active'+lock,
                        (context['organization_id'],context['actor_id']))).fetchone()
                enabled = await (await conn.execute('SELECT enabled FROM ecosystem.organization_domains WHERE organization_id=%s AND domain=%s AND enabled'+lock,
                        (context['organization_id'],domain))).fetchone()
                if member is None or enabled is None or not set(scopes)<=set(member['scopes']):
                    raise Rejected(failure('FORBIDDEN','Organization or tool access denied'))
                trusted = {'organization_id':context['organization_id'],'actor_id':context['actor_id'],
                           'scopes':member['scopes'],'request_id':str(uuid4()),'idempotency_key':None}
                uow = UnitOfWork(conn,trusted,domain)
                try:
                    yield conn, trusted, uow
                finally:
                    uow._active = False

    async def catalog(self, core, context, style):
        # Query each registered domain with its own RLS context; no tenant-wide SQL bypass.
        if not isinstance(context,dict) or any(not isinstance(context.get(k),str) or not context[k] for k in ('organization_id','actor_id')):
            raise PermissionError('UNAUTHENTICATED')
        specs=[]
        for domain in sorted(core.registry._domains):
            try:
                async with self.session(context,domain,[],read_only=True) as (_,trusted,_):
                    specs.extend(deepcopy(e.spec) for e in core.registry._entries.values()
                                 if e.domain==domain and set(e.spec['required_scopes'])<=set(trusted['scopes']))
            except Rejected as exc:
                if exc.result['error']['code']=='FORBIDDEN':
                    continue
                raise PermissionError(exc.result['error']['code']) from None
        return Core.export_specs(specs,style)

    async def dispatch(self, core, plugin, name, arguments, context):
        entry=core.registry._entries.get(name) if isinstance(name,str) else None
        if entry is None or entry.plugin is not plugin:
            return failure('NOT_FOUND','Unknown tool')
        try:
            args=core.validate_arguments(entry,arguments,core.input_limit)
        except (ValueError,TypeError,RecursionError,OverflowError):
            return failure('INVALID_ARGUMENT','Arguments do not match tool schema or limits')
        read_only=entry.spec['read_only']
        key=context.get('idempotency_key') if isinstance(context,dict) else None
        if not read_only and (not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,200}',key)):
            return failure('INVALID_ARGUMENT','A valid idempotency_key is required for writes')
        if not read_only and entry.spec['open_world']:
            return failure('NOT_IMPLEMENTED','External effects must use an outbox consumer')
        try:
            return await asyncio.wait_for(self._execute(core,entry,name,args,context,key),core.timeout)
        except Rejected as exc:
            return exc.result
        except (asyncio.TimeoutError,psycopg.OperationalError,psycopg.errors.QueryCanceled,psycopg.errors.LockNotAvailable):
            return failure('DEPENDENCY_UNAVAILABLE','Database operation unavailable; retry the same idempotency key')
        except Exception:
            return failure('INTERNAL_ERROR','Tool execution failed')

    async def _execute(self,core,entry,name,args,context,key):
        write=not entry.spec['read_only']
        async with self.session(context,entry.domain,entry.spec['required_scopes'],read_only=not write) as (conn,trusted,uow):
            org=trusted['organization_id']
            if write:
                trusted['idempotency_key']=key
                digest=hashlib.sha256(json.dumps(args,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
                claimed=await (await conn.execute('INSERT INTO ecosystem.idempotency(organization_id,domain,operation,key,actor_id,arguments_hash) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING key',
                    (org,entry.domain,name,key,trusted['actor_id'],digest))).fetchone()
                if claimed is None:
                    saved=await (await conn.execute('SELECT actor_id,arguments_hash,result FROM ecosystem.idempotency WHERE organization_id=%s AND operation=%s AND key=%s FOR UPDATE',(org,name,key))).fetchone()
                    if saved is None or saved['actor_id']!=trusted['actor_id'] or saved['arguments_hash']!=digest:
                        raise Rejected(failure('CONFLICT','Idempotency key already used for another request'))
                    cached=json_copy(saved['result'],core.output_limit)
                    if not core._valid_result(cached,entry):
                        raise Rejected(failure('CONFLICT','Stored result incompatible with current contract'))
                    await self._audit(conn,trusted,entry.domain,name,True)
                    return cached
            handler_context=deepcopy(trusted)
            handler_context['repository']=uow
            raw=await entry.handler(name,args,handler_context)
            result=json_copy(raw,core.output_limit)
            if not core._valid_result(result,entry):
                raise ValueError('Invalid handler result')
            if not result['ok']:
                # Roll back any tentative records, events, and idempotency reservation.
                raise Rejected(result)
            if write:
                await conn.execute('UPDATE ecosystem.idempotency SET result=%s WHERE organization_id=%s AND operation=%s AND key=%s', (Jsonb(result),org,name,key))
                await self._audit(conn,trusted,entry.domain,name,False)
            return result

    @staticmethod
    async def _audit(conn,context,domain,operation,replayed):
        await conn.execute('INSERT INTO ecosystem.audit_log(organization_id,id,domain,actor_id,request_id,operation,replayed) VALUES (%s,%s,%s,%s,%s,%s,%s)',
            (context['organization_id'],str(uuid4()),domain,context['actor_id'],context['request_id'],operation,replayed))

    async def claim_outbox(self,context,domain,*,limit=10,lease_seconds=60,max_attempts=5):
        if type(limit) is not int or not 1<=limit<=100 or type(lease_seconds) is not int or not 1<=lease_seconds<=3600 or type(max_attempts) is not int or not 1<=max_attempts<=20:
            raise ValueError('Invalid lease bounds')
        async with self.session(context,domain,[domain+':outbox']) as (conn,_,_):
            await conn.execute('UPDATE ecosystem.outbox SET dead_at=now(),lease_token=NULL,lease_until=NULL WHERE attempts >= %s AND delivered_at IS NULL AND dead_at IS NULL AND (lease_until IS NULL OR lease_until<=now())',(max_attempts,))
            rows=await (await conn.execute('SELECT id::text,event_id::text FROM ecosystem.outbox WHERE delivered_at IS NULL AND dead_at IS NULL AND available_at<=now() AND (lease_until IS NULL OR lease_until<=now()) ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT %s',(limit,))).fetchall()
            for row in rows:
                token=str(uuid4());row['lease_token']=token
                await conn.execute("UPDATE ecosystem.outbox SET attempts=attempts+1,lease_token=%s,lease_until=now()+make_interval(secs=>%s) WHERE id=%s",(token,lease_seconds,row['id']))
            return rows

    async def finish_outbox(self,context,domain,item_id,token,*,delivered=True,retry_seconds=30):
        if type(delivered) is not bool or type(retry_seconds) is not int or not 1<=retry_seconds<=86400:
            raise ValueError('Invalid retry delay')
        async with self.session(context,domain,[domain+':outbox']) as (conn,_,_):
            row=await (await conn.execute('UPDATE ecosystem.outbox SET delivered_at=CASE WHEN %s THEN now() ELSE NULL END,available_at=now()+make_interval(secs=>%s),lease_token=NULL,lease_until=NULL WHERE id=%s AND lease_token=%s AND lease_until>now() AND delivered_at IS NULL AND dead_at IS NULL RETURNING id',
                (delivered,retry_seconds,item_id,token))).fetchone()
            return row is not None
