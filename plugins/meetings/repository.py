"""Meetings-owned SQL using the core transaction, identity, RLS and outbox."""
import base64
import json
from datetime import datetime
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from core.db.store import Rejected
from core.runtime import failure
from .checks import RULE_VERSION, digest, text_hash, evidence_errors, consistency_errors


def reject(code, message):
    raise Rejected(failure(code, message))


def uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Expected a UUID') from None


class Meetings:
    def __init__(self, uow):
        self.uow = uow
        self.conn = uow.connection_for('meetings')
        self.org = uow.context['organization_id']
        self.actor = uow.context['actor_id']

    async def one(self, query, params=()):
        return await (await self.conn.execute(query, params)).fetchone()

    async def rows(self, query, params=()):
        return await (await self.conn.execute(query, params)).fetchall()

    async def execute(self, action, args):
        if action in ('import_batch', 'submit_draft', 'review_run', 'publish'):
            # Serialize domain writes per tenant: versions, history and publication
            # must be compared in the same transaction. Hash collisions only serialize.
            await self.conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 51477))', (self.org,))
            await self.conn.execute('INSERT INTO meetings.tenant_state(organization_id) VALUES (%s) ON CONFLICT DO NOTHING', (self.org,))
        return await getattr(self, action)(**args)

    async def list_batches(self, cursor, limit):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('Page size must be 1 to 50')
        params = [self.org]
        after = ''
        if cursor is not None:
            try:
                if not isinstance(cursor,str) or len(cursor)>256:
                    raise ValueError()
                stamp, item_id = json.loads(base64.b64decode(cursor,altchars=b'-_',validate=True))
                parsed = datetime.fromisoformat(stamp)
                if parsed.tzinfo is None:
                    raise ValueError()
                item_id = uuid(item_id)
            except (ValueError,TypeError,UnicodeError):
                raise ValueError('Invalid page cursor') from None
            after = ' AND (b.created_at,b.id)<(%s,%s::uuid)'
            params.extend([parsed,item_id])
        params.append(limit+1)
        rows = await self.rows('SELECT b.id::text AS batch_id,b.created_at,b.source_account_id,b.status AS ingestion_status,b.current_run_id::text AS run_id,r.status AS run_status,(SELECT count(*) FROM meetings.batch_entries e WHERE e.organization_id=b.organization_id AND e.batch_id=b.id) AS files FROM meetings.batches b LEFT JOIN meetings.runs r ON r.organization_id=b.organization_id AND r.id=b.current_run_id WHERE b.organization_id=%s'+after+' ORDER BY b.created_at DESC,b.id DESC LIMIT %s',params)
        more = len(rows)>limit
        rows = rows[:limit]
        for row in rows:
            row['created_at'] = row['created_at'].isoformat()
            row['stale'] = await self.stale(await self.run(row['run_id'])) if row['run_id'] else False
        next_cursor = base64.urlsafe_b64encode(json.dumps([rows[-1]['created_at'],rows[-1]['batch_id']]).encode()).decode() if more else None
        return {'records':rows,'next_cursor':next_cursor}

    async def import_batch(self, source_account_id, sources):
        if not source_account_id.strip() or len(source_account_id) > 128:
            raise ValueError('Source account is required (maximum 128 characters)')
        if not 1 <= len(sources) <= 20 or sum(len(s['text'].encode()) for s in sources) > 32768:
            raise ValueError('Batch limit: 1-20 text entries, 32768 UTF-8 bytes total')
        manifest = digest({'source_account_id': source_account_id, 'sources': sources})
        anchor = await self.uow.create_item('meeting_batch', {'state': 'ingested', 'manifest_hash': manifest})
        batch_id = anchor['id']
        await self.conn.execute('INSERT INTO meetings.batches(organization_id,id,source_account_id,manifest_hash,status) VALUES (%s,%s,%s,%s,%s)',
                                (self.org, batch_id, source_account_id, manifest, 'ingested'))
        seen, blocked = set(), False
        for index, source in enumerate(sources):
            filename, fmt, text, external_id = (source[k] for k in ('filename','format','text','external_id'))
            reason, version_id, status = '', None, 'accepted'
            if not filename.strip() or len(filename) > 255:
                reason = 'Invalid filename'
            elif fmt != 'text/plain':
                reason = 'Unsupported format; only explicit text/plain is accepted'
            elif not text.strip() or '\x00' in text:
                reason = 'Empty text or NUL characters'
            elif external_id is not None and (not external_id.strip() or len(external_id) > 256):
                reason = 'Invalid external ID'
            sha = text_hash(text)
            external_key = 'id:'+external_id if external_id is not None else 'sha256:'+sha
            if not reason and external_key in seen:
                reason = 'Repeated source identity in the same batch'
            if reason:
                status, blocked = 'rejected', True
            else:
                seen.add(external_key)
                source_row = await self.one('SELECT id::text FROM meetings.sources WHERE organization_id=%s AND source_account_id=%s AND external_key=%s',
                                           (self.org, source_account_id, external_key))
                if source_row is None:
                    source_row = {'id': str(uuid4())}
                    await self.conn.execute('INSERT INTO meetings.sources VALUES (%s,%s,%s,%s)',
                                           (self.org, source_row['id'], source_account_id, external_key))
                latest = await self.one('SELECT id::text,revision,sha256 FROM meetings.source_versions WHERE organization_id=%s AND source_id=%s ORDER BY revision DESC LIMIT 1',
                                        (self.org, source_row['id']))
                if latest and latest['sha256'] == sha:
                    version_id, status = latest['id'], 'duplicate'
                else:
                    version_id = str(uuid4())
                    await self.conn.execute('INSERT INTO meetings.source_versions(organization_id,id,source_id,revision,sha256,text_content) VALUES (%s,%s,%s,%s,%s,%s)',
                        (self.org, version_id, source_row['id'], latest['revision']+1 if latest else 1, sha, text))
            # PostgreSQL text cannot store NUL: preserve it in JSON? Reject the entire
            # request instead of silently altering the original or losing an entry.
            if any('\x00' in v for v in (filename, fmt, text, external_id or '')):
                raise ValueError('NUL bytes cannot be preserved in this text intake; no batch was saved')
            await self.conn.execute('INSERT INTO meetings.batch_entries VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (self.org, batch_id, index, filename, fmt, text, external_id, status, reason, version_id))
        if blocked:
            await self.conn.execute("UPDATE meetings.batches SET status='blocked_ingestion' WHERE organization_id=%s AND id=%s", (self.org,batch_id))
            await self.touch(batch_id, 'blocked_ingestion')
        return await self.get_batch(batch_id)

    async def batch(self, batch_id):
        row = await self.one('SELECT id::text,manifest_hash,status,current_run_id::text FROM meetings.batches WHERE organization_id=%s AND id=%s', (self.org,uuid(batch_id)))
        if row is None:
            reject('NOT_FOUND', 'Batch not found')
        return row

    async def get_batch(self, batch_id):
        batch = await self.batch(batch_id)
        entries = await self.rows('SELECT e.ordinal AS index,e.filename,e.format,e.raw_text AS text,e.external_id,e.status,e.reason,e.source_version_id::text,v.sha256 AS source_hash FROM meetings.batch_entries e LEFT JOIN meetings.source_versions v ON v.organization_id=e.organization_id AND v.id=e.source_version_id WHERE e.organization_id=%s AND e.batch_id=%s ORDER BY e.ordinal', (self.org,batch['id']))
        return {'batch_id': batch['id'], 'status': batch['status'], 'entries': entries}

    async def history_revision(self):
        row = await self.one('SELECT history_revision FROM meetings.tenant_state WHERE organization_id=%s', (self.org,))
        return row['history_revision'] if row else 0

    async def run(self, run_id):
        row = await self.one('SELECT id::text AS run_id,batch_id::text,status,artifact_hash,source_hash,rule_version,claims,history_revision,created_by,model_version,prompt_version FROM meetings.runs WHERE organization_id=%s AND id=%s', (self.org,uuid(run_id)))
        if row is None:
            reject('NOT_FOUND', 'Run not found')
        return row

    async def stale(self, run):
        batch = await self.batch(run['batch_id'])
        if batch['current_run_id'] != run['run_id'] or batch['manifest_hash'] != run['source_hash']:
            return True
        outdated = await self.one('SELECT EXISTS(SELECT 1 FROM meetings.batch_entries e JOIN meetings.source_versions v ON v.organization_id=e.organization_id AND v.id=e.source_version_id WHERE e.organization_id=%s AND e.batch_id=%s AND EXISTS(SELECT 1 FROM meetings.source_versions newer WHERE newer.organization_id=v.organization_id AND newer.source_id=v.source_id AND newer.revision>v.revision)) AS stale', (self.org,run['batch_id']))
        if outdated['stale']:
            return True
        return run['status'] != 'published' and run['history_revision'] != await self.history_revision()

    async def current_checks(self, run_id):
        return await self.rows('SELECT DISTINCT ON (check_no) check_no,verdict,reason,method,reviewer_id,source_hash,artifact_hash,rule_version FROM meetings.check_results WHERE organization_id=%s AND run_id=%s ORDER BY check_no,revision DESC', (self.org,run_id))

    async def get_run(self, run_id):
        run = await self.run(run_id)
        run['stale'] = await self.stale(run)
        run.pop('history_revision')
        run['checks'] = [{k:c[k] for k in ('check_no','verdict','reason','method','reviewer_id')} for c in await self.current_checks(run['run_id'])]
        return run

    async def get_history(self):
        records = await self.rows('SELECT r.id::text AS run_id,r.artifact_hash,r.claims FROM meetings.publications p JOIN meetings.runs r ON r.organization_id=p.organization_id AND r.id=p.run_id WHERE p.organization_id=%s ORDER BY p.history_revision DESC LIMIT 101', (self.org,))
        for record in records[:100]:
            record['stale'] = await self.stale(await self.run(record['run_id']))
        return {'records': records[:100], 'truncated': len(records)>100}

    async def add_check(self, run, number, verdict, reason, method='deterministic'):
        await self.conn.execute('INSERT INTO meetings.check_results(organization_id,run_id,check_no,revision,verdict,reason,method,rule_version,source_hash,artifact_hash,reviewer_id) SELECT %s,%s,%s,COALESCE(max(revision),0)+1,%s,%s,%s,%s,%s,%s,%s FROM meetings.check_results WHERE organization_id=%s AND run_id=%s AND check_no=%s',
            (self.org,run['run_id'],number,verdict,reason,method,RULE_VERSION,run['source_hash'],run['artifact_hash'],self.actor if method=='human' else None,self.org,run['run_id'],number))

    async def submit_draft(self, batch_id, claims, model_version, prompt_version):
        batch = await self.get_batch(batch_id)
        if batch['status'] != 'ingested':
            reject('CONFLICT', 'Batch contains rejected entries; import a corrected complete batch')
        if not 1 <= len(claims) <= 50:
            raise ValueError('Expected 1 to 50 claims')
        if any(v is not None and (not v.strip() or len(v)>200) for v in (model_version,prompt_version)):
            raise ValueError('Invalid model/prompt version')
        header = await self.batch(batch_id)
        history = await self.get_history()
        errors2 = evidence_errors(claims, batch['entries'])
        errors3 = consistency_errors(claims, history['records'])
        if history['truncated']:
            errors3.append('History exceeds this bounded review implementation')
        run = {'run_id':str(uuid4()),'source_hash':header['manifest_hash'],'artifact_hash':digest(claims)}
        status = 'failed' if errors2 or errors3 else 'needs_review'
        await self.conn.execute('INSERT INTO meetings.runs(organization_id,id,batch_id,artifact_hash,source_hash,claims,history_revision,rule_version,created_by,model_version,prompt_version,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (self.org,run['run_id'],batch_id,run['artifact_hash'],run['source_hash'],Jsonb(claims),await self.history_revision(),RULE_VERSION,self.actor,model_version,prompt_version,status))
        await self.conn.execute('UPDATE meetings.batches SET current_run_id=%s WHERE organization_id=%s AND id=%s', (run['run_id'],self.org,batch_id))
        await self.add_check(run,1,'pass','Every declared text entry is stored and linked; this does not prove the export itself was complete')
        await self.add_check(run,2,'fail' if errors2 else 'needs_review','; '.join(errors2) if errors2 else 'Exact quotations match; independent semantic and coverage review required')
        await self.add_check(run,3,'fail' if errors3 else 'needs_review','; '.join(errors3) if errors3 else 'No exact-key conflicts detected; contextual and history review required')
        await self.add_check(run,4,'needs_review','Publication blocked pending checks 1-3 and coverage confirmation')
        await self.touch(batch_id,status,run['run_id'])
        return await self.get_run(run['run_id'])

    async def validate_current(self, run_id, artifact_hash):
        run = await self.run(run_id)
        if run['artifact_hash'] != artifact_hash or digest(run['claims']) != artifact_hash:
            reject('CONFLICT', 'Artifact hash mismatch')
        if run['rule_version'] != RULE_VERSION or await self.stale(run):
            reject('CONFLICT', 'Run is stale; submit a new draft against the current source batch and history')
        return run

    async def review_run(self, run_id, artifact_hash, accuracy_pass, consistency_pass, coverage_confirmed, rationale):
        run = await self.validate_current(run_id, artifact_hash)
        if run['status'] == 'published':
            reject('CONFLICT', 'Published review is immutable; submit a new draft')
        if run['created_by'] == self.actor:
            reject('FORBIDDEN', 'An independent reviewer is required')
        if not rationale.strip() or len(rationale)>2000:
            raise ValueError('Review rationale is required (maximum 2000 characters)')
        batch = await self.get_batch(run['batch_id'])
        history = await self.get_history()
        if evidence_errors(run['claims'],batch['entries']) or consistency_errors(run['claims'],history['records']) or history['truncated']:
            reject('CONFLICT','Deterministic checks failed; correct the draft instead of overriding failures')
        await self.add_check(run,2,'pass' if accuracy_pass and coverage_confirmed else 'fail',rationale,'human')
        await self.add_check(run,3,'pass' if consistency_pass else 'fail',rationale,'human')
        checks = await self.current_checks(run_id)
        ready = coverage_confirmed and all(c['verdict']=='pass' for c in checks if c['check_no']<=3) and len(checks)==4
        await self.add_check(run,4,'pass' if ready else 'fail','All upstream checks and independent coverage attestation passed' if ready else 'Unresolved review findings')
        await self.conn.execute('UPDATE meetings.runs SET status=%s WHERE organization_id=%s AND id=%s', ('ready' if ready else 'needs_review',self.org,run_id))
        await self.touch(run['batch_id'],'ready' if ready else 'needs_review',run_id)
        return await self.get_run(run_id)

    async def publish(self, run_id, artifact_hash):
        run = await self.validate_current(run_id, artifact_hash)
        if run['status'] == 'published':
            return await self.get_run(run_id)
        checks = await self.current_checks(run_id)
        if run['status']!='ready' or {c['check_no'] for c in checks}!={1,2,3,4} or any(
            c['verdict']!='pass' or c['source_hash']!=run['source_hash'] or c['artifact_hash']!=artifact_hash or c['rule_version']!=RULE_VERSION for c in checks):
            reject('CONFLICT','Publication requires four passes from this exact current run')
        revision = await self.one('UPDATE meetings.tenant_state SET history_revision=history_revision+1 WHERE organization_id=%s RETURNING history_revision',(self.org,))
        await self.conn.execute('INSERT INTO meetings.publications(organization_id,run_id,history_revision,published_by) VALUES (%s,%s,%s,%s)', (self.org,run_id,revision['history_revision'],self.actor))
        await self.conn.execute("UPDATE meetings.runs SET status='published' WHERE organization_id=%s AND id=%s",(self.org,run_id))
        anchor = await self.touch(run['batch_id'],'published',run_id)
        await self.uow.record_event(run['batch_id'],anchor['revision'],'meetings.published')
        return await self.get_run(run_id)

    async def touch(self, batch_id, status, run_id=None):
        anchor = await self.uow.get_item(batch_id)
        return await self.uow.update_item(batch_id,anchor['revision'],{'state':status,'run_id':run_id})
