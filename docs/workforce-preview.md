# Workforce: preview before time import

Status: implemented read-only validator of normalized JSON rows. It runs through
the existing registry, dispatcher, authorization and transport adapters. It does
not read XLSX, resolve people, save rows, approve work or calculate pay.

## Purpose

A spreadsheet can contain missing site assignments, future dates marked as actual,
duplicate source rows and time counted at multiple sites. These must be visible
before the first operational import. A successful tool execution means that the
preview ran; `ok=true` is not approval of its input.

Tools: `workforce__describe` (`workforce:read`) and
`workforce__preview_timesheet` (`workforce:preview`). The plugin is opt-in through
the host's configured plugin list and tenant domain permissions. Default pilot
configuration continues to load its existing plugins.

## Input

```json
{
  "as_of_date": "2026-10-05",
  "expected_month": "2026-09",
  "records": [{
    "source_key": "synthetic-source:sheet:2",
    "person_id": "person-1",
    "project_id": "site-1",
    "engagement_id": "engagement-1",
    "work_date": "2026-09-30",
    "minutes": 600,
    "record_type": "actual"
  }]
}
```

All fields are required; missing references/date/minutes are explicit nulls.
`expected_month=null` supports a batch spanning months. `as_of_date` is an explicit
business-local reference date supplied for the preview; it is not server approval.
Use 1–500 rows, fields up to 200 characters, and the existing core request size
limit (64 KiB by default). IDs use at most 100 ASCII letters/digits/`_.:-` and start
with a letter or digit. Names and passports are not identifiers for this tool.

`source_key` identifies an immutable source row/version within a tenant. Its format
is adapter-owned. This preview detects duplicates only within the submitted batch.
A source hash alone does not identify semantically duplicated files or rows.

| Check | Behavior |
|---|---|
| Missing person, engagement or site | Error; site may be null for whole-day absence |
| Invalid date or selected month mismatch | Error; no date correction |
| Future actual work or actual absence | Error; future plan is permitted |
| Minutes missing, fractional, negative or >1440 | Error |
| Zero actual/plan minutes or absence with minutes | Error |
| Unknown record type | Error |
| Plan | Warning; must remain separate from actual costs |
| Same source key | All matching rows flagged as errors |
| Actual time per person/day >1440 across sites/engagements | All participating rows flagged |
| Actual work and whole-day absence on one day | Error on both sides |
| Similar person/site/engagement/date/time/type | Warning, never automatic deletion |

Only 1-based input indices, fixed codes and messages are returned; no source values
are echoed. No input is mutated and no state survives the call. Always
`references_verified=false` and `ready_to_post=false`, including clean batches.
`rows_without_detected_errors` is not a count of approved rows. Check warnings too.

## Checks

```bash
python -m unittest discover -s tests -p 'test_workforce.py' -v
bash scripts/check-tools.sh "$PWD/plugins/workforce"
```

Synthetic tests include multi-site days, duplicates, absence/plan separation,
invalid dates/minutes, membership/scopes and revocation, and a real loopback HTTP
and official MCP-client comparison. Database reference authorization, RLS, durable
idempotency and concurrent posting are not established by this stateless preview.

The existing host mounts `/workforce/v1/tools`, `/workforce/v1/tools/invoke` and
`/workforce/mcp` only when the plugin is configured and authorized. Generated
endpoint metadata describes registration; it does not publish an external URL.

## Next increment

1. Confirm one source workbook's reporting period and authoritative sheet.
2. Implement an explicit XLSX adapter with source/version/cell provenance and a
   reviewed name-to-person mapping; do not concatenate historical/duplicate tabs.
3. Resolve person, engagement and site references against authorized tenant data.
4. Save a draft import with atomic idempotency and a review queue.
5. Add approval only after dated assignments, history and all required references
   are checked again in the posting transaction.

Clock intervals, overlapping work below 24 total hours, source authenticity,
employment status, rate units, taxes, payroll rules and payments remain outside
this preview. Minutes are an input contract; a future adapter must document its
rounding of decimal spreadsheet hours and preserve the raw source value.
