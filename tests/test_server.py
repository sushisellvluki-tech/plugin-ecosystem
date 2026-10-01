"""Real loopback HTTP + official MCP client; no mocked transport."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import httpx2
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client
from core import Core, Registry, AccessPolicy
from scripts.new_plugin import generate
from server.app import create_app
from server.auth import TokenVerifier


def credential(token, scope='meetings:read', org='org', actor='actor', expires=None):
    return {'sha256': hashlib.sha256(token.encode()).hexdigest(), 'expires_at': expires or time.time()+3600,
            'organization_id': org, 'sub': actor, 'scope': scope}


class AuthenticationTests(unittest.TestCase):
    def test_service_key_expiry_and_unknown(self):
        verifier = TokenVerifier([credential('valid'), credential('old', expires=1)])
        self.assertEqual(verifier.verify('valid').organization_id, 'org')
        for token in ('old', 'unknown', ''):
            with self.assertRaises(ValueError):
                verifier.verify(token)

    def test_jwt_signature_issuer_audience_and_expiry(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        verifier = TokenVerifier(issuer='https://issuer.example', audience='ecosystem', public_key=key.public_key())
        claims = {'iss': 'https://issuer.example', 'aud': 'ecosystem', 'iat': int(time.time()),
                  'exp': int(time.time())+60, 'sub': 'actor', 'organization_id': 'org', 'scope': 'meetings:read'}
        self.assertEqual(verifier.verify(jwt.encode(claims, key, algorithm='RS256')).actor_id, 'actor')
        for patch in ({'iss': 'https://evil.example'}, {'aud': 'other'}, {'exp': 1}, {'scope': '*'}, {'organization_id': ''}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                verifier.verify(jwt.encode(claims | patch, key, algorithm='RS256'))
        with self.assertRaises(ValueError):
            verifier.verify(jwt.encode(claims, other, algorithm='RS256'))
        with self.assertRaises(ValueError):
            TokenVerifier()


    def test_oauth_resource_metadata_and_challenge(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        verifier = TokenVerifier(issuer='https://issuer.example', audience='https://service.example', public_key=key.public_key())
        app = create_app(Core(Registry(), AccessPolicy()), [], verifier, oauth_resource='https://service.example')
        async def verify():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://localhost', trust_env=False) as client:
                response = await client.get('/.well-known/oauth-protected-resource')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['resource'], 'https://service.example')
                challenge = await client.get('/meetings/mcp')
                self.assertEqual(challenge.status_code, 401)
                self.assertIn('https://service.example/.well-known/oauth-protected-resource', challenge.headers['www-authenticate'])
        asyncio.run(verify())
        with self.assertRaises(ValueError):
            create_app(Core(Registry(), AccessPolicy()), [], verifier, oauth_resource='http://unsafe.example')


class LiveServerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = generate('meetings', Path(cls.temp.name))/'plugin.py'
        spec = importlib.util.spec_from_file_location('live_meetings', path)
        cls.plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.plugin)
        registry, cls.policy = Registry(), AccessPolicy()
        registry.register(cls.plugin)
        cls.policy.grant('org', 'actor', ['meetings:read'])
        cls.policy.enable('org', ['meetings'])
        verifier = TokenVerifier([credential('good'), credential('limited', 'meetings:write'),
                                  credential('outsider', org='other'), credential('expired', expires=1)])
        app = create_app(Core(registry, cls.policy), [cls.plugin], verifier)
        cls.sock = socket.socket()
        cls.sock.bind(('127.0.0.1', 0))
        cls.url = 'http://127.0.0.1:' + str(cls.sock.getsockname()[1])
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, proxy_headers=False))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={'sockets': [cls.sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic()+10
        while not cls.server.started and cls.thread.is_alive() and time.monotonic()<deadline:
            time.sleep(.01)
        if not cls.server.started:
            raise RuntimeError('Loopback server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(10)
        cls.sock.close()
        cls.temp.cleanup()
        if cls.thread.is_alive():
            raise RuntimeError('Server failed to stop')

    def client(self, token='good'):
        return httpx2.AsyncClient(base_url=self.url, headers=({'Authorization': 'Bearer '+token} if token else {}), timeout=10, trust_env=False)

    async def test_http_catalog_and_invocation(self):
        async with self.client() as client:
            catalog = await client.get('/meetings/v1/tools')
            self.assertEqual(catalog.status_code, 200, catalog.text)
            self.assertEqual(catalog.json()['responses'][0]['name'], 'meetings__describe')
            result = await client.post('/meetings/v1/tools/invoke', json={'name': 'meetings__describe', 'arguments': {}})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(result.json()['ok'])
            forged = await client.post('/meetings/v1/tools/invoke', json={'name': 'meetings__describe', 'arguments': {}, 'organization_id': 'other'})
            self.assertEqual(forged.status_code, 400)

    async def test_auth_boundary_on_http_and_mcp(self):
        for token in ('unknown', 'expired', ''):
            async with self.client(token) as client:
                for path in ('/meetings/v1/tools', '/meetings/mcp'):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers['www-authenticate'], 'Bearer')

    async def test_scope_and_membership_are_both_required(self):
        async with self.client('limited') as client:
            catalog = await client.get('/meetings/v1/tools')
            self.assertEqual(catalog.json()['responses'], [])
            result = await client.post('/meetings/v1/tools/invoke', json={'name': 'meetings__describe', 'arguments': {}})
            self.assertEqual(result.status_code, 403)
        async with self.client('outsider') as client:
            self.assertEqual((await client.get('/meetings/v1/tools')).status_code, 403)

    async def test_host_origin_url_and_body_limits(self):
        async with self.client() as client:
            for headers, status in (({'Host': 'evil.example'}, 400), ({'Origin': 'https://evil.example'}, 403)):
                self.assertEqual((await client.get('/meetings/v1/tools', headers=headers)).status_code, status)
            self.assertEqual((await client.get('/meetings/v1/tools?access_token=')).status_code, 400)
            response = await client.post('/meetings/v1/tools/invoke', content='x'*131073)
            self.assertEqual(response.status_code, 413)
            self.assertEqual((await client.post('/meetings/v1/tools/invoke', content='{', headers={'Content-Type': 'application/json'})).status_code, 400)

    async def test_official_mcp_client_list_call_and_denial(self):
        async with self.client() as http:
            async with streamable_http_client(self.url+'/meetings/mcp', http_client=http) as streams:
                async with ClientSession(*streams, read_timeout_seconds=10) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    self.assertEqual([t.name for t in listed.tools], ['meetings__describe'])
                    result = await client.call_tool('meetings__describe', {})
                    self.assertFalse(result.is_error, result)
                    self.assertEqual(result.structured_content['domain'], 'meetings')
                    bad = await client.call_tool('meetings__describe', {'unexpected': True})
                    self.assertTrue(bad.is_error)
        async with self.client('limited') as http:
            async with streamable_http_client(self.url+'/meetings/mcp', http_client=http) as streams:
                async with ClientSession(*streams, read_timeout_seconds=10) as client:
                    await client.initialize()
                    self.assertEqual((await client.list_tools()).tools, [])
                    self.assertTrue((await client.call_tool('meetings__describe', {})).is_error)

    async def test_modern_client_and_credential_rechecked(self):
        async with self.client() as http:
            transport = streamable_http_client(self.url+'/meetings/mcp', http_client=http)
            async with Client(transport, read_timeout_seconds=10) as client:
                self.assertEqual([t.name for t in (await client.list_tools()).tools], ['meetings__describe'])
                self.assertFalse((await client.call_tool('meetings__describe', {})).is_error)
                # The next wire request must recheck credentials, even on the same client.
                http.headers['Authorization'] = 'Bearer expired'
                with self.assertRaises(Exception):
                    await client.list_tools(cache_mode='bypass')
