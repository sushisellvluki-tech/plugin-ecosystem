"""Read narrowly mounted secrets and start the non-admin application process."""
import json
import os
from pathlib import Path
import uvicorn
from server.__main__ import build


def main():
    os.environ['DATABASE_URL']=Path('/run/secrets/database_url').read_text().strip()
    config=json.loads(Path('/run/secrets/server_config').read_text())
    app=build(config)
    uvicorn.run(app,host='0.0.0.0',port=8000,proxy_headers=False,access_log=False)


if __name__=='__main__':
    main()
