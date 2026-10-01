import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/new-ecosystem.sh'


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


class EcosystemTests(unittest.TestCase):
    def run_script(self, cwd, *args):
        return subprocess.run(['bash', str(SCRIPT), *map(str, args)], cwd=cwd,
                              capture_output=True, text=True)

    def test_fresh_project_and_offline_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / 'project with spaces'
            result = self.run_script(tmp, 'meetings', dst)
            self.assertEqual(result.returncode, 0, result.stderr)
            for p in ('core/runtime.py', 'plugins/meetings/plugin.py', 'LICENSE',
                      'references/mcp-expose.md', 'scripts/new_ecosystem.py', 'docs/import-review.md'):
                self.assertTrue((dst / p).is_file(), p)
            self.assertTrue(os.access(dst / 'scripts/new-ecosystem.sh', os.X_OK))
            for command in (['bash', 'scripts/check-tools.sh', str(dst/'plugins/meetings')],
                            ['python3', '-m', 'core']):
                r = subprocess.run(command, cwd=dst, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
            # Executable generators remain usable after copying the whole project.
            r = subprocess.run(['bash', str(dst/'scripts/new-ecosystem.sh'), 'tasks', str(Path(tmp)/'second')],
                               cwd=tmp, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_default_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_script(tmp, 'meetings').returncode, 0)
            self.assertTrue((Path(tmp)/'ecosystem/plugins/meetings/plugin.py').exists())

    def test_existing_project_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp)/'existing'; dst.mkdir()
            (dst/'important.txt').write_text('keep')
            before = snapshot(dst)
            self.assertNotEqual(self.run_script(tmp, 'meetings', dst).returncode, 0)
            self.assertEqual(snapshot(dst), before)

    def test_invalid_names_leave_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('bad-name', '../escape', 'class', '123', ''):
                dst = Path(tmp)/'new'
                self.assertNotEqual(self.run_script(tmp, name, dst).returncode, 0)
                self.assertFalse(dst.exists())

    def test_dangling_symlink_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp)/'link'; target = Path(tmp)/'missing'
            dst.symlink_to(target)
            self.assertNotEqual(self.run_script(tmp, 'meetings', dst).returncode, 0)
            self.assertTrue(dst.is_symlink())
            self.assertFalse(target.exists())

    def test_missing_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp)/'missing/child'
            self.assertNotEqual(self.run_script(tmp, 'meetings', dst).returncode, 0)
            self.assertFalse(dst.parent.exists())


if __name__ == '__main__':
    unittest.main()
