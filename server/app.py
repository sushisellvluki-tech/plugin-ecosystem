"""ASGI adapter over core.ports; the registry remains the sole tool catalog."""
import asyncio
from urllib.parse import urlsplit
from contextlib import AsyncExitStack, asynccontextmanager
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
import mcp.types as types
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from core.ports import bind
from core.runtime import Core, failure
from .security import Security
from .cabinet import routes as cabinet_routes


class ScopedCore:
    def __init__(self, core):
        self.core, self.registry = core, core.registry

    async def catalog_async(self, context, style='native'):
        specs = await self.core.catalog_async(context, 'native')
        granted = set(context.get('_token_scopes', ()))
        return Core.export_specs([s for s in specs if set(s['required_scopes']) <= granted], style)

    async def dispatch(self, plugin, name, arguments, context):
        entry = self.registry._entries.get(name) if isinstance(name, str) else None
        if entry is None or entry.plugin is not plugin:
            return failure('NOT_FOUND', 'Unknown tool')
        if not set(entry.spec['required_scopes']) <= set(context.get('_token_scopes', ())):
            return failure('FORBIDDEN', 'Token scope does not permit this tool')
        return await self.core.dispatch(plugin, name, arguments, context)


class Host:
    def __init__(self, verifier, allowed_hosts, allowed_origins):
        self.routes, self.servers = [], []
        self.verifier = verifier
        self.allowed_hosts, self.allowed_origins = allowed_hosts, allowed_origins

    def add_json_get(self, path, handler):
        async def endpoint(request):
            try:
                result = await handler(request.state.identity)
                return JSONResponse(result, headers={'Cache-Control': 'no-store'})
            except PermissionError:
                return JSONResponse({'error': 'forbidden'}, status_code=403)
            except Exception:
                return JSONResponse({'error': 'service_unavailable'}, status_code=503)
        self.routes.append(Route(path, endpoint, methods=['GET']))

    def add_json_post(self, path, handler):
        async def endpoint(request):
            if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
                return JSONResponse({'error': 'expected_json'}, status_code=415)
            try:
                body = await request.json()
            except (ValueError, UnicodeError, RecursionError):
                return JSONResponse({'error': 'invalid_json'}, status_code=400)
            try:
                result = await handler(body, request.state.identity)
            except Exception:
                return JSONResponse({'error': 'service_unavailable'}, status_code=503)
            code = result['error']['code'] if not result['ok'] else None
            status = {'FORBIDDEN': 403, 'UNAUTHENTICATED': 401, 'NOT_FOUND': 404,
                      'INVALID_ARGUMENT': 400, 'CONFLICT': 409, 'INTERNAL_ERROR': 500,
                      'NOT_IMPLEMENTED': 501, 'TIMEOUT': 504, 'DEPENDENCY_UNAVAILABLE': 503,
                      'RATE_LIMITED': 429}.get(code, 200 if code is None else 500)
            return JSONResponse(result, status_code=status, headers={'Cache-Control': 'no-store'})
        self.routes.append(Route(path, endpoint, methods=['POST']))

    def add_mcp_endpoint(self, *, path, list_tools, call_tool, legacy_sse_path=None):
        if legacy_sse_path:
            raise ValueError('Legacy SSE is not enabled by this host')

        async def listing(ctx, params):
            try:
                rows = await list_tools(ctx.request.state.identity)
            except PermissionError:
                rows = []
            except Exception:
                raise ValueError('Catalog unavailable') from None
            return types.ListToolsResult(tools=[types.Tool(**row) for row in rows])

        async def calling(ctx, params):
            try:
                result = await call_tool(params.name, params.arguments or {}, ctx.request.state.identity)
            except Exception:
                result = {'isError': True, 'content': [{'type': 'text', 'text': 'Tool execution unavailable'}]}
            return types.CallToolResult(**result)

        server = Server(path.strip('/').replace('/', '-'), on_list_tools=listing, on_call_tool=calling)
        transport = TransportSecuritySettings(
            allowed_hosts=[item for h in self.allowed_hosts for item in (h, h + ':*')],
            allowed_origins=list(self.allowed_origins))
        app = server.streamable_http_app(stateless_http=True, json_response=True,
                    transport_security=transport, max_request_body_size=131072)
        self.servers.append(server)
        self.routes.append(Mount(path.rsplit('/', 1)[0], app=app))


def create_app(core, plugins, verifier, *, allowed_hosts=('127.0.0.1', 'localhost'), allowed_origins=(), oauth_resource=None):
    if not allowed_hosts or '*' in allowed_hosts:
        raise ValueError('Explicit allowed hosts are required')
    host = Host(verifier, allowed_hosts, allowed_origins)
    scoped = ScopedCore(core)
    for plugin in plugins:
        async def session(context, domain=plugin.DOMAIN):
            specs = await scoped.catalog_async(context)
            return {'organization_id': context['organization_id'], 'actor_id': context['actor_id'],
                    'tools': [s['name'] for s in specs if core.registry._entries[s['name']].domain == domain]}
        host.add_json_get(f'/{plugin.DOMAIN}/v1/session', session)
        bind(host, scoped, plugin)

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for server in host.servers:
                await stack.enter_async_context(server.session_manager.run())
            yield

    async def health(request):
        return JSONResponse({'status': 'up'})

    async def ready(request):
        if core.persistence is None:
            return JSONResponse({'status':'demo'},status_code=503,headers={'Cache-Control':'no-store'})
        try:
            await asyncio.wait_for(core.persistence.ready(),5)
            return JSONResponse({'status':'ready'},headers={'Cache-Control':'no-store'})
        except Exception:
            return JSONResponse({'status':'unavailable'},status_code=503,headers={'Cache-Control':'no-store'})

    public_routes = [Route('/healthz', health), Route('/readyz', ready), *cabinet_routes()]
    metadata_url = None
    if oauth_resource:
        parsed = urlsplit(oauth_resource)
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or parsed.username or parsed.password
                or not verifier.issuer or verifier.audience != oauth_resource):
            raise ValueError('OAuth resource must be an HTTPS origin matching the JWT audience')
        metadata_url = oauth_resource.rstrip('/') + '/.well-known/oauth-protected-resource'
        async def metadata(request):
            return JSONResponse({'resource': oauth_resource,
                'authorization_servers': [verifier.issuer], 'bearer_methods_supported': ['header'],
                'scopes_supported': sorted({s for e in core.registry._entries.values() for s in e.spec['required_scopes']})})
        public_routes.append(Route('/.well-known/oauth-protected-resource', metadata))
    protected = Security(Starlette(routes=host.routes), verifier, allowed_origins=allowed_origins,
                         resource_metadata=metadata_url, allow_same_origin=True)
    app = Starlette(routes=[*public_routes, Mount('/', app=protected)], lifespan=lifespan)
    return TrustedHostMiddleware(app, allowed_hosts=list(allowed_hosts), www_redirect=False)
