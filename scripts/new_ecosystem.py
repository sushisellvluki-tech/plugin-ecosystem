#!/usr/bin/env python3
"""Create a standalone development ecosystem in a NEW directory; never overwrite."""
import argparse
import keyword
from pathlib import Path
import re
import shutil
import sys

from new_plugin import generate


def create(domain, destination):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', domain) or keyword.iskeyword(domain):
        raise ValueError('Invalid Python domain; use lowercase letters, digits and underscores')
    # Keep final component unresolved: mkdir must reject even dangling symlinks.
    destination = Path(destination).expanduser().absolute()
    source = Path(__file__).resolve().parent.parent
    if destination.resolve() == source or source in destination.resolve().parents:
        raise ValueError('Destination must be outside the source repository')
    directories = ('core', 'server', 'scripts', 'assets', 'references', 'tests', 'docs', 'deploy')
    files = ('README.md', 'SKILL.md', 'requirements.txt', 'LICENSE', '.gitignore', '.dockerignore')
    for name in (*directories, *files):
        if not (source / name).exists():
            raise ValueError(f'Incomplete source bundle: {name}')
    # Require parent to exist; only the newly reserved directory is ours to clean up.
    destination.mkdir()
    try:
        for name in directories:
            shutil.copytree(source / name, destination / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.gitkeep'))
        for name in files:
            shutil.copy2(source / name, destination / name)
        for name in ('cabinet', 'bridges'):
            (destination / name).mkdir()
            (destination / name / '.gitkeep').touch()
        bundled = source / 'plugins' / 'meetings'
        if (bundled / 'repository.py').is_file():
            (destination / 'plugins').mkdir()
            (destination / 'plugins' / '__init__.py').write_text('"""Application plugins."""\n')
            shutil.copytree(bundled, destination / 'plugins' / 'meetings',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        if domain != 'meetings' or not (destination / 'plugins' / 'meetings').exists():
            generate(domain, destination)
        for script in (destination / 'scripts').glob('*.sh'):
            script.chmod(0o755)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('domain')
    parser.add_argument('destination', nargs='?', type=Path, default=Path.cwd() / 'ecosystem')
    args = parser.parse_args()
    try:
        path = create(args.domain, args.destination)
    except (OSError, ValueError) as exc:
        print(f'Creation failed: {exc}', file=sys.stderr)
        return 1
    print(f'Created development ecosystem: {path}')
    print('Local core and first diagnostic plugin are ready. No server or database was started.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
