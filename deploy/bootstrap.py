"""One-shot admin bootstrap. The running app never receives these admin secrets."""
import json
from pathlib import Path
import sys
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from core.db.migrate import migrate

SCOPES = {'author':['meetings:read','meetings:write','meetings:publish'],
          'reviewer':['meetings:read','meetings:review']}


def bootstrap(root=Path('/run/secrets')):
    admin = make_conninfo(host='db',dbname='ecosystem',user='postgres',password=(root/'postgres_password').read_text().strip(),connect_timeout=5)
    password=(root/'app_password').read_text().strip()
    config=json.loads((root/'bootstrap_config').read_text())
    with psycopg.connect(admin) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(8472042027)')
        role=conn.execute("SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolreplication FROM pg_roles WHERE rolname='ecosystem_app'").fetchone()
        if role is None:
            conn.execute(sql.SQL('CREATE ROLE ecosystem_app LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION').format(sql.Literal(password)))
        elif any(role):
            raise ValueError('Unsafe existing application role')
        if conn.execute("SELECT EXISTS(SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname='ecosystem_app')").fetchone()[0]:
            raise ValueError('Application role must not inherit other roles')
    migrate(admin,'ecosystem_app')
    with psycopg.connect(admin) as conn:
        conn.execute('INSERT INTO ecosystem.organizations(id,name) VALUES (%s,%s) ON CONFLICT DO NOTHING',(config['organization_id'],config['organization_name']))
        conn.execute("INSERT INTO ecosystem.organization_domains VALUES (%s,'meetings',true) ON CONFLICT DO NOTHING",(config['organization_id'],))
        for actor,scopes in SCOPES.items():
            # Restart must never reactivate a revoked actor or overwrite manual grants.
            conn.execute('INSERT INTO ecosystem.memberships(organization_id,actor_id,scopes) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(config['organization_id'],actor,scopes))
    # Catch a mismatched persistent role password before starting the runtime.
    with psycopg.connect(make_conninfo(host='db',dbname='ecosystem',user='ecosystem_app',password=password,connect_timeout=5)) as conn:
        conn.execute('SELECT 1')


if __name__=='__main__':
    try:
        bootstrap()
        print('Pilot schema and initial memberships ready. Existing revocations preserved.')
    except Exception as exc:
        print('Bootstrap failed ('+type(exc).__name__+'); check private configuration and database state.',file=sys.stderr)
        raise SystemExit(1)
