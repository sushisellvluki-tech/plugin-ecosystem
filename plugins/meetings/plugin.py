"""Meetings: bounded text intake, immutable drafts and independent review."""
from copy import deepcopy
from typing import Any
from .schemas import SPECS

DOMAIN = "meetings"
VERSION = "0.1.0"
CONTRACT_VERSION = "0.1"

_TOOLS = [{
    "name": DOMAIN + "__describe",
    "description": "Describe the bounded text ingestion and independent manual review implementation.",
    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    "output_schema": {
        "type": "object",
        "properties": {
            "domain": {"type": "string"},
            "version": {"type": "string"},
            "business_ready": {"type": "boolean"},
            "implementation": {"type": "string", "enum": ["manual_review"]},
        },
        "required": ["domain", "version", "business_ready", "implementation"],
        "additionalProperties": False,
    },
    "required_scopes": [DOMAIN + ":read"],
    "read_only": True,
    "idempotent": True,
    "destructive": False,
    "open_world": False,
}]


def tools() -> list[dict[str, Any]]:
    result = deepcopy(_TOOLS)
    for action, description, inputs, outputs, scope, read_only in SPECS:
        result.append({'name': DOMAIN+'__'+action, 'description': description,
            'input_schema': deepcopy(inputs), 'output_schema': deepcopy(outputs),
            'required_scopes': [DOMAIN+':'+scope], 'read_only': read_only,
            'idempotent': True, 'destructive': False, 'open_world': False})
    return result


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
    if name != DOMAIN+'__describe':
        repository = context.get('repository')
        if repository is None:
            return failure('NOT_IMPLEMENTED', 'Meetings requires PostgreSQL persistence')
        from .repository import Meetings
        from core.db.store import Rejected
        try:
            result = await Meetings(repository).execute(name.split('__', 1)[1], arguments)
            return {'ok': True, 'data': result, 'error': None, 'warnings': []}
        except Rejected as exc:
            return exc.result
        except ValueError as exc:
            return failure('INVALID_ARGUMENT', str(exc))
    if not isinstance(arguments, dict) or arguments:
        return failure("INVALID_ARGUMENT", "This diagnostic tool takes an empty object")
    return {
        "ok": True,
        "data": {"domain": DOMAIN, "version": VERSION, "business_ready": False, "implementation": "manual_review"},
        "error": None,
        "warnings": ["Text ingestion and independent manual review are supported. Automatic AI extraction and a native Plaud parser are not configured."],
    }
