"""Explicit operator action; requires the bootstrap service's administrative secrets."""
import argparse
import json
from pathlib import Path
import psycopg
from psycopg.conninfo import make_conninfo
from .bootstrap import SCOPES


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['revoke'])
    parser.add_argument('actor',choices=list(SCOPES))
    args=parser.parse_args()
    root=Path('/run/secrets')
    config=json.loads((root/'bootstrap_config').read_text())
    dsn=make_conninfo(host='db',dbname='ecosystem',user='postgres',password=(root/'postgres_password').read_text().strip(),connect_timeout=5)
    with psycopg.connect(dsn) as conn:
        conn.execute('UPDATE ecosystem.memberships SET active=false WHERE organization_id=%s AND actor_id=%s',(config['organization_id'],args.actor))
    print('Membership revoked. Subsequent calls are denied; bootstrap will not reactivate it.')


if __name__=='__main__':main()
