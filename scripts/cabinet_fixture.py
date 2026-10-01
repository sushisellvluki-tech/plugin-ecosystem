"""Private stdio fixture for browser CI. Never starts against a non-opted-in DB."""
import hashlib
import json
import os
import socket
import sys
import threading
import time
from uuid import uuid4
import psycopg
import uvicorn
from core import Core,Registry
from core.db import PostgresStore
from core.db.migrate import migrate
from plugins.meetings import plugin
from server.app import create_app
from server.auth import TokenVerifier


def main():
    if os.environ.get('TEST_DATABASE_DISPOSABLE')!='yes':
        raise RuntimeError('Disposable database opt-in required')
    admin,app=os.environ['TEST_DATABASE_ADMIN_URL'],os.environ['TEST_DATABASE_URL']
    migrate(admin,'ecosystem_app')
    org='browser-'+uuid4().hex;other='browser-other-'+uuid4().hex
    tokens={actor:uuid4().hex+uuid4().hex for actor in ('author','reviewer','outsider')}
    keys=[]
    with psycopg.connect(admin) as conn:
        for tenant in (org,other):
            conn.execute('INSERT INTO ecosystem.organizations VALUES (%s,%s)',(tenant,'Synthetic browser fixture'))
            conn.execute("INSERT INTO ecosystem.organization_domains VALUES (%s,'meetings',true)",(tenant,))
        for actor in tokens:
            tenant=other if actor=='outsider' else org
            scopes=['meetings:read']+(['meetings:write','meetings:publish'] if actor=='author' else ['meetings:review'] if actor=='reviewer' else [])
            conn.execute('INSERT INTO ecosystem.memberships(organization_id,actor_id,scopes) VALUES (%s,%s,%s)',(tenant,actor,scopes))
            keys.append({'sha256':hashlib.sha256(tokens[actor].encode()).hexdigest(),'expires_at':time.time()+600,
                         'organization_id':tenant,'sub':actor,'scope':' '.join(scopes)})
    registry=Registry();registry.register(plugin)
    application=create_app(Core(registry,None,persistence=PostgresStore(app)),[plugin],TokenVerifier(keys))
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(application,log_level='error',access_log=False,proxy_headers=False))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:
            time.sleep(.01)
        if not server.started:raise RuntimeError('Server not started')
        print(json.dumps({'url':f'http://127.0.0.1:{sock.getsockname()[1]}','tokens':tokens}),flush=True)
        sys.stdin.read()
    finally:
        server.should_exit=True;thread.join(10);sock.close()


if __name__=='__main__':
    main()
