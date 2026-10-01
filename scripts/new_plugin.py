#!/usr/bin/env python3
"""Create one domain without overwriting existing files; Python stdlib only."""
import argparse
import json
import keyword
from pathlib import Path
import re
import shutil
import sys


def generate(domain: str, root: Path) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", domain) or keyword.iskeyword(domain):
        raise ValueError("domain: 1–32 lowercase ASCII letters/digits/underscores; start with a letter; no Python keywords")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    plugins = root / "plugins"
    if plugins.is_symlink():
        raise ValueError("Refusing a symlinked plugins directory")
    plugins.mkdir(exist_ok=True)
    init = plugins / "__init__.py"
    if not init.exists():
        try:
            with init.open("x", encoding="utf-8") as f:
                f.write('"""Application plugin packages."""\n')
        except FileExistsError:
            pass
    if not init.is_file() or init.is_symlink():
        raise ValueError("plugins/__init__.py must be a regular file")
    destination = plugins / domain
    destination.mkdir()  # atomic no-overwrite guard, including dangling symlinks
    templates = Path(__file__).resolve().parent.parent / "assets" / "plugin-template"
    try:
        (destination / "expose").mkdir()
        (destination / "__init__.py").write_text("", encoding="utf-8")
        (destination / "expose" / "__init__.py").write_text("", encoding="utf-8")
        for src, dest in (("plugin.py.tmpl", "plugin.py"), ("openai.py.tmpl", "expose/openai.py"), ("mcp.py.tmpl", "expose/mcp.py")):
            text = (templates / src).read_text(encoding="utf-8").replace("__DOMAIN__", domain)
            compile(text, dest, "exec")
            (destination / dest).write_text(text, encoding="utf-8")
        metadata = {
            "domain": domain, "version": "0.1.0", "contract_version": "0.1",
            "status": "scaffold", "business_ready": False, "runtime_included": False,
            "entrypoint": f"plugins.{domain}.plugin",
            "adapters": {"openai": f"plugins.{domain}.expose.openai:register", "mcp": f"plugins.{domain}.expose.mcp:register"},
            "database": {"planned_engine": "postgresql", "planned_schema": domain, "migrations_implemented": False},
        }
        endpoints = {
            "status": "registration_plan_only", "runtime_required": True,
            "function_catalog": f"/{domain}/v1/tools", "function_invoke": f"/{domain}/v1/tools/invoke",
            "mcp": {"path": f"/{domain}/mcp", "transport": "streamable_http", "implemented_protocol_versions": []},
            "legacy_sse": {"path": f"/{domain}/mcp/sse", "enabled": False, "requires_sdk_post_backchannel": True},
            "authentication": "required_in_host_not_implemented_here",
        }
        for name, body in (("manifest.json", metadata), ("endpoint-plan.json", endpoints)):
            (destination / name).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except BaseException:
        shutil.rmtree(destination)  # only the directory exclusively created by this invocation
        raise
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain")
    parser.add_argument("project_root", type=Path)
    args = parser.parse_args()
    try:
        destination = generate(args.domain, args.project_root)
    except (OSError, ValueError) as exc:
        print(f"Generation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"created": str(destination), "status": "scaffold", "server_running": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
