"""MCP schema/result adaptation. The core must implement transport with an official SDK."""
import json
from copy import deepcopy
from .. import plugin


def definitions(context: dict) -> list[dict]:
    return [{
        "name": spec["name"], "description": spec["description"],
        "inputSchema": deepcopy(spec["input_schema"]), "outputSchema": deepcopy(spec["output_schema"]),
        "annotations": {"readOnlyHint": spec["read_only"], "idempotentHint": spec["idempotent"],
                        "destructiveHint": spec["destructive"], "openWorldHint": spec["open_world"]},
    } for spec in plugin.visible_tools(context)]


def encode_result(result: dict) -> dict:
    if not result["ok"]:
        return {"isError": True, "content": [{"type": "text", "text": json.dumps(result["error"], ensure_ascii=False)}]}
    content = [{"type": "text", "text": json.dumps(result["data"], ensure_ascii=False)}]
    if result.get("warnings"):
        content.append({"type": "text", "text": "Warnings: " + "; ".join(result["warnings"])})
    return {"isError": False, "structuredContent": result["data"], "content": content}


def register(host, dispatch, *, legacy_sse: bool = False) -> None:
    """Both transports must receive the same verified organization context."""
    async def list_tools(context):
        return definitions(context)

    async def call_tool(name, arguments, context):
        return encode_result(await dispatch(plugin, name, arguments, context))

    host.add_mcp_endpoint(
        path=f"/{plugin.DOMAIN}/mcp", list_tools=list_tools, call_tool=call_tool,
        legacy_sse_path=f"/{plugin.DOMAIN}/mcp/sse" if legacy_sse else None,
    )
