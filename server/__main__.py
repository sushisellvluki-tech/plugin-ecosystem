"""Run the configured host: python -m server --config /path/server.json."""
import argparse
import importlib
import json
import os
from pathlib import Path
import uvicorn
from core import Core, Registry, AccessPolicy
from core.db.store import PostgresStore
from .app import create_app
from .auth import TokenVerifier


def build(config, *, demo=False):
    registry, policy = Registry(), AccessPolicy()
    plugins = [importlib.import_module(name) for name in config['plugins']]
    if not plugins:
        raise ValueError('At least one plugin is required')
    for plugin in plugins:
        registry.register(plugin)
    auth = config.get('jwt', {})
    verifier = TokenVerifier(config.get('service_keys', ()), issuer=auth.get('issuer'),
                audience=auth.get('audience'),
                public_key=Path(auth['public_key_file']).read_text() if auth.get('public_key_file') else None)
    if demo:
        for member in config.get('demo_memberships', ()):
            policy.grant(member['organization_id'], member['actor_id'], member['scopes'])
            policy.enable(member['organization_id'], member['domains'])
        store = None
    else:
        dsn = os.environ.get('DATABASE_URL')
        if not dsn:
            raise ValueError('DATABASE_URL required; use --demo explicitly for process-local read-only demo')
        store = PostgresStore(dsn)
    return create_app(Core(registry, policy, persistence=store), plugins, verifier,
                      allowed_hosts=config.get('allowed_hosts', ['127.0.0.1', 'localhost']),
                      allowed_origins=config.get('allowed_origins', []),
                      oauth_resource=config.get('oauth_resource'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    app = build(json.loads(args.config.read_text()), demo=args.demo)
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False, access_log=False)


if __name__ == '__main__':
    main()
