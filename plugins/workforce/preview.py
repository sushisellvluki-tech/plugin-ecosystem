"""Read-only validation of normalized time rows, never a payroll/posting engine."""
from collections import defaultdict
from datetime import date
import re

from jsonschema import Draft202012Validator
from .schemas import INPUT

RULE_VERSION = 'workforce-preview-v1'
MAX_ROWS = 500
_VALIDATOR = Draft202012Validator(INPUT)
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}')


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def preview(arguments):
    # This also makes direct local use deterministic and rejects extra tenant fields.
    if not _VALIDATOR.is_valid(arguments):
        raise ValueError('Arguments do not match the preview schema')
    as_of = parse_date(arguments['as_of_date'])
    month = arguments['expected_month']
    if as_of is None or (month is not None and parse_date(month + '-01') is None):
        raise ValueError('Expected an ISO date and a nullable YYYY-MM reporting period')
    records = arguments['records']
    if not 1 <= len(records) <= MAX_ROWS:
        raise ValueError('Expected 1 to 500 records per preview')
    for row in records:
        if any(isinstance(v, str) and len(v) > 200 for v in row.values()):
            raise ValueError('Preview fields must not exceed 200 characters')

    issues = []
    source_groups, daily, potential_duplicates, actual_absence = (
        defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list))

    def issue(index, code, message, severity='error'):
        issues.append({'row_index': index, 'code': code, 'severity': severity, 'message': message})

    for index, row in enumerate(records, 1):
        kind = row['record_type']
        for field in ('person_id', 'engagement_id', 'project_id'):
            # Absence does not imply presence at a construction site.
            if field == 'project_id' and kind == 'absence' and row[field] is None:
                continue
            if row[field] is None or not row[field].strip():
                issue(index, 'MISSING_' + field.upper(), 'Assign a confirmed reference before import')
            elif not _ID.fullmatch(row[field]):
                issue(index, 'INVALID_' + field.upper(), 'Use an opaque ID, not a name or placeholder')
        key = row['source_key']
        if not key.strip() or key != key.strip():
            issue(index, 'INVALID_SOURCE_KEY', 'A stable nonblank source-row key is required')
        else:
            source_groups[key].append(index)
        work_date = parse_date(row['work_date'])
        if work_date is None:
            issue(index, 'INVALID_DATE', 'A real ISO work date is required')
        else:
            if month is not None and work_date.strftime('%Y-%m') != month:
                issue(index, 'PERIOD_MISMATCH', 'Date differs from the selected reporting period')
            if work_date > as_of and kind in ('actual', 'absence'):
                issue(index, 'FUTURE_ACTUAL', 'A future date cannot confirm actual work or absence')
        minutes = row['minutes']
        if minutes is None or type(minutes) is not int or not 0 <= minutes <= 1440:
            issue(index, 'INVALID_MINUTES', 'Use integer minutes between 0 and 1440')
        elif kind in ('actual', 'plan') and minutes == 0:
            issue(index, 'ZERO_WORK_TIME', 'Use absence for zero time, or correct the work duration')
        elif kind == 'absence' and minutes != 0:
            issue(index, 'ABSENCE_WITH_TIME', 'Absence cannot include worked minutes')
        if kind == 'unknown':
            issue(index, 'UNKNOWN_RECORD_TYPE', 'Confirm whether this is work, a plan or absence')
        elif kind == 'plan':
            issue(index, 'PLAN_EXCLUDED', 'Planned time must not enter actual costs', 'warning')

        person = row['person_id']
        if person and _ID.fullmatch(person) and work_date:
            day_key = (person, work_date)
            if kind in ('actual', 'absence'):
                actual_absence[day_key].append((index, kind))
            if kind == 'actual' and type(minutes) is int and 0 < minutes <= 1440:
                daily[day_key].append((index, minutes))
        # Similar rows may be separate work intervals: flag for review, never auto-delete.
        if kind in ('actual', 'plan') and all(row[k] and _ID.fullmatch(row[k])
                for k in ('person_id', 'project_id', 'engagement_id')) and work_date:
            potential_duplicates[(person, row['project_id'], row['engagement_id'],
                                  work_date, minutes, kind)].append(index)

    for indices in source_groups.values():
        if len(indices) > 1:
            for index in indices:
                issue(index, 'DUPLICATE_SOURCE_ROW', 'The same source-row key occurs more than once')
    for entries in daily.values():
        if sum(minutes for _, minutes in entries) > 1440:
            for index, _ in entries:
                issue(index, 'DAILY_TIME_EXCEEDED', 'Actual time across all sites exceeds 24 hours')
    for entries in actual_absence.values():
        if {kind for _, kind in entries} == {'actual', 'absence'}:
            for index, _ in entries:
                issue(index, 'WORK_ABSENCE_CONFLICT', 'Work and whole-day absence require reconciliation')
    for indices in potential_duplicates.values():
        if len(indices) > 1:
            for index in indices:
                issue(index, 'POSSIBLE_DUPLICATE', 'Similar work rows require source review', 'warning')

    issues.sort(key=lambda x: (x['row_index'], x['severity'], x['code']))
    bad = {i['row_index'] for i in issues if i['severity'] == 'error'}
    return {'stage': 'preview_only', 'rule_version': RULE_VERSION,
            'total_rows': len(records), 'rows_with_errors': len(bad),
            'rows_without_detected_errors': len(records) - len(bad),
            'has_blockers': bool(bad), 'issues': issues,
            'references_verified': False, 'ready_to_post': False}
