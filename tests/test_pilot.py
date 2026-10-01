import json
import os
from pathlib import Path
import tempfile
import unittest
from deploy.prepare import prepare
from server.auth import TokenVerifier


class PilotBundleTests(unittest.TestCase):
    def test_private_distinct_roles_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=prepare(Path(tmp)/'private','fixture','Synthetic fixture',1)
            config=json.loads((root/'server.json').read_text())
            verifier=TokenVerifier(config['service_keys'])
            author=verifier.verify((root/'author.key').read_text().strip())
            reviewer=verifier.verify((root/'reviewer.key').read_text().strip())
            self.assertIn('meetings:publish',author.scopes)
            self.assertNotIn('meetings:review',author.scopes)
            self.assertIn('meetings:review',reviewer.scopes)
            self.assertNotIn('meetings:write',reviewer.scopes)
            self.assertNotEqual(author.actor_id,reviewer.actor_id)
            self.assertEqual(root.stat().st_mode&0o777,0o700)
            self.assertEqual((root/'author.key').stat().st_mode&0o777,0o600)
            self.assertNotIn((root/'author.key').read_text().strip(),(root/'server.json').read_text())
            before=(root/'server.json').read_bytes()
            with self.assertRaises(FileExistsError):prepare(root)
            self.assertEqual((root/'server.json').read_bytes(),before)

    def test_invalid_settings_leave_no_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'private'
            with self.assertRaises(ValueError):prepare(root,hours=0)
            self.assertFalse(root.exists())
