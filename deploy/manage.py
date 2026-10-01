"""Operate the private pilot. Restore always targets a NEW restore_* database."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]


def compose(*args, **kwargs):
    project=os.environ.get('COMPOSE_PROJECT_NAME','plugin-pilot')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,60}',project):
        raise ValueError('Invalid Compose project name')
    return subprocess.run(['docker','compose','--project-name',project,'-f',str(ROOT/'deploy/compose.yaml'),*args],check=True,**kwargs)


def backup(path):
    path=Path(path).expanduser().absolute()
    if path.with_suffix(path.suffix+'.sha256').exists():
        raise ValueError('Checksum file already exists')
    with os.fdopen(os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb') as stream:
        try:
            compose('exec','-T','db','pg_dump','-U','postgres','-d','ecosystem','--format=custom','--no-owner',stdout=stream)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    with path.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    with path.with_suffix(path.suffix+'.sha256').open('x') as stream:
        stream.write(digest+'\n')
    print('Backup and SHA-256 written:',path)


def restore(path, database):
    path=Path(path).expanduser().absolute()
    if not re.fullmatch(r'restore_[a-z0-9_]{1,55}',database):
        raise ValueError('Restore destination must be a NEW restore_* database')
    expected=path.with_suffix(path.suffix+'.sha256').read_text().strip()
    with path.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=expected:
            raise ValueError('Backup checksum mismatch')
    compose('exec','-T','db','createdb','-U','postgres','--template=template0',database)
    with path.open('rb') as stream:
        compose('exec','-T','db','pg_restore','-U','postgres','--exit-on-error','--single-transaction','--no-owner','-d',database,stdin=stream)
    print('Restored into new database:',database)
    print('The live DATABASE_URL was not changed. Validate before a planned cutover.')


def start():
    port=int(os.environ.get('PILOT_HTTP_PORT','8000'))
    if not 1<=port<=65535:raise ValueError('Invalid port')
    compose('up','--build','-d')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        try:
            with opener.open(f'http://127.0.0.1:{port}/readyz',timeout=6) as response:
                if response.status==200:
                    print(f'Pilot ready: http://127.0.0.1:{port}/cabinet')
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('Readiness timeout; inspect docker compose ps/logs without publishing secrets')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('start');sub.add_parser('stop');sub.add_parser('status')
    dump=sub.add_parser('backup');dump.add_argument('path',type=Path)
    load=sub.add_parser('restore');load.add_argument('path',type=Path);load.add_argument('database')
    revoke=sub.add_parser('revoke');revoke.add_argument('actor',choices=['author','reviewer'])
    args=parser.parse_args()
    if args.action=='start':start()
    elif args.action=='stop':compose('stop')
    elif args.action=='status':compose('ps','--all')
    elif args.action=='backup':backup(args.path)
    elif args.action=='restore':restore(args.path,args.database)
    elif args.action=='revoke':compose('run','--rm','bootstrap','python','-m','deploy.admin','revoke',args.actor)


if __name__=='__main__':main()
