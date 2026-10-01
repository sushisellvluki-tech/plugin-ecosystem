"""Offline demo: python -m core. Does not start a server or change the repository."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
from . import Core, Registry, AccessPolicy
from scripts.new_plugin import generate


async def main():
    with tempfile.TemporaryDirectory() as directory:
        path = generate('meetings', Path(directory)) / 'plugin.py'
        spec = importlib.util.spec_from_file_location('demo_meetings', path)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        registry, policy = Registry(), AccessPolicy()
        registry.register(plugin)
        policy.grant('demo-org', 'demo-actor', ['meetings:read'])
        policy.enable('demo-org', ['meetings'])
        result = await Core(registry, policy).dispatch(plugin, 'meetings__describe', {},
                {'organization_id': 'demo-org', 'actor_id': 'demo-actor'})
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
