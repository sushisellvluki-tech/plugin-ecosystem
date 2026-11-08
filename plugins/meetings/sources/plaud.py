"""
Normalize Plaud-like export payloads into meetings import_batch sources.

Boundaries (aligned with docs/meetings.md and references/plaud-integration.md):
- No binary/audio parser; only explicit text fields.
- external_id comes from a stable recording id when present.
- Does not call the network, CLI, or database.
- Output is ready for meetings__import_batch.

Supported input shapes (object or list of objects):
  { "id"|"plaud_id"|"recording_id": "...", "transcript"|"text"|"content": "...",
    "title"|"name": "...", "date"|"created_at": "..." }
"""

from __future__ import annotations

from typing import Any


def _pick(record: dict, *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def normalize_record(record: dict, *, default_filename: str = "plaud.txt") -> dict:
    """Turn one Plaud-like record into a single import_batch source entry."""
    if not isinstance(record, dict):
        raise ValueError("Each Plaud record must be an object")

    text = _pick(record, "transcript", "text", "content", "raw_transcript")
    if text is None:
        raise ValueError("Missing transcript/text/content field")
    if not isinstance(text, str):
        raise ValueError("Transcript must be a string")
    text = text  # preserve as-is; import_batch rejects NUL

    external = _pick(record, "id", "plaud_id", "recording_id", "uuid")
    if external is not None:
        external = str(external).strip() or None

    title = _pick(record, "title", "name")
    if isinstance(title, str) and title.strip():
        # Safe ASCII-ish filename fragment; import_batch only checks length/non-empty
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in title.strip())[:80]
        filename = f"{safe or 'plaud'}.txt"
    elif external:
        filename = f"plaud_{external}.txt"
    else:
        filename = default_filename

    return {
        "filename": filename,
        "format": "text/plain",
        "text": text,
        "external_id": external,
    }


def normalize_payload(payload: Any, *, source_account_id: str = "plaud-export") -> dict:
    """
    Normalize a full export (object, list, or {recordings:[...]}) into
    import_batch arguments: {source_account_id, sources}.
    """
    if not source_account_id or not str(source_account_id).strip():
        raise ValueError("source_account_id is required")
    source_account_id = str(source_account_id).strip()[:128]

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        nested = payload.get("recordings") or payload.get("items") or payload.get("data")
        if isinstance(nested, list):
            records = nested
        else:
            records = [payload]
    else:
        raise ValueError("Expected JSON object or array")

    if not records:
        raise ValueError("Empty Plaud payload")

    sources = [normalize_record(rec) for rec in records]
    return {"source_account_id": source_account_id, "sources": sources}
