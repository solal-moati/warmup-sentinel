"""Contributor contract: preserve scope, chronology, evidence and human control across CRMs."""
import copy
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from snapshot import analyze_snapshot
from review import apply_proposals
from recommendations import validate_proposal
from preview_recap import card_text, render_recap, slack_messages

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def sample():
    return {'schema_version': '1', 'source': 'custom-crm', 'as_of': NOW.isoformat(),
            'complete': True, 'expected_deals': 1, 'deals': [{
                'id': 'fictional-1', 'name': 'Fictional evaluation', 'status': 'open',
                'stage_id': 's1', 'stage_label': 'Custom stage', 'coverage': 'direct',
                'entered_stage_at': '2026-09-01T09:00:00Z',
                'events': [{'id': 'mail-1', 'kind': 'email', 'association': 'direct',
                            'at': '2026-09-20T09:00:00Z', 'title': 'Your evaluation',
                            'text': 'Here are the two implementation options we discussed.',
                            'direction': 'outbound', 'status': 'sent', 'automated': False}]}]}


class SnapshotTests(unittest.TestCase):
    def test_same_facts_same_diagnosis_despite_crm_and_stage_names(self):
        for source in ('hubspot', 'attio', 'folk', 'custom-crm'):
            value = sample(); value['source'] = source
            value['deals'][0]['stage_label'] = source + ' custom label'
            rows, meta = analyze_snapshot(value, now=NOW)
            self.assertEqual(rows[0]['bucket'], 'stalled_pre_meeting')
            self.assertEqual(meta['processed_deals'], 1)

    def test_cycle_specific_rules_change_inactivity_interpretation(self):
        value = sample(); value['stage_rules'] = {'s1': {'inactivity_days': 45}}
        rows, _ = analyze_snapshot(value, now=NOW)
        self.assertEqual(rows[0]['bucket'], 'recent_activity_no_step_recorded')

    def test_scheduled_meeting_prevents_generic_followup(self):
        value = sample(); value['deals'][0]['events'].append({
            'id': 'meeting', 'kind': 'meeting', 'association': 'direct',
            'at': '2026-10-20T09:00:00Z', 'title': 'Security review', 'status': 'scheduled'})
        rows, _ = analyze_snapshot(value, now=NOW)
        self.assertEqual(rows[0]['bucket'], 'action_planned')
        proposal = self.proposal(rows[0])
        reviewed = apply_proposals(rows, [proposal], now=NOW)
        self.assertIn('existing_planned_action', reviewed[0]['recommendation_validation']['issues'])

    def proposal(self, row):
        return {'deal_id': row['deal_id'], 'review_id': row['review_id'], 'proposal': {
            'decision': 'prepare_followup', 'reason': 'Ask whether the options need clarification.',
            'action_text': 'Is there anything I can clarify about the implementation options?',
            'due_date': None, 'citations': [{'source_id': 'emails:mail-1',
                    'quote': 'Here are the two implementation options we discussed.'}]}}

    def test_notes_from_any_note_taker_remain_citable(self):
        value = sample(); value['deals'][0]['events'].append({
            'id': 'note-1', 'kind': 'note', 'association': 'direct',
            'at': '2026-10-13T09:00:00Z', 'text': 'The security team needs the deployment diagram.'})
        rows, _ = analyze_snapshot(value, now=NOW)
        evidence = rows[0]['review_context']['evidence']['records']
        self.assertTrue(any(r['source_id'] == 'notes:note-1' for r in evidence))

    def test_partial_or_ambiguous_context_blocks_followup_but_keeps_deal(self):
        for mode in ('partial', 'ambiguous'):
            value = sample()
            if mode == 'partial': value['deals'][0]['coverage'] = 'partial'
            else: value['deals'][0]['events'].append({
                'id': 'uncertain', 'kind': 'email', 'association': 'ambiguous'})
            rows, _ = analyze_snapshot(value, now=NOW)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['bucket'], 'unclear')
            reviewed = apply_proposals(rows, [self.proposal(rows[0])], now=NOW)
            self.assertIn('incomplete_context', reviewed[0]['recommendation_validation']['issues'])

    def test_missing_activity_is_not_proof_of_inactivity(self):
        value = sample(); value['deals'][0]['events'] = []
        rows, _ = analyze_snapshot(value, now=NOW)
        self.assertEqual(rows[0]['bucket'], 'unclear')

    def test_automated_email_does_not_count_as_personal_contact(self):
        value = sample(); value['deals'][0]['events'][0]['automated'] = True
        rows, _ = analyze_snapshot(value, now=NOW)
        self.assertIsNone(rows[0]['signals']['days_since_personal_touch'])

    def test_refusal_in_inbound_message_is_reviewed_not_followed_up(self):
        value = sample(); value['deals'][0]['events'].append({
            'id': 'incoming', 'kind': 'email', 'association': 'direct',
            'at': '2026-10-13T09:00:00Z', 'direction': 'inbound',
            'status': 'received', 'automated': False, 'text': 'Not interested. Do not contact us.'})
        rows, _ = analyze_snapshot(value, now=NOW)
        reviewed = apply_proposals(rows, [self.proposal(rows[0])], now=NOW)
        issues = reviewed[0]['recommendation_validation']['issues']
        self.assertIn('unresolved_inbound_intent', issues)
        self.assertIn('possible_contact_restriction', issues)

    def test_incomplete_collection_duplicates_and_closed_deals_rejected(self):
        values = []
        value = sample(); value['complete'] = False; values.append(value)
        value = sample(); value['expected_deals'] = 2; values.append(value)
        value = sample(); value['deals'][0]['status'] = 'won'; values.append(value)
        value = sample(); value['deals'] *= 2; value['expected_deals'] = 2; values.append(value)
        value = sample(); value['deals'][0]['events'] *= 2; values.append(value)
        for value in values:
            with self.assertRaises(ValueError): analyze_snapshot(value, now=NOW)

    def test_ambiguous_dates_and_unsafe_links_rejected(self):
        for field, bad in [('url', 'javascript:alert(1)'), ('entered_stage_at', '2026-09-01T09:00:00')]:
            value = sample(); value['deals'][0][field] = bad
            with self.assertRaises(ValueError): analyze_snapshot(value, now=NOW)

    def test_no_deal_cap_and_all_deals_reach_recap(self):
        value = sample(); base = value['deals'][0]
        value['deals'] = [{**copy.deepcopy(base), 'id': str(i), 'name': f'Deal {i}'} for i in range(120)]
        value['expected_deals'] = 120
        rows, meta = analyze_snapshot(value, now=NOW)
        summary, blocks = render_recap(rows)
        self.assertEqual(meta['processed_deals'], 120)
        self.assertIn('Deal 119', json.dumps(slack_messages(summary, blocks)))

    def test_import_does_not_make_stale_source_look_fresh(self):
        rows, _ = analyze_snapshot(sample(), now=NOW + timedelta(days=2))
        with self.assertRaises(ValueError):
            apply_proposals(rows, [self.proposal(rows[0])], now=NOW + timedelta(days=2))

    def test_omitted_evidence_blocks_an_actionable_generated_draft(self):
        rows, _ = analyze_snapshot(sample(), now=NOW)
        context = rows[0]['review_context']; context['evidence']['omitted'] = True
        result = validate_proposal(json.dumps(self.proposal(rows[0])['proposal']),
                                   context['evidence'], context['signals'], NOW.date())
        self.assertIn('incomplete_context', result['issues'])

    def test_coverage_is_visible_in_recap(self):
        value = sample(); value['deals'][0]['coverage'] = 'partial'
        rows, _ = analyze_snapshot(value, now=NOW)
        self.assertIn('export partiel', card_text(rows[0]))


if __name__ == '__main__':
    unittest.main()
