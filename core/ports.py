"""Bind canonical core catalogs to host ports; no HTTP or MCP transport here."""
import json
from .runtime import failure


def bind(host, core, plugin, *, legacy_sse=False):
    domain = plugin.DOMAIN
    # Require the exact registered module, not a replacement with the same name.
    if not any(e.plugin is plugin for e in core.registry._entries.values()):
        raise ValueError('Register the plugin first')

    async def catalog(context, style):
        rows = await core.catalog_async(context, style)
        names = {name for name, entry in core.registry._entries.items() if entry.domain == domain}
        return [r for r in rows if (r['function']['name'] if style == 'chat_completions' else r['name']) in names]

    async def functions(context):
        return {'contract_version': '0.1', 'domain': domain,
                'responses': await catalog(context, 'responses'), 'chat_completions': await catalog(context, 'chat_completions')}

    async def invoke(body, context):
        if not isinstance(body, dict) or set(body) != {'name', 'arguments'}:
            return failure('INVALID_ARGUMENT', 'Expected name and arguments only')
        return await core.dispatch(plugin, body['name'], body['arguments'], context)

    async def list_tools(context):
        return await catalog(context, 'mcp')

    async def call_tool(name, arguments, context):
        result = await core.dispatch(plugin, name, arguments, context)
        if not result['ok']:
            return {'isError': True, 'content': [{'type': 'text', 'text': json.dumps(result['error'])}]}
        content = [{'type': 'text', 'text': json.dumps(result['data'], ensure_ascii=False)}]
        if result['warnings']:
            content.append({'type': 'text', 'text': 'Warnings: ' + '; '.join(result['warnings'])})
        return {'isError': False, 'structuredContent': result['data'], 'content': content}

    host.add_json_get(f'/{domain}/v1/tools', functions)
    host.add_json_post(f'/{domain}/v1/tools/invoke', invoke)
    host.add_mcp_endpoint(path=f'/{domain}/mcp', list_tools=list_tools,
                          call_tool=call_tool, legacy_sse_path=f'/{domain}/mcp/sse' if legacy_sse else None)
