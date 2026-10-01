import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from core import Core, Registry, AccessPolicy
from core.ports import bind
from scripts.new_plugin import generate
from scripts.check_tools import RegistrationHost

EMPTY = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}


def fixture(*, read_only=True, handler=None, extra=None):
    spec = {'name': 'demo__read', 'description': 'Test', 'input_schema': deepcopy(EMPTY),
            'output_schema': deepcopy(EMPTY), 'required_scopes': ['demo:read'],
            'read_only': read_only, 'idempotent': True, 'destructive': False, 'open_world': False}
    if extra:
        spec.update(extra)
    async def default(name, args, ctx):
        return {'ok': True, 'data': {}, 'error': None, 'warnings': []}
    return SimpleNamespace(DOMAIN='demo', CONTRACT_VERSION='0.1', tools=lambda: [spec], handle=handler or default)


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry, self.policy = Registry(), AccessPolicy()
        self.policy.grant('org-a', 'alice', ['demo:read'])
        self.policy.enable('org-a', ['demo'])
        self.context = {'organization_id': 'org-a', 'actor_id': 'alice'}
        self.plugin = fixture()
        self.registry.register(self.plugin)
        self.core = Core(self.registry, self.policy, timeout=0.02)

    async def call(self, args=None, context=None, plugin=None):
        return await self.core.dispatch(plugin or self.plugin, 'demo__read', {} if args is None else args,
                                        self.context if context is None else context)

    async def test_success(self):
        self.assertTrue((await self.call())['ok'])

    async def test_missing_identity(self):
        self.assertEqual((await self.call(context={}))['error']['code'], 'UNAUTHENTICATED')

    async def test_cross_organization(self):
        self.assertEqual((await self.call(context={**self.context, 'organization_id': 'org-b'}))['error']['code'], 'FORBIDDEN')

    async def test_forged_scope_and_revocation(self):
        self.policy.grant('org-a', 'alice', [])
        self.assertEqual((await self.call(context={**self.context, 'scopes': ['demo:read']}))['error']['code'], 'FORBIDDEN')
        self.policy.revoke('org-a', 'alice')
        self.assertEqual((await self.call())['error']['code'], 'FORBIDDEN')

    async def test_disabled_domain(self):
        self.policy.enable('org-a', [])
        self.assertEqual(self.core.catalog(self.context), [])
        self.assertEqual((await self.call())['error']['code'], 'FORBIDDEN')

    async def test_arguments_limits_and_types(self):
        for value in ({'organization_id': 'org-b'}, [], {'x': float('nan')}, {1: 'x'}, {'x': 'a'*70000}):
            with self.subTest(value=type(value)):
                self.assertEqual((await self.call(args=value))['error']['code'], 'INVALID_ARGUMENT')

    async def test_unknown_and_spoofed_plugin(self):
        self.assertEqual((await self.call(plugin=fixture()))['error']['code'], 'NOT_FOUND')
        self.assertEqual((await self.core.dispatch(self.plugin, [], {}, self.context))['error']['code'], 'NOT_FOUND')

    async def with_handler(self, handler, *, read_only=True):
        registry = Registry()
        plugin = fixture(handler=handler, read_only=read_only)
        registry.register(plugin)
        return await Core(registry, self.policy, timeout=0.01).dispatch(plugin, 'demo__read', {}, self.context)

    async def test_bad_result_and_exception_redaction(self):
        async def invalid(*args):
            return {'ok': True, 'data': {'secret': 'PRIVATE'}, 'error': None, 'warnings': []}
        async def broken(*args):
            raise RuntimeError('PRIVATE')
        for handler in (invalid, broken):
            result = await self.with_handler(handler)
            self.assertEqual(result['error']['code'], 'INTERNAL_ERROR')
            self.assertNotIn('PRIVATE', str(result))

    async def test_timeout(self):
        async def slow(*args):
            await asyncio.sleep(1)
        self.assertEqual((await self.with_handler(slow))['error']['code'], 'DEPENDENCY_UNAVAILABLE')

    async def test_write_never_runs(self):
        called = []
        async def write(*args):
            called.append(True)
        self.assertEqual((await self.with_handler(write, read_only=False))['error']['code'], 'NOT_IMPLEMENTED')
        self.assertEqual(called, [])

    async def test_catalog_snapshot_and_export_equivalence(self):
        self.plugin.tools()[0]['description'] = 'changed'
        native = self.core.catalog(self.context)
        self.assertEqual(native[0]['description'], 'Test')
        native[0]['input_schema']['type'] = 'array'
        self.assertEqual(self.core.catalog(self.context)[0]['input_schema']['type'], 'object')
        response = self.core.catalog(self.context, 'responses')[0]
        chat = self.core.catalog(self.context, 'chat_completions')[0]['function']
        mcp = self.core.catalog(self.context, 'mcp')[0]
        self.assertEqual(response['parameters'], chat['parameters'])
        self.assertEqual(chat['parameters'], mcp['inputSchema'])

    async def test_duplicate_and_atomic_registration(self):
        with self.assertRaises(ValueError):
            self.registry.register(self.plugin)
        registry = Registry()
        plugin = fixture()
        original = plugin.tools()[0]
        plugin.tools = lambda: [original, {**original, 'name': 'wrong'}]
        with self.assertRaises(ValueError):
            registry.register(plugin)
        self.assertEqual(registry._entries, {})

    async def test_unsupported_schema(self):
        registry = Registry()
        with self.assertRaises(ValueError):
            registry.register(fixture(extra={'input_schema': {**EMPTY, '$ref': 'https://example.org/schema'}}))

    async def test_generated_plugin_through_both_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate('meetings', Path(tmp)) / 'plugin.py'
            spec = importlib.util.spec_from_file_location('generated_plugin', path)
            plugin = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(plugin)
            registry = Registry()
            registry.register(plugin)
            self.policy.grant('org-a', 'alice', ['meetings:read'])
            self.policy.enable('org-a', ['meetings'])
            core = Core(registry, self.policy)
            host = RegistrationHost()
            bind(host, core, plugin)
            http = await host.posts['/meetings/v1/tools/invoke']({'name': 'meetings__describe', 'arguments': {}}, self.context)
            mcp = await host.mcp['/meetings/mcp']['call_tool']('meetings__describe', {}, self.context)
            self.assertTrue(http['ok'])
            self.assertEqual(http['data'], mcp['structuredContent'])
            self.policy.revoke('org-a', 'alice')
            with self.assertRaises(PermissionError):
                await host.mcp['/meetings/mcp']['list_tools'](self.context)
            self.assertTrue((await host.mcp['/meetings/mcp']['call_tool']('meetings__describe', {}, self.context))['isError'])

    async def test_parallel_tenant_contexts(self):
        self.policy.grant('org-b', 'bob', ['demo:read'])
        self.policy.enable('org-b', ['demo'])
        contexts = []
        async def inspect_context(name, args, context):
            await asyncio.sleep(0)
            contexts.append((context['organization_id'], context['actor_id'], context['request_id']))
            return {'ok': True, 'data': {}, 'error': None, 'warnings': []}
        registry = Registry()
        plugin = fixture(handler=inspect_context)
        registry.register(plugin)
        core = Core(registry, self.policy)
        await asyncio.gather(core.dispatch(plugin, 'demo__read', {}, self.context),
                             core.dispatch(plugin, 'demo__read', {}, {'organization_id':'org-b','actor_id':'bob'}))
        self.assertEqual({(o,a) for o,a,r in contexts}, {('org-a','alice'), ('org-b','bob')})
        self.assertEqual(len({r for o,a,r in contexts}), 2)


if __name__ == '__main__':
    unittest.main()
