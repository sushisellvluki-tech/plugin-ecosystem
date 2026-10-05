"""Workforce: deterministic time-row preview through the existing core."""
from copy import deepcopy
from typing import Any
from .schemas import INPUT, OUTPUT
from .preview import preview

DOMAIN = "workforce"
VERSION = "0.1.0"
CONTRACT_VERSION = "0.1"

_TOOLS = [{
    "name": DOMAIN + "__describe",
    "description": "Describe read-only time-row validation and its unimplemented posting boundary.",
    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    "output_schema": {
        "type": "object",
        "properties": {
            "domain": {"type": "string"},
            "version": {"type": "string"},
            "business_ready": {"type": "boolean"},
            "implementation": {"type": "string", "enum": ["preview_only"]},
        },
        "required": ["domain", "version", "business_ready", "implementation"],
        "additionalProperties": False,
    },
    "required_scopes": [DOMAIN + ":read"],
    "read_only": True,
    "idempotent": True,
    "destructive": False,
    "open_world": False,
}, {
    'name': DOMAIN + '__preview_timesheet',
    'description': 'Validate normalized time rows before import; returns row errors, never writes or calculates payroll. IDs are not checked against a database.',
    'input_schema': INPUT, 'output_schema': OUTPUT,
    'required_scopes': [DOMAIN + ':preview'],
    'read_only': True, 'idempotent': True, 'destructive': False, 'open_world': False,
}]


def tools() -> list[dict[str, Any]]:
    return deepcopy(_TOOLS)


def visible_tools(context: dict) -> list[dict]:
    """Context must be supplied by the authenticated core, never by tool arguments."""
    if not context.get("organization_id") or not context.get("actor_id"):
        raise PermissionError("Authenticated organization context is required")
    granted = set(context.get("scopes", []))
    return [tool for tool in tools() if set(tool["required_scopes"]) <= granted]


def failure(code: str, message: str) -> dict:
    return {"ok": False, "data": None, "error": {"code": code, "message": message, "retryable": False}, "warnings": []}


async def handle(name: str, arguments: dict, context: dict) -> dict:
    """Called through the shared dispatcher; this redundant check protects the example."""
    if not context.get("organization_id") or not context.get("actor_id"):
        return failure("UNAUTHENTICATED", "Authenticated organization context is required")
    if name not in {tool["name"] for tool in tools()}:
        return failure("NOT_FOUND", "Unknown tool")
    if name not in {tool["name"] for tool in visible_tools(context)}:
        return failure("FORBIDDEN", "Missing required scope")
    if name == DOMAIN + '__preview_timesheet':
        try:
            data = preview(arguments)
        except ValueError as exc:
            return failure('INVALID_ARGUMENT', str(exc))
        return {'ok': True, 'data': data, 'error': None,
                'warnings': ['Preview only: no database lookup, durable import, approval or payroll calculation.']}
    if not isinstance(arguments, dict) or arguments:
        return failure("INVALID_ARGUMENT", "This diagnostic tool takes an empty object")
    return {
        "ok": True,
        "data": {"domain": DOMAIN, "version": VERSION, "business_ready": False, "implementation": "preview_only"},
        "error": None,
        "warnings": ["Normalized time-row preview is implemented. XLSX import, reference lookup, persistence and payroll are not implemented."],
    }
