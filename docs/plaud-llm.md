# Plaud normalize + optional claim proposal

This branch adds **adapters only**. It does not replace independent human review
or the four-check publication barrier described in [meetings.md](meetings.md).

## 1. Plaud → `import_batch`

Native binary Plaud export is still not confirmed. Supported path:

1. Obtain an explicit text export (JSON with transcript fields).
2. Normalize with `plugins.meetings.sources.plaud.normalize_payload`.
3. Call existing `meetings__import_batch`.

CLI helper (no network):

```bash
python scripts/plaud_to_batch.py path/to/export.json --account my-plaud
# prints { "source_account_id", "sources": [ {filename, format, text, external_id} ] }
```

`external_id` is taken from `id` / `plaud_id` / `recording_id` when present so
idempotent re-imports land on the same source key.

## 2. `meetings__propose_claims`

Read-scoped tool. Loads accepted texts for a batch and returns candidate claims.

- With `LLM_API_KEY` or `XAI_API_KEY`: OpenAI-compatible chat (`LLM_BASE_URL`, `LLM_MODEL`).
- Without a key: deterministic heuristic on action-like lines.
- Every `quote` is verified as a substring of the source; bad model output is dropped.
- **Does not** write a run, review, or publish.

Typical flow:

1. `import_batch` (or Plaud normalize → import_batch)
2. `propose_claims` → inspect claims / warnings
3. Human edits, then `submit_draft` (optionally pass returned `model_version` / `prompt_version`)
4. Independent `review_run` with a different actor
5. `publish` only after four passes

## 3. What this does not do

- No audio/ZIP/PDF parser.
- No automatic `submit_draft` / `review_run` / `publish`.
- No change to evidence_errors (exact quotes still required).
- No continuous Plaud sync; batch export remains the owner-driven path.

## Env

```bash
# optional LLM
export LLM_API_KEY=...          # or XAI_API_KEY
export LLM_BASE_URL=https://api.x.ai/v1
export LLM_MODEL=grok-4-fast
```
