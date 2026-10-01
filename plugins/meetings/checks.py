"""Deterministic evidence checks. A matching quote is not a semantic fact check."""
import hashlib
import json

RULE_VERSION = 'meetings-manual-review-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def evidence_errors(claims, entries):
    errors = []
    if not 1 <= len(claims) <= 50:
        return ['Expected 1 to 50 claims']
    for i, claim in enumerate(claims):
        if any(not isinstance(claim[k], str) or not claim[k].strip() or len(claim[k]) > 2000
               for k in ('quote', 'text', 'subject', 'predicate', 'value')):
            errors.append(f'Claim {i}: blank or oversized field')
        index, start = claim['source_index'], claim['start']
        if type(index) is not int or not 0 <= index < len(entries) or type(start) is not int or start < 0:
            errors.append(f'Claim {i}: invalid source position')
            continue
        entry = entries[index]
        if entry['status'] == 'rejected' or entry['text'][start:start+len(claim['quote'])] != claim['quote']:
            errors.append(f'Claim {i}: quotation differs from original text')
    covered = {c['source_index'] for c in claims}
    if covered != set(range(len(entries))):
        errors.append('Every batch source must be represented; coverage still requires human review')
    return errors


def consistency_errors(claims, history):
    known, errors = {}, []
    for claim in claims:
        key = (claim['subject'].strip().casefold(), claim['predicate'].strip().casefold())
        value = claim['value'].strip().casefold()
        if key in known and known[key] != value:
            errors.append('Conflicting values for the same subject/predicate in this batch')
        known[key] = value
    for artifact in history:
        for claim in artifact['claims']:
            key = (claim['subject'].strip().casefold(), claim['predicate'].strip().casefold())
            if key in known and known[key] != claim['value'].strip().casefold():
                errors.append('Conflict with published history; clarify subject/predicate context in a new draft')
    return sorted(set(errors))
