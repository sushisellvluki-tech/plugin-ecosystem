"""One registry and dispatcher for trusted, installed Python plugins.

Host must authenticate actor_id before calling these APIs. Never bind request JSON
as trusted context. This process-local policy is not an OAuth implementation.
"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import inspect
import json
import logging
import re
from uuid import uuid4

from jsonschema import Draft202012Validator
from scripts.check_tools import strict_subset

LOG = logging.getLogger(__name__)
CODES = frozenset({'UNAUTHENTICATED', 'FORBIDDEN', 'INVALID_ARGUMENT', 'NOT_FOUND',
                  'CONFLICT', 'NOT_IMPLEMENTED', 'RATE_LIMITED',
                  'DEPENDENCY_UNAVAILABLE', 'INTERNAL_ERROR'})


def failure(code, message):
    return {'ok': False, 'data': None,
            'error': {'code': code, 'message': message, 'retryable': code in {'DEPENDENCY_UNAVAILABLE', 'RATE_LIMITED'}}, 'warnings': []}


def json_copy(value, limit):
    # Strict JSON roundtrip rejects NaN and detaches mutable handler inputs/outputs.
    def check(v):
        if type(v) is dict:
            if any(type(k) is not str for k in v):
                raise ValueError('Non-string JSON key')
            for child in v.values():
                check(child)
        elif type(v) is list:
            for child in v:
                check(child)
        elif type(v) not in (str, int, float, bool, type(None)):
            raise ValueError('Not JSON')
    check(value)
    encoded = json.dumps(value, allow_nan=False, ensure_ascii=False).encode('utf-8')
    if len(encoded) > limit:
        raise ValueError('JSON size limit exceeded')
    return json.loads(encoded)


@dataclass(frozen=True)
class Entry:
    domain: str
    plugin: object
    handler: object
    spec: dict
    input_validator: object
    output_validator: object


class Registry:
    """Atomic plugin registration. Catalogs are detached copies of a frozen snapshot."""
    def __init__(self):
        self._entries = {}
        self._domains = set()

    def register(self, plugin):
        domain = getattr(plugin, 'DOMAIN', None)
        if not isinstance(domain, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', domain):
            raise ValueError('Invalid domain')
        if domain in self._domains or getattr(plugin, 'CONTRACT_VERSION', None) != '0.1':
            raise ValueError('Duplicate domain or unsupported contract')
        if not callable(getattr(plugin, 'tools', None)) or not inspect.iscoroutinefunction(getattr(plugin, 'handle', None)):
            raise ValueError('Expected tools() and async handle()')
        specs = json_copy(plugin.tools(), 1024 * 1024)
        if not isinstance(specs, list) or not specs:
            raise ValueError('Empty tool catalog')
        pending = {}
        for spec in specs:
            if not isinstance(spec, dict):
                raise ValueError('Invalid tool')
            name = spec.get('name')
            if (not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name)
                    or not name.startswith(domain + '__') or name in pending or name in self._entries):
                raise ValueError('Invalid or duplicate name')
            if not isinstance(spec.get('description'), str) or not spec['description'].strip():
                raise ValueError('Description required')
            scopes = spec.get('required_scopes')
            if not isinstance(scopes, list) or not scopes or any(not isinstance(s, str) or not s.startswith(domain + ':') or s == domain + ':' for s in scopes):
                raise ValueError('Domain scopes required')
            for flag in ('read_only', 'idempotent', 'destructive', 'open_world'):
                if type(spec.get(flag)) is not bool:
                    raise ValueError('Boolean flags required')
            if spec['read_only'] and spec['destructive']:
                raise ValueError('Contradictory safety flags')
            validators = []
            for key in ('input_schema', 'output_schema'):
                schema = spec.get(key)
                strict_subset(schema)
                if schema['type'] != 'object':
                    raise ValueError('Root schema must be object')
                Draft202012Validator.check_schema(schema)
                validators.append(Draft202012Validator(schema))
            pending[name] = Entry(domain, plugin, plugin.handle, spec, *validators)
        self._entries.update(pending)
        self._domains.add(domain)


class AccessPolicy:
    """Server-configured membership/scopes/domain allowlist, rechecked on every call.

    Replace with a database-backed policy in the persistence stage. Authenticated
    actor IDs come from the future host, never from tool arguments.
    """
    def __init__(self):
        self._members = {}
        self._enabled = {}

    def grant(self, organization_id, actor_id, scopes):
        if not all(isinstance(x, str) and x for x in (organization_id, actor_id)):
            raise ValueError('Identity required')
        if not isinstance(scopes, (list, set, frozenset, tuple)) or any(not isinstance(s, str) or not s for s in scopes):
            raise ValueError('Invalid scopes')
        self._members[organization_id, actor_id] = frozenset(scopes)

    def revoke(self, organization_id, actor_id):
        self._members.pop((organization_id, actor_id), None)

    def enable(self, organization_id, domains):
        if not isinstance(organization_id, str) or not organization_id or not isinstance(domains, (list, set, tuple, frozenset)) or any(not isinstance(d, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', d) for d in domains):
            raise ValueError('Invalid domains')
        self._enabled[organization_id] = frozenset(domains)

    def resolve(self, context):
        if not isinstance(context, dict) or any(not isinstance(context.get(k), str) or not context[k] for k in ('organization_id', 'actor_id')):
            return None, 'UNAUTHENTICATED'
        org, actor = context['organization_id'], context['actor_id']
        scopes = self._members.get((org, actor))
        if scopes is None:
            return None, 'FORBIDDEN'
        # Caller-supplied scopes/request_id are deliberately ignored.
        return {'organization_id': org, 'actor_id': actor, 'scopes': sorted(scopes),
                'request_id': str(uuid4()), 'idempotency_key': None}, None

    def permits(self, spec, context, domain):
        return domain in self._enabled.get(context['organization_id'], ()) and set(spec['required_scopes']) <= set(context['scopes'])


class Core:
    def __init__(self, registry, policy, *, timeout=10, input_limit=65536, output_limit=1048576, persistence=None):
        if timeout <= 0 or input_limit <= 0 or output_limit <= 0:
            raise ValueError('Positive limits required')
        self.registry, self.policy = registry, policy
        self.persistence = persistence
        self.timeout, self.input_limit, self.output_limit = timeout, input_limit, output_limit

    async def catalog_async(self, context, style='native'):
        if self.persistence is not None:
            return await self.persistence.catalog(self, context, style)
        return self.catalog(context, style)

    def catalog(self, context, style='native'):
        if self.persistence is not None:
            raise RuntimeError('Use await catalog_async() with database persistence')
        trusted, error = self.policy.resolve(context)
        if error:
            raise PermissionError(error)
        specs = [deepcopy(e.spec) for e in self.registry._entries.values() if self.policy.permits(e.spec, trusted, e.domain)]
        return self.export_specs(specs, style)

    @staticmethod
    def export_specs(specs, style):
        if style == 'native':
            return specs
        if style in ('responses', 'chat_completions'):
            result = []
            for s in specs:
                f = {'name': s['name'], 'description': s['description'], 'parameters': s['input_schema'], 'strict': True}
                result.append({'type': 'function', **f} if style == 'responses' else {'type': 'function', 'function': f})
            return result
        if style == 'mcp':
            return [{'name': s['name'], 'description': s['description'], 'inputSchema': s['input_schema'],
                     'outputSchema': s['output_schema'], 'annotations': {'readOnlyHint': s['read_only'],
                     'idempotentHint': s['idempotent'], 'destructiveHint': s['destructive'],
                     'openWorldHint': s['open_world']}} for s in specs]
        raise ValueError('Unknown catalog style')

    async def dispatch(self, plugin, name, arguments, context):
        """Compatible with generated adapter dispatch(plugin, name, args, context)."""
        if self.persistence is not None:
            return await self.persistence.dispatch(self, plugin, name, arguments, context)
        trusted, error = self.policy.resolve(context)
        if error:
            return failure(error, 'Authenticated membership required')
        entry = self.registry._entries.get(name) if isinstance(name, str) else None
        if entry is None or entry.plugin is not plugin:
            return failure('NOT_FOUND', 'Unknown tool')
        if not self.policy.permits(entry.spec, trusted, entry.domain):
            return failure('FORBIDDEN', 'Tool access denied')
        try:
            args = self.validate_arguments(entry, arguments, self.input_limit)
        except (ValueError, TypeError, RecursionError, OverflowError):
            return failure('INVALID_ARGUMENT', 'Arguments do not match the tool schema or limits')
        # No write handler can run without the future transactional persistence layer.
        if not entry.spec['read_only']:
            return failure('NOT_IMPLEMENTED', 'Durable write dispatch is not implemented')
        result = failure('INTERNAL_ERROR', 'Tool execution failed')
        try:
            raw = await asyncio.wait_for(entry.handler(name, args, deepcopy(trusted)), self.timeout)
            raw = json_copy(raw, self.output_limit)
            if not self._valid_result(raw, entry):
                raise ValueError('Invalid tool result')
            result = raw
        except asyncio.TimeoutError:
            result = failure('DEPENDENCY_UNAVAILABLE', 'Tool execution timed out')
        except Exception:
            # No arguments, outputs, exception strings, credentials or transcript text.
            LOG.warning('tool_failure request_id=%s tool=%s', trusted['request_id'], name)
        LOG.info('tool_completed request_id=%s tool=%s ok=%s', trusted['request_id'], name, result['ok'])
        return result

    @staticmethod
    def validate_arguments(entry, arguments, limit):
        args = json_copy(arguments, limit)
        if not entry.input_validator.is_valid(args):
            raise ValueError('Invalid arguments')
        return args

    @staticmethod
    def _valid_result(r, entry):
        if not isinstance(r, dict) or set(r) != {'ok', 'data', 'error', 'warnings'} or type(r['ok']) is not bool:
            return False
        if not isinstance(r['warnings'], list) or any(not isinstance(w, str) for w in r['warnings']):
            return False
        if r['ok']:
            return r['error'] is None and entry.output_validator.is_valid(r['data'])
        e = r['error']
        return (r['data'] is None and isinstance(e, dict) and set(e) == {'code', 'message', 'retryable'}
                and isinstance(e['code'], str) and e['code'] in CODES
                and isinstance(e['message'], str) and type(e['retryable']) is bool)
