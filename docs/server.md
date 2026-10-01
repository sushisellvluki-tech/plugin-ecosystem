# HTTP + MCP host (Заход 4)

Python 3.12 recommended. Install `requirements.txt`. The host uses the official
MCP Python SDK 2.2.0, Streamable HTTP, stateless requests and JSON responses.
Both protocols call `core.ports` and the same registry/dispatcher. Legacy SSE is
not enabled. HTTP function exports are not a `/chat/completions` model endpoint.

## Local diagnostic run

From the repository root, create the diagnostic domain once:

```bash
bash scripts/new-plugin.sh meetings "$PWD"
python -m pip install -r requirements.txt
```

Create a private config outside the repository. This example writes a fresh
short-lived credential to your terminal once; keep it out of Git and logs:

```bash
python - <<'PY'
import hashlib, json, os, secrets, time
from pathlib import Path
token = secrets.token_urlsafe(32)
config = {
    "plugins": ["plugins.meetings.plugin"],
    "allowed_hosts": ["127.0.0.1", "localhost"],
    "allowed_origins": [],
    "service_keys": [{"sha256": hashlib.sha256(token.encode()).hexdigest(),
        "expires_at": int(time.time()) + 3600,
        "organization_id": "demo-org", "sub": "demo-user", "scope": "meetings:read"}],
    "demo_memberships": [{"organization_id": "demo-org", "actor_id": "demo-user",
        "scopes": ["meetings:read"], "domains": ["meetings"]}]
}
path = Path.home() / 'ecosystem-server-demo.json'
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
    json.dump(config, f, indent=2)
print('Config:', path)
print('Bearer token (save privately):', token)
PY
python -m server --config "$HOME/ecosystem-server-demo.json" --demo
```

The default listener is `127.0.0.1:8000`. Demo memberships are independent of token
scopes; both must permit a call. Demo mode has no durable writes.

Routes:

| Route | Purpose |
|---|---|
| `GET /healthz` | Public process liveness; not database readiness |
| `GET /meetings/v1/tools` | Responses and Chat Completions function definitions |
| `POST /meetings/v1/tools/invoke` | JSON `{"name":"meetings__describe","arguments":{}}` |
| `/meetings/mcp` | Official Streamable HTTP MCP endpoint |

Pass `Authorization: Bearer <token>` on every protected request. Token-bearing
URLs are rejected. The HTTP body cannot supply identity or scopes.
For durable writes pass `Idempotency-Key` in the request header; the database
validates the key, actor and payload. A dedicated request/HTTP client per write
avoids sharing a key between unrelated operations.

## PostgreSQL and authorization

Without `--demo`, `DATABASE_URL` is mandatory. Follow [postgres.md](postgres.md)
for migrations, a restricted runtime role, memberships and enabled domains.
`demo_memberships` is ignored in this mode. Plugins listed in config are trusted
Python modules; loading arbitrary third-party code is not sandboxed.

Service credentials contain SHA-256 hashes, explicit expiry, organization, actor
and scopes. Use high-entropy random tokens. Removing a hash requires a restart;
database membership revocation is checked on each catalog request and call.

JWT access tokens are optional. Add:

```json
{
  "jwt": {
    "issuer": "https://your-issuer.example",
    "audience": "https://your-service.example",
    "public_key_file": "/absolute/private-config/issuer-public.pem"
  },
  "oauth_resource": "https://your-service.example"
}
```

The verifier accepts only RS256 and validates the signature, issuer, audience,
expiry and issued-at time. Required claims: `iss`, `aud`, `exp`, `iat`, `sub`,
`organization_id`, and space-separated `scope`. The token's organization claim
must be issued by your trusted provider, not chosen freely by its users.

When `oauth_resource` is configured, the host serves
`/.well-known/oauth-protected-resource` and advertises it in 401 challenges.
The resource must be an HTTPS origin exactly matching the configured audience.
This is a resource server, not an OAuth authorization server. Provider setup,
PKCE/client registration, consent and JWKS rotation remain deployment work.
The pinned public key must be replaced and the host restarted during rotation.

## Exposure and verification

Use a TLS reverse proxy before external access; configure explicit allowed hosts
and browser origins. Proxy identity headers are not trusted. No CORS browser
flow is provided yet. Health does not expose credentials. Server access logs are
disabled by the CLI. Configure proxy logging to avoid credential leakage.

The host limits request bodies to 128 KiB and body reception to 10 seconds;
tool argument/result/time limits remain in core. Quotas, external deployment,
client-specific onboarding and an OAuth provider are not bundled.

```bash
python -m unittest discover -s tests -v
```

`tests/test_server.py` starts Uvicorn on a real loopback socket and uses the
official SDK client to initialize, list and call tools, including invalid input
and denied scopes. It also checks JWT validation, expired service keys, tenant
membership, Host/Origin checks and body limits. PostgreSQL tests require the
separate environment described in `postgres.md`; GitHub Actions supplies it.

SDK reference: https://py.sdk.modelcontextprotocol.io/run/asgi/

Verified in [GitHub Actions run 36920680040](https://github.com/sushisellvluki-tech/plugin-ecosystem/actions/runs/36920680040):
46 tests passed, none skipped, with PostgreSQL 16. This includes an authenticated
HTTP database write, idempotent replay and rejection after membership revocation.
