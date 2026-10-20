"""Existing-assistant handoff must preserve scope, evidence and freshness."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review import apply_proposals, review_id, write_private_json
from preview_recap import render_recap

NOW = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
QUOTE = 'We are comparing the two implementation options internally.'


def sample():
    row = {'deal_id': 'fictional-deal', 'analyzed_at': NOW.isoformat(),
           'bucket': 'stalled_post_meeting', 'prepared_actions': [],
           'review_context': {'context': 'Fictional example', 'signals': {}, 'evidence': {
               'records': [{'source_id': 'notes:example', 'text': QUOTE}],
               'coverage': 'direct_associations_only'}}}
    row['review_id'] = review_id(row)
    item = {'deal_id': row['deal_id'], 'review_id': row['review_id'], 'proposal': {
        'decision': 'prepare_followup', 'reason': 'Ask whether either option needs clarification.',
        'action_text': 'Hi, is there anything I can clarify about the two implementation options?',
        'due_date': None, 'citations': [{'source_id': 'notes:example', 'quote': QUOTE}]}}
    return row, item


class ReviewTests(unittest.TestCase):
    def test_cited_copy_survives_into_recap_without_a_model_api(self):
        row, item = sample()
        result = apply_proposals([row], [item], now=NOW)
        self.assertEqual(result[0]['prepared_actions'][0]['kind'], 'followup_proposal')
        self.assertNotIn('review_context', result[0])
        self.assertIn('review_context', row)
        _, blocks = render_recap(result)
        self.assertEqual(json.dumps(blocks).count(item['proposal']['action_text']), 1)

    def test_fabricated_quote_never_appears_as_actionable_copy(self):
        row, item = sample()
        item['proposal']['citations'][0]['quote'] = 'The buyer accepted the contract and will sign.'
        result = apply_proposals([row], [item], now=NOW)
        self.assertEqual(result[0]['recommendation_validation']['status'], 'needs_review')
        self.assertNotIn(item['proposal']['action_text'], json.dumps(result))

    def test_missing_proposal_keeps_the_deal_visible(self):
        row, _ = sample()
        result = apply_proposals([row], [], now=NOW)
        self.assertEqual(len(result), 1)
        self.assertIn('missing_review', result[0]['recommendation_validation']['issues'])

    def test_duplicate_and_unknown_deals_are_rejected(self):
        row, item = sample()
        with self.assertRaises(ValueError):
            apply_proposals([row], [item, item], now=NOW)
        item['deal_id'] = 'another-deal'
        with self.assertRaises(ValueError):
            apply_proposals([row], [item], now=NOW)

    def test_changed_snapshot_and_wrong_proposal_binding_are_rejected(self):
        row, item = sample()
        altered = copy.deepcopy(row)
        altered['review_context']['signals']['planned_actions'] = [{'kind': 'meeting'}]
        with self.assertRaises(ValueError):
            apply_proposals([altered], [item], now=NOW)
        item['review_id'] = 'wrong-snapshot'
        with self.assertRaises(ValueError):
            apply_proposals([row], [item], now=NOW)

    def test_expired_snapshot_requires_a_new_collection(self):
        row, item = sample()
        with self.assertRaises(ValueError):
            apply_proposals([row], [item], now=NOW + timedelta(hours=25))

    def test_followup_cannot_override_a_future_plan(self):
        row, item = sample()
        row['review_context']['signals']['planned_actions'] = [{'kind': 'meeting'}]
        row['review_id'] = review_id(row)
        item['review_id'] = row['review_id']
        result = apply_proposals([row], [item], now=NOW)
        self.assertIn('existing_planned_action', result[0]['recommendation_validation']['issues'])

    def test_private_files_are_created_with_restricted_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.json'
            write_private_json(target, {'private': True})
            self.assertEqual(target.stat().st_mode & 0o077, 0)
            self.assertEqual(json.loads(target.read_text()), {'private': True})


if __name__ == '__main__':
    unittest.main()
