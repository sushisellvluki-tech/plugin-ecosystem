"""Function-call schema export and registration port; not an OpenAI model server."""
from copy import deepcopy
from .. import plugin


def definitions(context: dict, style: str = "responses") -> list[dict]:
    if style not in {"responses", "chat_completions"}:
        raise ValueError("Unsupported function-calling style")
    result = []
    for spec in plugin.visible_tools(context):
        function = {
            "name": spec["name"], "description": spec["description"],
            "parameters": deepcopy(spec["input_schema"]), "strict": True,
        }
        result.append({"type": "function", **function} if style == "responses" else {"type": "function", "function": function})
    return result


def register(host, dispatch) -> None:
    """host authenticates requests; dispatch validates, authorizes, runs, and audits."""
    async def catalog(context):
        return {"contract_version": plugin.CONTRACT_VERSION, "domain": plugin.DOMAIN,
                "responses": definitions(context), "chat_completions": definitions(context, "chat_completions")}

    async def invoke(body, context):
        if (not isinstance(body, dict) or set(body) != {"name", "arguments"}
                or not isinstance(body["name"], str) or not isinstance(body["arguments"], dict)):
            return plugin.failure("INVALID_ARGUMENT", "Expected name and arguments only")
        return await dispatch(plugin, body["name"], body["arguments"], context)

    host.add_json_get(f"/{plugin.DOMAIN}/v1/tools", catalog)
    host.add_json_post(f"/{plugin.DOMAIN}/v1/tools/invoke", invoke)
