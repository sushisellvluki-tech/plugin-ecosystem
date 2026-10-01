"""Create a NEW private pilot bundle outside the checkout. Never print credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import time
from .bootstrap import SCOPES


def prepare(destination, organization_id='pilot', organization_name='Пилотная организация', hours=168):
    if not organization_id.strip() or len(organization_id)>100 or not organization_name.strip() or len(organization_name)>200:
        raise ValueError('Organization ID/name required and bounded')
    if not 1<=hours<=720:
        raise ValueError('Expiry must be 1 to 720 hours')
    destination=Path(destination).expanduser().absolute()
    source=Path(__file__).resolve().parents[1]
    if destination.resolve()==source or source in destination.resolve().parents:
        raise ValueError('Private bundle must be outside the repository')
    destination.mkdir(mode=0o700)
    try:
        destination.chmod(0o700)
        def write(name,value,mode=0o444):
            with (destination/name).open('x') as stream:stream.write(value+'\n')
            (destination/name).chmod(mode)
        admin_password,app_password=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        write('postgres_password',admin_password)
        write('app_password',app_password)
        write('database_url',f'host=db dbname=ecosystem user=ecosystem_app password={app_password} connect_timeout=5')
        write('bootstrap.json',json.dumps({'organization_id':organization_id,'organization_name':organization_name},ensure_ascii=False,indent=2))
        keys=[]
        for actor,scopes in SCOPES.items():
            token=secrets.token_urlsafe(48)
            write(actor+'.key',token,0o600)
            keys.append({'sha256':hashlib.sha256(token.encode()).hexdigest(),'expires_at':int(time.time())+hours*3600,
                         'organization_id':organization_id,'sub':actor,'scope':' '.join(scopes)})
        write('server.json',json.dumps({'plugins':['plugins.meetings.plugin'],'allowed_hosts':['127.0.0.1','localhost'],
                'allowed_origins':[],'service_keys':keys},ensure_ascii=False,indent=2))
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',type=Path)
    parser.add_argument('--org',default='pilot')
    parser.add_argument('--name',default='Пилотная организация')
    parser.add_argument('--hours',type=int,default=168)
    args=parser.parse_args()
    result=prepare(args.destination,args.org,args.name,args.hours)
    print('Private pilot bundle created:',result)
    print('Separate author.key and reviewer.key generated. Credentials were not printed.')


if __name__=='__main__':main()
