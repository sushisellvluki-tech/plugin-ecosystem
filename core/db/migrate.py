"""Checksum-verified, transactionally applied SQL migrations. Admin connection only."""
import hashlib
import os
from pathlib import Path
import psycopg
from psycopg import sql


def migrate(dsn, app_role):
    with psycopg.connect(dsn) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(8472042026)')
        conn.execute('CREATE TABLE IF NOT EXISTS public.ecosystem_migrations (name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())')
        conn.execute('REVOKE ALL ON public.ecosystem_migrations FROM PUBLIC')
        role = conn.execute('SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname=%s', (app_role,)).fetchone()
        if role is None or any(role):
            raise ValueError('Create a restricted application role before migration')
        for path in sorted((Path(__file__).parent/'migrations').glob('*.sql')):
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            existing = conn.execute('SELECT sha256 FROM public.ecosystem_migrations WHERE name=%s', (path.name,)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ValueError('Applied migration checksum changed')
                continue
            conn.execute(content.decode())
            conn.execute('INSERT INTO public.ecosystem_migrations(name,sha256) VALUES (%s,%s)', (path.name,digest))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA ecosystem TO {}').format(sql.Identifier(app_role)))
        for table in ('organizations','memberships','organization_domains'):
            conn.execute(sql.SQL('GRANT SELECT ON ecosystem.{} TO {}').format(sql.Identifier(table),sql.Identifier(app_role)))
        for table in ('items','idempotency','outbox'):
            conn.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE ON ecosystem.{} TO {}').format(sql.Identifier(table),sql.Identifier(app_role)))
        for table in ('events','audit_log'):
            conn.execute(sql.SQL('GRANT SELECT, INSERT ON ecosystem.{} TO {}').format(sql.Identifier(table),sql.Identifier(app_role)))


        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA meetings TO {}').format(sql.Identifier(app_role)))
        for table in ('sources','source_versions','batch_entries','check_results','publications'):
            conn.execute(sql.SQL('GRANT SELECT, INSERT ON meetings.{} TO {}').format(sql.Identifier(table),sql.Identifier(app_role)))
        for table in ('tenant_state','batches','runs'):
            conn.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE ON meetings.{} TO {}').format(sql.Identifier(table),sql.Identifier(app_role)))


if __name__ == '__main__':
    migrate(os.environ['DATABASE_ADMIN_URL'], os.environ['DATABASE_APP_ROLE'])
    print('Migrations applied and checksums verified.')
