#!/usr/bin/env python3
"""Validate a generated domain contract offline; does not certify a deployed server."""
import argparse
import asyncio
import importlib
import json
from pathlib import Path
import re
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strict_subset(schema, path="schema"):
    require(isinstance(schema, dict), f"{path}: must be an object")
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum", "description"}
    require(set(schema) <= allowed, f"{path}: unsupported schema keywords; extend the validator deliberately")
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    require(bool(types) and all(t in {"object", "array", "string", "boolean", "integer", "number", "null"} for t in types), f"{path}: invalid type")
    if "object" in types:
        props = schema.get("properties")
        require(isinstance(props, dict), f"{path}: properties required")
        required = schema.get("required")
        require(isinstance(required, list) and len(required) == len(props) and set(required) == set(props), f"{path}: every property must be required")
        require(schema.get("additionalProperties") is False, f"{path}: additionalProperties must be false")
        for name, child in props.items():
            strict_subset(child, f"{path}.{name}")
    if "array" in types:
        strict_subset(schema.get("items"), f"{path}[]")


class RegistrationHost:
    """In-memory core port probe, not a networking implementation."""
    def __init__(self):
        self.gets, self.posts, self.mcp = {}, {}, {}

    def add_json_get(self, path, handler):
        require(path not in self.gets, "duplicate GET route")
        self.gets[path] = handler

    def add_json_post(self, path, handler):
        require(path not in self.posts, "duplicate POST route")
        self.posts[path] = handler

    def add_mcp_endpoint(self, **registration):
        require(registration["path"] not in self.mcp, "duplicate MCP route")
        self.mcp[registration["path"]] = registration


async def validate(plugin_path):
    plugin_path = plugin_path.resolve()
    require(plugin_path.parent.name == "plugins", "Expected PROJECT/plugins/DOMAIN")
    manifest = json.loads((plugin_path / "manifest.json").read_text())
    domain = manifest["domain"]
    require(plugin_path.name == domain, "domain/path mismatch")
    sys.path.insert(0, str(plugin_path.parent.parent))
    module = importlib.import_module(f"plugins.{domain}.plugin")
    openai = importlib.import_module(f"plugins.{domain}.expose.openai")
    mcp = importlib.import_module(f"plugins.{domain}.expose.mcp")
    tools = module.tools()
    require(bool(tools), "empty tool registry")
    names = [tool["name"] for tool in tools]
    require(len(set(names)) == len(names), "duplicate tool name")
    for spec in tools:
        require(bool(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", spec["name"])) and spec["name"].startswith(domain + "__"), "invalid tool name")
        strict_subset(spec["input_schema"], spec["name"] + ".input")
        strict_subset(spec["output_schema"], spec["name"] + ".output")
        require(spec["input_schema"].get("type") == "object" and spec["output_schema"].get("type") == "object", "root schemas must be objects")
        require(bool(spec["required_scopes"]) and all(isinstance(s, str) and s.startswith(domain + ":") for s in spec["required_scopes"]), "invalid scopes")
        for flag in ("read_only", "idempotent", "destructive", "open_world"):
            require(type(spec[flag]) is bool, f"invalid flag: {flag}")
    context = {"organization_id": "fixture-org", "actor_id": "fixture-user",
               "scopes": sorted({s for t in tools for s in t["required_scopes"]}), "request_id": "offline-check", "idempotency_key": None}
    no_scopes = {**context, "scopes": []}
    response_defs = openai.definitions(context)
    chat_defs = openai.definitions(context, "chat_completions")
    mcp_defs = mcp.definitions(context)
    require([x["name"] for x in response_defs] == names, "Responses names diverge")
    require([x["function"]["name"] for x in chat_defs] == names, "Chat names diverge")
    require([x["name"] for x in mcp_defs] == names, "MCP names diverge")
    for spec, response, chat, wire in zip(tools, response_defs, chat_defs, mcp_defs):
        require(spec["input_schema"] == response["parameters"] == chat["function"]["parameters"] == wire["inputSchema"], "input schemas diverge")
        require(spec["output_schema"] == wire["outputSchema"], "output schema diverges")
    require(not openai.definitions(no_scopes) and not mcp.definitions(no_scopes), "scope filtering failed")
    for exporter in (openai.definitions, mcp.definitions):
        try:
            exporter({})
        except PermissionError:
            pass
        else:
            raise ValueError("missing tenant context did not fail closed")

    calls = []
    async def dispatch(module, name, args, trusted_context):
        calls.append(name)
        return await module.handle(name, args, trusted_context)

    host = RegistrationHost()
    openai.register(host, dispatch)
    mcp.register(host, dispatch)
    mcp_registration = host.mcp[f"/{domain}/mcp"]
    require(mcp_registration["legacy_sse_path"] is None, "legacy transport enabled by default")
    catalog = await host.gets[f"/{domain}/v1/tools"](context)
    require(catalog["responses"] == response_defs, "catalog does not use canonical registry")
    require(await mcp_registration["list_tools"](context) == mcp_defs, "MCP list diverges")
    legacy_host = RegistrationHost()
    mcp.register(legacy_host, dispatch, legacy_sse=True)
    require(legacy_host.mcp[f"/{domain}/mcp"]["legacy_sse_path"] == f"/{domain}/mcp/sse", "legacy registration path mismatch")

    # Call only the known generated diagnostic; never arbitrary user tools.
    diagnostic = domain + "__describe"
    require(diagnostic in names, "scaffold diagnostic missing")
    direct = await module.handle(diagnostic, {}, context)
    http = await host.posts[f"/{domain}/v1/tools/invoke"]({"name": diagnostic, "arguments": {}}, context)
    wire = await mcp_registration["call_tool"](diagnostic, {}, context)
    require(direct["ok"] and direct == http and wire["structuredContent"] == direct["data"], "adapters changed the result")
    require(direct["data"]["business_ready"] is False, "scaffold falsely claims business readiness")
    require(calls == [diagnostic, diagnostic], "adapters bypassed the dispatcher")
    for name, args, ctx, expected in ((diagnostic, {}, no_scopes, "FORBIDDEN"), (diagnostic, {}, {}, "UNAUTHENTICATED"),
                                      (diagnostic, {"organization_id": "other"}, context, "INVALID_ARGUMENT"), ("unknown", {}, context, "NOT_FOUND")):
        result = await module.handle(name, args, ctx)
        require(not result["ok"] and result["error"]["code"] == expected, f"expected {expected}")
    return {"domain": domain, "status": "passed", "tools": names,
            "verified": ["contract", "schema_exports", "registration_ports", "shared_dispatch", "diagnostic_errors"],
            "not_verified": ["network_transport", "oauth", "database", "tenant_data_isolation", "external_clients", "business_pipeline"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin_path", type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(validate(args.plugin_path))
    except Exception as exc:
        print(f"Contract check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
