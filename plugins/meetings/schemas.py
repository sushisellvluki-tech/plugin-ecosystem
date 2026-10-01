"""One strict schema source for core, HTTP and MCP."""
S = {'type': 'string'}
I = {'type': 'integer'}
B = {'type': 'boolean'}
N = {'type': ['string', 'null']}


def obj(**properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def array(item):
    return {'type': 'array', 'items': item}


SOURCE = obj(filename=S, format=S, text=S, external_id=N)
CLAIM = obj(source_index=I, start=I, quote=S, text=S, subject=S, predicate=S, value=S)
CHECK = obj(check_no=I, verdict={'type': 'string', 'enum': ['pass', 'fail', 'needs_review']},
            reason=S, method=S, reviewer_id=N)
ENTRY = obj(index=I, filename=S, format=S, text=S, external_id=N, status=S, reason=S,
            source_version_id=N, source_hash=N)
BATCH = obj(batch_id=S, status=S, entries=array(ENTRY))
RUN = obj(run_id=S, batch_id=S, status=S, artifact_hash=S, source_hash=S, rule_version=S,
          claims=array(CLAIM), checks=array(CHECK), stale=B, created_by=S, model_version=N, prompt_version=N)
HISTORY = obj(records=array(obj(run_id=S, artifact_hash=S, claims=array(CLAIM), stale=B)), truncated=B)
PAGE = obj(records=array(obj(batch_id=S,created_at=S,source_account_id=S,ingestion_status=S,
                              run_id=N,run_status=N,stale=B,files=I)),next_cursor=N)
SPECS = [
    ('list_batches', 'List this organization batches with stable cursor pagination; no raw transcripts in the projection.',
     obj(cursor=N,limit=I), PAGE, 'read', True),
    ('import_batch', 'Import a bounded batch of explicit text/plain transcripts; preserve rejected entries. No binary Plaud parser.',
     obj(source_account_id=S, sources=array(SOURCE)), BATCH, 'write', False),
    ('get_batch', 'Read original texts and per-entry ingestion outcomes in this organization.', obj(batch_id=S), BATCH, 'read', True),
    ('submit_draft', 'Store immutable proposed claims with exact source offsets. Does not call an AI model or approve facts.',
     obj(batch_id=S, claims=array(CLAIM), model_version=N, prompt_version=N), RUN, 'write', False),
    ('get_run', 'Read checks and stale status for a specific draft version.', obj(run_id=S), RUN, 'read', True),
    ('get_history', 'Read up to 100 published artifacts for human consistency review; truncated history blocks new publication.',
     obj(), HISTORY, 'read', True),
    ('review_run', 'Independent human attestation of meaning, coverage and consistency. Cannot override broken citations or structural conflicts.',
     obj(run_id=S, artifact_hash=S, accuracy_pass=B, consistency_pass=B, coverage_confirmed=B, rationale=S), RUN, 'review', False),
    ('publish', 'Atomically publish only a current run with four matching passes; records an outbox event without sending notifications.',
     obj(run_id=S, artifact_hash=S), RUN, 'publish', False),
]
