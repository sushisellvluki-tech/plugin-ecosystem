"""Synthetic-only workforce preview, authorization and real MCP regression tests."""
from copy import deepcopy
import hashlib
import json
import socket
import threading
import time
import unittest

import httpx2
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from core import AccessPolicy, Core, Registry
from plugins.workforce import plugin
from plugins.workforce.preview import preview
from server.app import create_app
from server.auth import TokenVerifier


def row(**changes):
    return {'source_key': 'synthetic:sheet:2', 'person_id': 'person-1',
            'project_id': 'site-1', 'engagement_id': 'engagement-1',
            'work_date': '2026-09-30', 'minutes': 600,
            'record_type': 'actual', **changes}


def batch(*rows, **changes):
    return {'as_of_date': '2026-10-05', 'expected_month': '2026-09',
            'records': list(rows or [row()]), **changes}


def codes(result):
    return {issue['code'] for issue in result['issues']}


class PreviewTests(unittest.TestCase):
    def test_clean_rows_still_cannot_be_posted(self):
        data = batch()
        before = deepcopy(data)
        result = preview(data)
        self.assertEqual(result['rows_without_detected_errors'], 1)
        self.assertEqual(result['issues'], [])
        self.assertFalse(result['ready_to_post'])
        self.assertFalse(result['references_verified'])
        self.assertEqual(data, before)
        self.assertEqual(preview(data), result)

    def test_missing_references_future_actual_and_period(self):
        result = preview(batch(row(person_id=None, project_id=None,
            engagement_id=None, work_date='2026-10-06')))
        self.assertEqual(codes(result), {'MISSING_PERSON_ID', 'MISSING_PROJECT_ID',
            'MISSING_ENGAGEMENT_ID', 'FUTURE_ACTUAL', 'PERIOD_MISMATCH'})
        self.assertEqual(result['rows_with_errors'], 1)

    def test_invalid_dates_and_leap_day(self):
        for value in (None, '2026-02-29', '20261005', '05.10.2026', '2026-13-01'):
            with self.subTest(value=value):
                self.assertIn('INVALID_DATE', codes(preview(batch(row(work_date=value)))))
        result = preview(batch(row(work_date='2024-02-29'), expected_month='2024-02'))
        self.assertFalse(result['has_blockers'])

    def test_plan_and_absence_not_treated_as_work(self):
        plan = preview(batch(row(work_date='2026-10-06', record_type='plan'), expected_month='2026-10'))
        self.assertEqual(codes(plan), {'PLAN_EXCLUDED'})
        self.assertFalse(plan['has_blockers'])
        absence = preview(batch(row(record_type='absence', minutes=0, project_id=None)))
        self.assertEqual(absence['issues'], [])
        self.assertIn('ABSENCE_WITH_TIME', codes(preview(batch(row(record_type='absence')))))
        self.assertIn('UNKNOWN_RECORD_TYPE', codes(preview(batch(row(record_type='unknown')))))

    def test_minutes_boundary_and_fractional_input(self):
        for value in (None, -1, 1441, 10.5, True):
            with self.subTest(value=value):
                data = batch(row(minutes=value))
                if value is True or value == 10.5:
                    with self.assertRaises(ValueError):
                        preview(data)
                else:
                    self.assertIn('INVALID_MINUTES', codes(preview(data)))
        self.assertIn('ZERO_WORK_TIME', codes(preview(batch(row(minutes=0)))))
        self.assertFalse(preview(batch(row(minutes=1440)))['has_blockers'])

    def test_transfer_between_sites_and_daily_limit(self):
        second = row(source_key='synthetic:sheet:3', project_id='site-2', minutes=480)
        self.assertFalse(preview(batch(row(minutes=480), second))['has_blockers'])
        result = preview(batch(row(minutes=1000), second))
        self.assertEqual(result['rows_with_errors'], 2)
        self.assertIn('DAILY_TIME_EXCEEDED', codes(result))

    def test_duplicate_source_marks_both_rows(self):
        result = preview(batch(row(minutes=300), row(minutes=301)))
        self.assertEqual(result['rows_with_errors'], 2)
        self.assertEqual(codes(result), {'DUPLICATE_SOURCE_ROW'})

    def test_similar_rows_only_warn_and_are_never_removed(self):
        result = preview(batch(row(minutes=300), row(source_key='synthetic:sheet:3', minutes=300)))
        self.assertEqual(codes(result), {'POSSIBLE_DUPLICATE'})
        self.assertEqual(result['total_rows'], 2)
        self.assertFalse(result['has_blockers'])

    def test_work_absence_conflict(self):
        result = preview(batch(row(), row(source_key='synthetic:sheet:3',
                              record_type='absence', project_id=None, minutes=0)))
        self.assertEqual(codes(result), {'WORK_ABSENCE_CONFLICT'})
        self.assertEqual(result['rows_with_errors'], 2)

    def test_person_days_do_not_mix(self):
        result = preview(batch(row(minutes=1000), row(source_key='synthetic:sheet:3',
                              person_id='person-2', minutes=1000)))
        self.assertFalse(result['has_blockers'])

    def test_invalid_envelope_bounds_and_no_raw_values_in_output(self):
        for data in (batch(records=[]), batch(records=[row()]*501),
                     batch(organization_id='foreign'), batch(as_of_date='bad'),
                     batch(expected_month='2026-13'), batch(row(source_key='x'*201))):
            with self.assertRaises(ValueError):
                preview(data)
        result = preview(batch(row(person_id='Confidential Person', source_key='')))
        self.assertIn('INVALID_PERSON_ID', codes(result))
        self.assertIn('INVALID_SOURCE_KEY', codes(result))
        self.assertNotIn('Confidential Person', json.dumps(result))


class CorePreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry, self.policy = Registry(), AccessPolicy()
        self.registry.register(plugin)
        self.policy.grant('tenant-a', 'actor-a', ['workforce:read', 'workforce:preview'])
        self.policy.enable('tenant-a', ['workforce'])
        self.core = Core(self.registry, self.policy)
        self.context = {'organization_id': 'tenant-a', 'actor_id': 'actor-a'}

    async def test_dispatch_and_all_exports_share_one_schema(self):
        result = await self.core.dispatch(plugin, 'workforce__preview_timesheet', batch(), self.context)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data'], preview(batch()))
        r = self.core.catalog(self.context, 'responses')[1]
        c = self.core.catalog(self.context, 'chat_completions')[1]['function']
        m = self.core.catalog(self.context, 'mcp')[1]
        self.assertEqual(r['parameters'], c['parameters'])
        self.assertEqual(c['parameters'], m['inputSchema'])

    async def test_membership_scope_and_revocation(self):
        result = await self.core.dispatch(plugin, 'workforce__preview_timesheet', batch(),
            {**self.context, 'organization_id': 'tenant-b', 'scopes': ['workforce:preview']})
        self.assertEqual(result['error']['code'], 'FORBIDDEN')
        self.policy.grant('tenant-a', 'actor-a', ['workforce:read'])
        result = await self.core.dispatch(plugin, 'workforce__preview_timesheet', batch(), self.context)
        self.assertEqual(result['error']['code'], 'FORBIDDEN')
        self.policy.revoke('tenant-a', 'actor-a')
        self.assertFalse((await self.core.dispatch(plugin, 'workforce__describe', {}, self.context))['ok'])

    async def test_extra_fields_and_limit_rejected_by_core(self):
        for data in (batch(organization_id='tenant-b'), batch(row(minutes=True)),
                     batch(row(source_key='x'*70000))):
            result = await self.core.dispatch(plugin, 'workforce__preview_timesheet', data, self.context)
            self.assertEqual(result['error']['code'], 'INVALID_ARGUMENT')


class LivePreviewTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        registry, policy = Registry(), AccessPolicy()
        registry.register(plugin)
        policy.grant('tenant-a', 'actor-a', ['workforce:read', 'workforce:preview'])
        policy.enable('tenant-a', ['workforce'])
        verifier = TokenVerifier([{'sha256': hashlib.sha256(b'synthetic-preview-key').hexdigest(),
            'expires_at': time.time()+60, 'organization_id': 'tenant-a', 'sub': 'actor-a',
            'scope': 'workforce:read workforce:preview'}])
        app = create_app(Core(registry, policy), [plugin], verifier)
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
            raise RuntimeError('Preview test server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(10)
        cls.sock.close()
        if cls.thread.is_alive():
            raise RuntimeError('Preview test server did not stop')

    async def test_http_and_official_mcp_return_same_preview(self):
        data = batch(row(project_id=None))
        async with httpx2.AsyncClient(base_url=self.url, trust_env=False,
                headers={'Authorization': 'Bearer synthetic-preview-key'}) as http:
            response = await http.post('/workforce/v1/tools/invoke',
                json={'name': 'workforce__preview_timesheet', 'arguments': data})
            self.assertEqual(response.status_code, 200)
            async with streamable_http_client(self.url+'/workforce/mcp', http_client=http) as streams:
                async with ClientSession(*streams, read_timeout_seconds=10) as client:
                    await client.initialize()
                    self.assertIn('workforce__preview_timesheet', [t.name for t in (await client.list_tools()).tools])
                    result = await client.call_tool('workforce__preview_timesheet', data)
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content, response.json()['data'])
                    self.assertTrue(result.structured_content['has_blockers'])


if __name__ == '__main__':
    unittest.main()
