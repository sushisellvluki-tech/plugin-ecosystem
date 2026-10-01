#!/usr/bin/env python3
"""Bounded Streamable HTTP smoke check; no legacy SSE, OAuth flow or write calls."""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CheckFailed(Exception):
    pass


def read_reply(response, request_id):
    content_type = response.headers.get_content_type()
    limit = 2 * 1024 * 1024
    if content_type == "application/json":
        data = response.read(limit + 1)
        if len(data) > limit:
            raise CheckFailed("Response too large")
        reply = json.loads(data)
        if reply.get("jsonrpc") != "2.0" or reply.get("id") != request_id:
            raise CheckFailed("Not a matching JSON-RPC response")
        return reply
    if content_type != "text/event-stream":
        raise CheckFailed("Expected JSON or SSE, not HTML/health content")
    started, total, lines = time.monotonic(), 0, []
    while time.monotonic() - started < 30:
        raw = response.readline(limit + 1)
        if not raw:
            break
        total += len(raw)
        if total > limit:
            raise CheckFailed("SSE response too large")
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
        elif line == "" and lines:
            payload = "\n".join(lines)
            lines = []
            if not payload:
                continue
            reply = json.loads(payload)
            if reply.get("jsonrpc") == "2.0" and reply.get("id") == request_id:
                return reply
    raise CheckFailed("SSE ended/timed out without a matching response")


class Probe:
    def __init__(self, url, token, protocol):
        parsed = urllib.parse.urlsplit(url)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CheckFailed("URL must not contain credentials, query parameters or a fragment")
        if not parsed.hostname or not (parsed.scheme == "https" or (parsed.scheme == "http" and local)):
            raise CheckFailed("Use HTTPS, or HTTP on loopback only")
        if parsed.path.endswith("/sse"):
            raise CheckFailed("Legacy HTTP+SSE requires the official SDK/Inspector")
        self.url, self.token, self.protocol = url, token, protocol
        self.session = None
        self.next_id = 1
        self.opener = urllib.request.build_opener(NoRedirect())

    def rpc(self, method, params=None, *, anonymous=False, notification=False):
        params = dict(params or {})
        request_id = None if notification else self.next_id
        self.next_id += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = request_id
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if not anonymous:
            headers["Authorization"] = "Bearer " + self.token
        if self.protocol == "2026-07-28":
            params["_meta"] = {"io.modelcontextprotocol/protocolVersion": self.protocol,
                               "io.modelcontextprotocol/clientInfo": {"name": "ecosystem-smoke", "version": "0.1.0"},
                               "io.modelcontextprotocol/clientCapabilities": {}}
            headers["MCP-Protocol-Version"] = self.protocol
            headers["Mcp-Method"] = method
            if method == "tools/call":
                headers["Mcp-Name"] = params["name"]
        elif method != "initialize":
            headers["MCP-Protocol-Version"] = self.protocol
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=15) as response:
                if anonymous:
                    raise CheckFailed("Anonymous request was accepted; protected endpoint expected")
                if method == "initialize":
                    self.session = response.headers.get("Mcp-Session-Id")
                if notification:
                    if response.status != 202:
                        raise CheckFailed("Accepted notification must return 202")
                    return None
                reply = read_reply(response, request_id)
        except urllib.error.HTTPError as exc:
            if anonymous and exc.code in {401, 403}:
                return None
            raise CheckFailed(f"HTTP {exc.code}; server response omitted to protect private data") from None
        if "error" in reply:
            raise CheckFailed(f"JSON-RPC error {reply['error'].get('code')}; details omitted")
        if not isinstance(reply.get("result"), dict):
            raise CheckFailed("Missing JSON-RPC result object")
        return reply["result"]

    def begin(self):
        if self.protocol == "2026-07-28":
            self.rpc("tools/list", anonymous=True)
        else:
            params = {"protocolVersion": self.protocol, "capabilities": {}, "clientInfo": {"name": "ecosystem-smoke", "version": "0.1.0"}}
            self.rpc("initialize", params, anonymous=True)
            result = self.rpc("initialize", params)
            if result.get("protocolVersion") != self.protocol:
                raise CheckFailed("Negotiated a different protocol; rerun explicitly with the supported profile")
            self.rpc("notifications/initialized", notification=True)

    def close(self):
        if not self.session:
            return
        request = urllib.request.Request(self.url, method="DELETE", headers={
            "Authorization": "Bearer " + self.token, "Mcp-Session-Id": self.session,
            "MCP-Protocol-Version": self.protocol,
        })
        try:
            with self.opener.open(request, timeout=5):
                pass
        except Exception:
            pass  # best-effort disposal; not a claim of cleanup support


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--protocol", required=True, choices=["2025-11-25", "2026-07-28"])
    parser.add_argument("--tool", help="Explicit known read-only tool; never inferred from user data")
    parser.add_argument("--arguments", default="{}")
    args = parser.parse_args()
    token = os.environ.get("MCP_ACCESS_TOKEN")
    if not token:
        print("MCP_ACCESS_TOKEN is required; do not put it in the URL or command arguments", file=sys.stderr)
        return 1
    probe = None
    try:
        arguments = json.loads(args.arguments)
        if not isinstance(arguments, dict):
            raise CheckFailed("Arguments must be a JSON object")
        if args.tool and not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.tool):
            raise CheckFailed("Unsupported tool name")
        probe = Probe(args.url, token, args.protocol)
        probe.begin()
        tools, cursor = [], None
        seen_cursors = set()
        for _ in range(20):
            result = probe.rpc("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list) or not all(isinstance(t, dict) and isinstance(t.get("name"), str) and isinstance(t.get("inputSchema"), dict) for t in page):
                raise CheckFailed("Malformed tool catalog")
            tools.extend(page)
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise CheckFailed("Invalid/repeating pagination cursor")
            seen_cursors.add(cursor)
        else:
            raise CheckFailed("Catalog exceeds smoke-check page limit")
        if not tools:
            raise CheckFailed("Empty tool catalog; check scopes and plugin activation")
        if args.tool:
            tool = next((t for t in tools if t.get("name") == args.tool), None)
            if not tool or tool.get("annotations", {}).get("readOnlyHint") is not True:
                raise CheckFailed("Selected tool is absent or not declared read-only")
            result = probe.rpc("tools/call", {"name": args.tool, "arguments": arguments})
            if result.get("isError") or not isinstance(result.get("content"), list):
                raise CheckFailed("Tool call failed or returned a malformed result")
        print(json.dumps({"status": "passed", "protocol": args.protocol, "tool_count": len(tools),
                          "read_only_call_checked": bool(args.tool), "oauth_flow_checked": False,
                          "external_client_compatibility_checked": False}))
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, CheckFailed) else type(exc).__name__
        print("MCP smoke check failed: " + message, file=sys.stderr)
        return 1
    finally:
        if probe:
            probe.close()


if __name__ == "__main__":
    raise SystemExit(main())
