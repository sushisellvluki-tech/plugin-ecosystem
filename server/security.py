"""Pure ASGI security boundary. Applies to every MCP request, not just initialize."""
import asyncio
from urllib.parse import parse_qs
from starlette.responses import JSONResponse


class Security:
    def __init__(self, app, verifier, *, resource_metadata=None, allowed_origins=(), max_body=131072):
        self.app,self.verifier=app,verifier
        self.metadata=resource_metadata
        self.origins=frozenset(allowed_origins)
        self.max_body=max_body

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        async def reject(status,code):
            headers={'Cache-Control':'no-store'}
            if status==401:
                headers['WWW-Authenticate']='Bearer'+(f' resource_metadata="{self.metadata}"' if self.metadata else '')
            await JSONResponse({'error':code},status_code=status,headers=headers)(scope,receive,send)
        headers={}
        for k,v in scope.get('headers',[]):
            key=k.decode('latin-1').lower()
            if key in headers and key in ('authorization','content-length','origin','idempotency-key'):
                return await reject(400,'duplicate_security_header')
            headers[key]=v.decode('latin-1')
        origin=headers.get('origin')
        if origin is not None and origin not in self.origins:
            return await reject(403,'origin_denied')
        if any(k in parse_qs(scope.get('query_string',b'').decode('latin-1'), keep_blank_values=True) for k in ('access_token','token','api_key')):
            return await reject(400,'token_in_url_forbidden')
        raw=headers.get('authorization','').split()
        if len(raw)!=2 or raw[0].lower()!='bearer':
            return await reject(401,'unauthenticated')
        try:
            principal=self.verifier.verify(raw[1])
        except ValueError:
            return await reject(401,'unauthenticated')
        domain=scope['path'].strip('/').split('/')[0]
        if not any(s.startswith(domain+':') for s in principal.scopes):
            return await reject(403,'insufficient_scope')
        context=principal.context()
        context['idempotency_key']=headers.get('idempotency-key')
        scope.setdefault('state',{})['identity']=context
        try:
            length = int(headers.get('content-length','0'))
            if length < 0:
                return await reject(400,'invalid_content_length')
            if length>self.max_body:
                return await reject(413,'request_too_large')
        except ValueError:
            return await reject(400,'invalid_content_length')
        body=bytearray()
        async def collect():
            while True:
                message=await receive()
                if message['type']=='http.disconnect':
                    return False
                body.extend(message.get('body',b''))
                if len(body)>self.max_body:
                    return None
                if not message.get('more_body',False):
                    return True
        try:
            done=await asyncio.wait_for(collect(),10)
        except asyncio.TimeoutError:
            return await reject(408,'body_timeout')
        if done is None:
            return await reject(413,'request_too_large')
        if done is False:
            return
        sent=False
        async def replay():
            nonlocal sent
            if not sent:
                sent=True
                return {'type':'http.request','body':bytes(body),'more_body':False}
            return await receive()
        await self.app(scope,replay,send)
