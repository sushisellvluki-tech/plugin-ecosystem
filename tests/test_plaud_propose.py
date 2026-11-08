"""Unit tests for Plaud normalize and claim proposer (no Postgres required)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from plugins.meetings.sources.plaud import normalize_payload, normalize_record
from plugins.meetings.ai_propose import propose_claims
from plugins.meetings.checks import evidence_errors


class PlaudNormalizeTests(unittest.TestCase):
    def test_single_record(self):
        src = normalize_record({
            "plaud_id": "rec_1",
            "title": "Синк Q3",
            "transcript": "Нужно сдать отчёт.",
        })
        self.assertEqual(src["format"], "text/plain")
        self.assertEqual(src["external_id"], "rec_1")
        self.assertEqual(src["text"], "Нужно сдать отчёт.")
        self.assertTrue(src["filename"].endswith(".txt"))

    def test_payload_list_and_account(self):
        batch = normalize_payload(
            [{"id": "a", "text": "one"}, {"recording_id": "b", "content": "two"}],
            source_account_id="acct",
        )
        self.assertEqual(batch["source_account_id"], "acct")
        self.assertEqual(len(batch["sources"]), 2)
        self.assertEqual(batch["sources"][0]["external_id"], "a")
        self.assertEqual(batch["sources"][1]["external_id"], "b")

    def test_missing_text_raises(self):
        with self.assertRaises(ValueError):
            normalize_record({"id": "x"})


class ProposeClaimsTests(unittest.TestCase):
    def test_heuristic_quotes_match_source(self):
        text = (
            "Игорь: нужно подготовить отчёт.\n"
            "Аня: сделаю проверку к пятнице.\n"
            "Обсудили бюджет."
        )
        entries = [{"index": 0, "text": text, "status": "accepted"}]
        result = propose_claims(entries, use_llm=False)
        self.assertEqual(result["extractor"], "heuristic")
        self.assertGreaterEqual(len(result["claims"]), 1)
        errors = evidence_errors(result["claims"], entries)
        self.assertEqual(errors, [], errors)

    def test_empty_entries(self):
        result = propose_claims([], use_llm=False)
        self.assertEqual(result["claims"], [])
        self.assertTrue(result["warnings"])


class SchemaToolPresent(unittest.TestCase):
    def test_propose_in_specs(self):
        from plugins.meetings.schemas import SPECS
        names = [s[0] for s in SPECS]
        self.assertIn("propose_claims", names)
        self.assertIn("import_batch", names)

    def test_tools_include_propose(self):
        from plugins.meetings.plugin import tools
        names = {t["name"] for t in tools()}
        self.assertIn("meetings__propose_claims", names)


if __name__ == "__main__":
    unittest.main()
