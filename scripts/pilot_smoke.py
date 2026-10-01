"""Exercise the actual Compose pilot using synthetic data and private generated keys."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import httpx2
from deploy.manage import compose,backup,restore


def require(condition,message):
    if not condition:raise AssertionError(message)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('private',type=Path);parser.add_argument('backup_path',type=Path);args=parser.parse_args()
    port=int(os.environ.get('PILOT_HTTP_PORT','8000'))
    root=args.private
    tokens={actor:(root/(actor+'.key')).read_text().strip() for actor in ('author','reviewer')}
    with httpx2.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=15) as client:
        require(client.get('/readyz').status_code==200,'Runtime not ready')
        require(client.get('/cabinet').status_code==200,'Cabinet unavailable')
        require(client.get('/meetings/v1/session').status_code==401,'Missing credential accepted')
        for actor in tokens:
            session=client.get('/meetings/v1/session',headers={'Authorization':'Bearer '+tokens[actor]}).json()
            require(session['actor_id']==actor,'Wrong identity')
            require(('meetings__review_run' in session['tools'])==(actor=='reviewer'),'Role catalog mismatch')
        def invoke(actor,action,arguments,key):
            return client.post('/meetings/v1/tools/invoke',headers={'Authorization':'Bearer '+tokens[actor],'Idempotency-Key':key},json={'name':'meetings__'+action,'arguments':arguments})
        body={'source_account_id':'compose-fixture','sources':[{'filename':'synthetic.txt','format':'text/plain','text':'Synthetic restart and backup check.','external_id':'compose-fixture'}]}
        first=invoke('author','import_batch',body,'compose-import')
        require(first.status_code==200 and first.json()['ok'],'Author import failed')
        batch=first.json()['data']
        denied=invoke('reviewer','import_batch',body,'forbidden-import')
        require(denied.status_code==403,'Reviewer could import')
        # Runtime mounts only the restricted connection string and hashed token config.
        # Test actual container UID and ensure admin secret is absent, without printing secrets.
        compose('exec','-T','app','python','-c',"import os; from pathlib import Path; assert os.getuid()==10001; assert not Path('/run/secrets/postgres_password').exists(); assert not Path('/run/secrets/app_password').exists()")
        compose('restart','app')
        import time
        for _ in range(60):
            try:
                if client.get('/readyz').status_code==200:break
            except httpx2.HTTPError:pass
            time.sleep(1)
        fetched=invoke('author','get_batch',{'batch_id':batch['batch_id']},'read')
        require(fetched.status_code==200 and fetched.json()['data']==batch,'Data lost after app restart')
        repeat=invoke('author','import_batch',body,'compose-import')
        require(repeat.json()==first.json(),'Replay changed after restart')
        backup(args.backup_path)
        restore(args.backup_path,'restore_pilot_smoke')
        result=compose('exec','-T','db','psql','-U','postgres','-d','restore_pilot_smoke','-Atc',
            "SELECT (SELECT count(*) FROM meetings.batches),(SELECT count(*) FROM meetings.source_versions),(SELECT count(*) FROM ecosystem.idempotency), (SELECT count(*) FROM pg_policies WHERE schemaname='meetings')",capture_output=True,text=True)
        require(result.stdout.strip()=='1|1|1|8','Restore counts or RLS policies differ')
        compose('run','--rm','bootstrap','python','-m','deploy.admin','revoke','author')
        require(invoke('author','get_batch',{'batch_id':batch['batch_id']},'revoked').status_code==403,'Revocation failed')
        compose('run','--rm','bootstrap')
        require(invoke('author','get_batch',{'batch_id':batch['batch_id']},'still-revoked').status_code==403,'Bootstrap reactivated revoked membership')
    print('PILOT_RESULT '+json.dumps({'status':'passed','checks':['real_container_boot','restricted_runtime_uid','separate_author_reviewer','database_ready','no_admin_secret_in_runtime','restart_persistence','idempotency_after_restart','backup_restore_counts_and_rls','revocation_survives_bootstrap']}))


if __name__=='__main__':main()
