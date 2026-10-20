"""Checks target actual failure modes: uncited facts, wrong dates and duplicate plans."""
import copy
import json
import os
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyze
from recommendations import validate_proposal, evidence_from, audit_legacy_dates, prepared_action
from preview_recap import render_recap, render_html

DAY = date(2026, 9, 29)
QUOTE = 'Vous pouvez me recontacter le 2026-10-12 pour préparer la revue.'
EVIDENCE = {'records': [{'source_id': 'emails:example', 'text': QUOTE, 'at': '2026-09-20T10:00:00Z', 'kind': 'emails', 'truncated': False}],
            'coverage': 'direct_associations_only', 'omitted': False, 'truncated': False}


def proposal(**changes):
    result = {'decision': 'wait', 'reason': 'Le contact a précisé quand reprendre la discussion.',
              'action_text': 'Respecter la date de reprise convenue.', 'due_date': '2026-10-12',
              'citations': [{'source_id': 'emails:example', 'quote': QUOTE}]}
    result.update(changes)
    return json.dumps(result, ensure_ascii=False)


class RecommendationTests(unittest.TestCase):
    def check(self, raw=None, evidence=None, signals=None):
        return validate_proposal(raw or proposal(), evidence or EVIDENCE, signals or {}, DAY)

    def test_cited_future_commitment_passes_but_still_requires_human_review(self):
        result = self.check()
        self.assertEqual(result['status'], 'checks_passed')
        self.assertTrue(result['requires_human_review'])
        self.assertEqual(result['citations'][0]['quote'], QUOTE)

    def test_fake_quote_and_source_are_rejected(self):
        for citation in [{'source_id': 'made-up', 'quote': QUOTE},
                         {'source_id': 'emails:example', 'quote': 'Le contact a accepté notre devis.'}]:
            result = self.check(proposal(citations=[citation]))
            self.assertEqual(result['status'], 'needs_review')
            self.assertEqual(result['citations'], [])

    def test_malformed_or_unstructured_output_does_not_escape_to_recap(self):
        for raw in ['Send the proposal immediately.', '[]', '{"decision": {}}',
                    proposal(action_text=['bad']), proposal(citations=[]), proposal(decision=['wait'])]:
            self.assertEqual(self.check(raw)['status'], 'needs_review')

    def test_past_invalid_or_unsupported_deadline_is_rejected(self):
        for value, issue in [('2026-09-20', 'past_deadline'), ('2026-02-30', 'invalid_deadline'),
                             ('2026-10-13', 'unsupported_deadline')]:
            self.assertIn(issue, self.check(proposal(due_date=value))['issues'])

    def test_event_timestamp_does_not_count_as_a_promised_due_date(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence['records'][0]['at'] = '2026-10-13T12:00:00Z'
        self.assertIn('unsupported_deadline', self.check(proposal(due_date='2026-10-13'), evidence)['issues'])

    def test_dates_cannot_bypass_validation_in_free_text(self):
        for text in ['Relancer vendredi 2026-05-30.', 'Call tomorrow.', 'Contacter le 12 octobre.', 'Relancer au 12/10.']:
            self.assertIn('date_in_action_text', self.check(proposal(action_text=text, due_date=None))['issues'])

    def test_planned_meeting_prevents_a_competing_followup(self):
        result = self.check(proposal(decision='prepare_followup'), signals={'planned_actions': [{'kind': 'meeting'}]})
        self.assertIn('existing_planned_action', result['issues'])

    def test_current_action_must_be_considered_not_just_another_email(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence['records'].append({'source_id': 'deal.current_next_step', 'text': 'Attendre la décision du comité.', 'at': None})
        self.assertIn('existing_action_not_cited', self.check(evidence=evidence)['issues'])
        citations = [{'source_id': 'emails:example', 'quote': QUOTE},
                     {'source_id': 'deal.current_next_step', 'quote': 'Attendre la décision du comité.'}]
        result = self.check(proposal(decision='keep_plan', citations=citations), evidence,
                            {'current_next_step': 'Attendre la décision du comité.'})
        self.assertEqual(result['status'], 'checks_passed')

    def test_unresolved_reply_requires_review(self):
        result = self.check(proposal(decision='prepare_followup'),
                            signals={'inbound_after_last_outbound': True})
        self.assertIn('unresolved_inbound_intent', result['issues'])

    def test_undated_existing_action_can_be_replaced_only_when_cited(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence['records'].append({'source_id': 'deal.current_next_step',
                                    'text': 'Send a short recap of the requirements.', 'at': None})
        citations = [{'source_id': 'emails:example', 'quote': QUOTE},
                     {'source_id': 'deal.current_next_step',
                      'quote': 'Send a short recap of the requirements.'}]
        result = self.check(proposal(decision='prepare_followup', due_date=None,
                                     citations=citations), evidence,
                            {'next_step_undated': True,
                             'current_next_step': 'Send a short recap of the requirements.'})
        self.assertEqual(result['status'], 'checks_passed')

    def test_meeting_brief_requires_a_future_meeting_and_meeting_citation(self):
        raw = proposal(decision='prepare_meeting_brief', due_date=None)
        result = self.check(raw)
        self.assertIn('no_scheduled_meeting', result['issues'])
        self.assertIn('meeting_not_cited', result['issues'])
        evidence = copy.deepcopy(EVIDENCE)
        evidence['records'].append({'source_id': 'meetings:next', 'kind': 'meetings',
                                    'text': 'hs_meeting_title: Commercial review meeting',
                                    'at': '2026-10-05T09:00:00Z', 'truncated': False})
        raw = proposal(decision='prepare_meeting_brief', due_date=None,
                       citations=[{'source_id': 'meetings:next',
                                   'quote': 'Commercial review meeting'}])
        result = self.check(raw, evidence,
                            {'planned_actions': [{'kind': 'meeting',
                                                  'at': '2026-10-05T09:00:00Z'}]})
        self.assertEqual(result['status'], 'checks_passed')
        self.assertEqual(prepared_action(result)['kind'], 'meeting_brief')

    def test_omitted_records_require_review_before_a_followup(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence['omitted'] = True
        result = self.check(proposal(decision='prepare_followup', due_date=None), evidence)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIn('incomplete_context', result['issues'])
        action = prepared_action(result)
        self.assertNotEqual(action['kind'], 'followup_proposal')

    def test_refusal_in_a_different_source_cannot_be_ignored_by_citation_selection(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence['records'].append({'source_id': 'notes:other', 'text': 'Buyer said: not interested, do not contact us.', 'at': None})
        self.assertIn('possible_contact_restriction', self.check(proposal(decision='prepare_followup'), evidence)['issues'])

    def test_truncated_evidence_and_existing_draft_prevent_new_followup(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence['truncated'] = True
        evidence['records'].append({'source_id': 'emails:draft', 'text': 'hs_email_status: DRAFT', 'at': None})
        issues = self.check(proposal(decision='prepare_followup'), evidence)['issues']
        self.assertIn('incomplete_context', issues)
        self.assertIn('existing_draft', issues)

    def test_evidence_keeps_real_ids_and_marks_truncation(self):
        pack = evidence_from({'properties': {}}, {'notes': [{'_source_id': 'example', 'hs_note_body': 'a' * 20}]}, max_chars=10)
        self.assertTrue(pack['truncated'])
        self.assertEqual(pack['records'][0]['source_id'], 'notes:example')

    def test_historical_date_failures_are_detected(self):
        self.assertIn('weekday_date_mismatch', audit_legacy_dates('Call by Friday 2026-05-30.', date(2026, 5, 27)))
        self.assertIn('past_date_requires_review', audit_legacy_dates('Lock onboarding date for April 30th meeting.', date(2026, 5, 27)))
        self.assertEqual(audit_legacy_dates('Call by Friday 2026-05-29.', date(2026, 5, 27)), [])

    def test_provider_to_recap_flow_uses_only_validated_content(self):
        client = Mock()
        client.messages.create.return_value = Mock(content=[Mock(text=proposal())], stop_reason='end_turn',
                                                   usage=Mock(input_tokens=20, output_tokens=10))
        with patch.dict(os.environ, {'ANTHROPIC_MODEL': 'offline-mock'}):
            draft = analyze.generate_next_step('unclear', '', client, evidence=EVIDENCE, as_of=datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(draft['validation']['status'], 'checks_passed')
        action = {'kind': 'suggestion', 'title': 'Proposition étayée, à relire', **draft['validation']}
        row = {'deal_id': 'example', 'bucket': 'unclear', 'prepared_actions': [action]}
        summary, blocks = render_recap([row])
        page = render_html([row], summary)
        self.assertIn('lundi 12 octobre 2026', page)
        self.assertIn('Source : Email', json.dumps(blocks, ensure_ascii=False))
        self.assertIn('Source', page)

    def test_rejected_output_is_grouped_for_review_without_unsafe_copy(self):
        unsafe = 'Send the fabricated quote immediately.'
        result = self.check(proposal(action_text=unsafe, citations=[]))
        row = {'deal_id': 'example', 'bucket': 'stalled_post_meeting', 'recommendation_validation': result,
               'prepared_actions': [{'kind': 'suggestion', 'title': 'Vérification requise', 'text': result['text']}]}
        summary, blocks = render_recap([row])
        self.assertIn('1 triage', summary)
        self.assertNotIn(unsafe, json.dumps(blocks))

    def test_wait_label_cannot_smuggle_a_new_followup(self):
        result = self.check(proposal(action_text='Relancer immédiatement pour signer.'))
        self.assertEqual(result['status'], 'checks_passed')
        self.assertNotIn('Relancer immédiatement', result['text'])
        self.assertIn('Respecter le report', result['text'])

    def test_wait_cannot_move_a_known_plan_earlier(self):
        result = self.check(signals={'planned_actions': [{'kind': 'next_step', 'title': 'Rappel', 'at': '2026-10-20T12:00:00Z'}]})
        self.assertIn('deadline_precedes_existing_plan', result['issues'])

    def test_contradictory_weekday_in_a_source_is_not_silently_accepted(self):
        text = 'Call by Friday 2026-10-10 to review the request.'
        evidence = copy.deepcopy(EVIDENCE)
        evidence['records'][0]['text'] = text
        result = self.check(proposal(due_date='2026-10-10', citations=[{'source_id': 'emails:example', 'quote': text}]), evidence)
        self.assertIn('inconsistent_source_date', result['issues'])

    def test_truncated_provider_output_is_not_used_even_if_json_parses(self):
        client = Mock()
        client.messages.create.return_value = Mock(content=[Mock(text=proposal())], stop_reason='max_tokens',
                                                   usage=Mock(input_tokens=20, output_tokens=10))
        with patch.dict(os.environ, {'ANTHROPIC_MODEL': 'offline-mock'}):
            result = analyze.generate_next_step('unclear', '', client, evidence=EVIDENCE)
        self.assertIn('truncated_model_response', result['validation']['issues'])


if __name__ == '__main__':
    unittest.main()
