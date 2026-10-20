"""Review a CRM-neutral JSON snapshot locally. No credentials, network or writes to a CRM."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from preview_recap import safe_url
from recommendations import evidence_from
from review import review_id, write_private_json
from signals import DEFAULT_RULES, classify, compute, prepare_actions, to_dt
from context_links import content_hash

KINDS = {'email': 'emails', 'meeting': 'meetings', 'call': 'calls', 'task': 'tasks', 'note': 'notes'}
STATUSES = {
    'email': {'sent', 'received', 'draft', 'unknown'},
    'meeting': {'scheduled', 'completed', 'cancelled', 'no_show', 'unknown'},
    'call': {'completed', 'missed', 'unknown'},
    'task': {'open', 'completed', 'unknown'},
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value, field, *, empty=False):
    require(isinstance(value, str) and (empty or bool(value.strip())), field + ' must be a string')
    return value


def timestamp(value, field, *, optional=False):
    if optional and value is None:
        return None
    require(isinstance(value, str) and to_dt(value) is not None, field + ' needs an ISO timestamp with timezone')
    return to_dt(value).isoformat()


def checked_rules(value):
    require(isinstance(value, dict), 'rules must be an object')
    require(not set(value) - set(DEFAULT_RULES), 'Unknown rule name')
    for name, number in value.items():
        require(type(number) is int and number > 0, name + ' must be a positive integer')
    return value


def normalize_events(events, as_of):
    """Bridge neutral event fields to the existing signal engine's internal vocabulary."""
    require(isinstance(events, list), 'events must be a list')
    engagements = {kind: [] for kind in KINDS.values()}
    seen, warnings = set(), []
    for event in events:
        require(isinstance(event, dict), 'Each event must be an object')
        kind = event.get('kind')
        require(isinstance(kind, str) and kind in KINDS, 'Unknown event kind')
        source_id = text(event.get('id'), 'event.id')
        identity = (kind, source_id)
        require(identity not in seen, 'Duplicate event ID within a deal')
        seen.add(identity)
        association = event.get('association')
        require(association in ('direct', 'verified', 'ambiguous'), 'An explicit event association is required')
        if association == 'ambiguous':
            warnings.append('Interaction non attribuée avec certitude à ce deal : exclue du diagnostic')
            continue
        if association == 'verified':
            text(event.get('association_reason'), 'event.association_reason')
        at = timestamp(event.get('at'), 'event.at', optional=True)
        title = text(event.get('title', ''), 'event.title', empty=True)
        body = text(event.get('text', ''), 'event.text', empty=True)
        props = {'_source_id': source_id, 'hs_timestamp': at}
        status = event.get('status')
        if kind in STATUSES:
            require(isinstance(status, str) and status in STATUSES[kind], 'Unknown or missing event status')
            if status == 'unknown':
                warnings.append('Statut d’interaction inconnu : vérifier le contexte')
        if at is None:
            warnings.append('Interaction sans date : chronologie incomplète')
        if at and to_dt(at) > as_of and kind in {'email', 'note', 'call'}:
            warnings.append('Interaction postérieure à la capture : vérifier les dates')
        if kind == 'email':
            direction = event.get('direction')
            require(direction in ('inbound', 'outbound'), 'email.direction must be inbound or outbound')
            require(type(event.get('automated')) is bool, 'email.automated must be explicit')
            require((direction == 'inbound' and status in {'received', 'unknown'}) or
                    (direction == 'outbound' and status in {'sent', 'draft', 'unknown'}),
                    'Email status does not match its direction')
            props.update(hs_email_subject=title, hs_email_text=body,
                         hs_email_direction='INCOMING_EMAIL' if direction == 'inbound' and status == 'received'
                         else 'EMAIL' if direction == 'outbound' else 'UNKNOWN',
                         hs_email_status=status.upper(), _automated=event['automated'])
        elif kind == 'meeting':
            props.update(hs_meeting_title=title, hs_meeting_body=body,
                         hs_meeting_start_time=at,
                         hs_meeting_outcome='' if status == 'unknown' else status.upper())
        elif kind == 'call':
            props.update(hs_call_title=title, hs_call_body=body, hs_call_status=status.upper())
        elif kind == 'task':
            props.update(hs_task_subject=title,
                         hs_task_status={'open': 'NOT_STARTED', 'completed': 'COMPLETED', 'unknown': ''}[status])
        else:
            props.update(hs_note_body=body)
        engagements[KINDS[kind]].append(props)
    for items in engagements.values():
        items.sort(key=lambda item: to_dt(item.get('hs_timestamp')) or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
    return engagements, warnings


def analyze_snapshot(snapshot, *, now=None):
    require(isinstance(snapshot, dict) and snapshot.get('schema_version') == '1', 'Unsupported snapshot schema')
    source = text(snapshot.get('source'), 'source')
    captured = timestamp(snapshot.get('as_of'), 'as_of')
    as_of = to_dt(captured)
    require(as_of <= (now or datetime.now(timezone.utc)), 'Snapshot capture time is in the future')
    require(snapshot.get('complete') is True, 'Incomplete collection: finish the selected scope before importing')
    records = snapshot.get('deals')
    require(isinstance(records, list), 'deals must be a list')
    require(type(snapshot.get('expected_deals')) is int and snapshot['expected_deals'] == len(records),
            'Collected deals do not match the declared scope')
    defaults = checked_rules(snapshot.get('rules', {}))
    stage_rules = snapshot.get('stage_rules', {})
    require(isinstance(stage_rules, dict), 'stage_rules must be an object')
    for stage, rules in stage_rules.items():
        text(stage, 'stage_rules key')
        checked_rules(rules)
    rows, seen = [], set()
    for record in records:
        require(isinstance(record, dict), 'Each deal must be an object')
        deal_id = text(record.get('id'), 'deal.id')
        require(deal_id not in seen, 'Duplicate deal ID')
        seen.add(deal_id)
        require(record.get('status') == 'open', 'Only confirmed open deals belong in this snapshot')
        name = text(record.get('name'), 'deal.name')
        stage = text(record.get('stage_id'), 'deal.stage_id')
        label = text(record.get('stage_label'), 'deal.stage_label')
        meaning = text(record.get('stage_context', ''), 'deal.stage_context', empty=True)
        coverage = record.get('coverage')
        require(coverage in ('direct', 'partial', 'verified'), 'Explicit deal coverage is required')
        url = record.get('url')
        require(url is None or safe_url(url), 'deal.url must be HTTPS without credentials')
        step = record.get('next_step', {})
        require(isinstance(step, dict), 'next_step must be an object')
        step_text = text(step.get('text', ''), 'next_step.text', empty=True)
        step_at = timestamp(step.get('due_at'), 'next_step.due_at', optional=True)
        require(not step_at or step_text.strip(), 'A next-step date needs its action text')
        entered = timestamp(record.get('entered_stage_at'), 'entered_stage_at', optional=True)
        require(not entered or to_dt(entered) <= as_of, 'Stage entry cannot be later than capture')
        deal = {'id': deal_id, 'properties': {'dealname': name, 'dealstage': stage,
                'hs_next_step': step_text, 'next_step_due_at': step_at,
                f'hs_v2_date_entered_{stage}': entered}}
        engagements, warnings = normalize_events(record.get('events'), as_of)
        if coverage == 'partial':
            warnings.append('Sources incomplètes : vérifier le contexte avant de préparer un suivi')
        signals = compute(deal, engagements, now=as_of, stage_labels={stage: label},
                          is_automated=lambda email: email.get('_automated', False),
                          rules={**defaults, **stage_rules.get(stage, {})},
                          next_step_date_property='next_step_due_at')
        signals['warnings'] = sorted(set(signals['warnings'] + warnings))
        evidence = evidence_from(deal, engagements)
        coverage_label = {'direct': 'direct_associations_only', 'partial': 'partial_snapshot',
                          'verified': 'verified_snapshot_associations'}[coverage]
        evidence['coverage'] = coverage_label
        row = {'deal_id': deal_id, 'dealname': name, 'deal_url': url, 'stage': label,
               'activity_version': content_hash(sorted(record['events'], key=lambda e: (e['kind'], e['id']))),
               'context_scan': {'status': 'snapshot', 'candidates': [
                   {'source_id': f"{KINDS[event['kind']]}:{event['id']}",
                    'content_hash': content_hash(event), 'properties': event,
                    'via': event.get('via', []), 'status': 'association_to_confirm'}
                   for event in record['events'] if event['association'] == 'ambiguous']},
               'analyzed_at': captured, 'source': source, 'coverage': coverage_label,
               'fictional_example': snapshot.get('fictional_example') is True,
               'bucket': 'unclear' if signals['warnings'] else classify(signals), 'signals': signals,
               'current_next_step': step_text, 'prepared_actions': prepare_actions(signals),
               'review_context': {'context': json.dumps({'deal': name, 'stage_meaning': meaning,
                                    'source': source, 'evidence': evidence}, ensure_ascii=False),
                                  'evidence': evidence, 'signals': signals}}
        row['review_id'] = review_id(row)
        rows.append(row)
    metadata = {'expected_deals': len(records), 'processed_deals': len(rows), 'complete': True,
                'errors': 0, 'source': source, 'captured_at': captured,
                'mode': 'local_snapshot', 'deals_with_warnings': sum(bool(r['signals']['warnings']) for r in rows)}
    return rows, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.suffix != '.json':
        parser.error('Output must end with .json')
    metadata = args.output.with_suffix('.meta.json')
    if args.input.resolve() in {args.output.resolve(), metadata.resolve()}:
        parser.error('Keep the source snapshot separate from the output')
    try:
        rows, meta = analyze_snapshot(json.loads(args.input.read_text()))
    except (ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    write_private_json(args.output, rows)
    write_private_json(metadata, meta)
    print(f'Local review prepared: {len(rows)} open deals. No API call or external action.')


if __name__ == '__main__':
    main()
