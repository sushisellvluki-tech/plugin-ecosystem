"""Canonical preview schemas; adapters derive their exports from this catalog."""


def obj(properties):
    return {'type': 'object', 'properties': properties,
            'required': list(properties), 'additionalProperties': False}


TEXT = {'type': 'string'}
MAYBE_TEXT = {'type': ['string', 'null']}
ROW = obj({
    'source_key': TEXT,
    'person_id': MAYBE_TEXT,
    'project_id': MAYBE_TEXT,
    'engagement_id': MAYBE_TEXT,
    'work_date': MAYBE_TEXT,
    'minutes': {'type': ['integer', 'null']},
    'record_type': {'type': 'string', 'enum': ['actual', 'plan', 'absence', 'unknown']},
})
INPUT = obj({
    'as_of_date': TEXT,
    'expected_month': MAYBE_TEXT,
    'records': {'type': 'array', 'items': ROW},
})
ISSUE = obj({
    'row_index': {'type': 'integer'},
    'code': TEXT,
    'severity': {'type': 'string', 'enum': ['error', 'warning']},
    'message': TEXT,
})
OUTPUT = obj({
    'stage': {'type': 'string', 'enum': ['preview_only']},
    'rule_version': TEXT,
    'total_rows': {'type': 'integer'},
    'rows_with_errors': {'type': 'integer'},
    'rows_without_detected_errors': {'type': 'integer'},
    'has_blockers': {'type': 'boolean'},
    'issues': {'type': 'array', 'items': ISSUE},
    'references_verified': {'type': 'boolean', 'enum': [False]},
    'ready_to_post': {'type': 'boolean', 'enum': [False]},
})
