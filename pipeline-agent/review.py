"""Validate an existing assistant's proposals locally, without CRM or model calls."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from recommendations import validate_proposal, prepared_action, rejected
from signals import to_dt


def review_id(row):
    payload = {key: row.get(key) for key in ('deal_id', 'analyzed_at', 'review_context')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_private_json(path, value):
    """Create the temporary file with private permissions before writing data."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.review-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_proposals(rows, proposals, *, now=None, max_age_hours=24):
    now = now or datetime.now(timezone.utc)
    if not isinstance(rows, list) or not isinstance(proposals, list):
        raise ValueError('Analysis and proposals must be JSON lists')
    row_ids = [str(row['deal_id']) for row in rows]
    if len(set(row_ids)) != len(row_ids):
        raise ValueError('Duplicate deals in analysis')
    by_id = {}
    for item in proposals:
        if not isinstance(item, dict) or set(item) != {'deal_id', 'review_id', 'proposal'}:
            raise ValueError('Each proposal needs deal_id, review_id and proposal')
        deal_id = str(item['deal_id'])
        if deal_id in by_id or deal_id not in row_ids:
            raise ValueError('Duplicate or out-of-scope proposal')
        by_id[deal_id] = item
    output = []
    generated_kinds = {'followup_proposal', 'keep_plan', 'wait', 'review', 'suggestion'}
    for original in rows:
        row = copy.deepcopy(original)
        item = by_id.get(str(row['deal_id']))
        source = row.get('review_context')
        captured = to_dt(row.get('analyzed_at'))
        if row.get('error'):
            output.append(row)
            continue
        if not source or not captured or row.get('review_id') != review_id(row):
            raise ValueError('Missing or altered review context; collect a fresh export')
        if captured > now or now - captured > timedelta(hours=max_age_hours):
            raise ValueError('Review context is stale; collect a fresh export')
        if item is None:
            validation = rejected(['missing_review'])
        else:
            if item['review_id'] != row['review_id']:
                raise ValueError('Proposal belongs to another snapshot')
            validation = validate_proposal(json.dumps(item['proposal'], ensure_ascii=False),
                                           source['evidence'], source['signals'], now.date())
            if isinstance(item['proposal'], dict) and item['proposal'].get('decision') == 'prepare_followup' and any(
                c.get('status') != 'completed' and (c.get('side') in {'seller', 'unknown'} or c.get('blocked_by'))
                for c in row.get('commitments', [])
            ):
                validation = rejected(['pending_commitment_requires_review'])
        generated = prepared_action(validation)
        actions = [a for a in row.get('prepared_actions', [])
                   if a.get('kind') not in generated_kinds
                   and not (a.get('kind') == 'meeting_brief' and a.get('citations'))]
        if generated['kind'] == 'meeting_brief':
            actions = [a for a in actions if a.get('kind') != 'meeting_brief']
        row.update(prepared_actions=actions + [generated], recommendation_validation=validation,
                   suggested_next_step=validation['text'], generation_mode='existing_assistant',
                   reviewed_at=now.isoformat())
        row.pop('review_context', None)
        output.append(row)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--proposals', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.resolve() in {args.input.resolve(), args.proposals.resolve()}:
        parser.error('Choose a separate output file to preserve the source snapshot')
    metadata_path = args.input.with_suffix('.meta.json')
    if not metadata_path.is_file():
        parser.error('The collection metadata file is required')
    metadata = json.loads(metadata_path.read_text())
    if not metadata.get('complete') or metadata.get('errors'):
        parser.error('Collection incomplete or contains errors; resolve before reviewing')
    rows = json.loads(args.input.read_text())
    if metadata.get('processed_deals') != len(rows) or metadata.get('expected_deals') != len(rows):
        parser.error('Collection counts do not match the input')
    result = apply_proposals(rows, json.loads(args.proposals.read_text()))
    write_private_json(args.output, result)
    write_private_json(args.output.with_suffix('.meta.json'), {
        **metadata, 'generation_mode': 'existing_assistant',
        'reviewed_deals': sum((r.get('recommendation_validation') or {}).get('status') == 'checks_passed'
                              for r in result),
    })
    print(f'Review complete: {len(result)} deals. Local output: {args.output}')


if __name__ == '__main__':
    main()
