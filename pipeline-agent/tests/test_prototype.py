"""Regression checks for full coverage, relevant signals and user-defined cadence."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyze
from signals import compute, classify, to_dt, prepare_actions
from scheduling import validate_schedule, next_occurrences
from preview_recap import render_recap, slack_messages, render_html

NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


class SignalTests(unittest.TestCase):
    def signals(self, engagements, **kw):
        deal = {'id': '1', 'properties': {'dealstage': 'custom', **kw.pop('props', {})}}
        return compute(deal, engagements, now=NOW, stage_labels={}, is_automated=lambda e: e.get('auto', False), **kw)

    def test_future_and_cancelled_meetings_are_not_past_activity(self):
        s = self.signals({'meetings': [
            {'hs_meeting_start_time': '2026-09-30T10:00:00Z', 'hs_meeting_outcome': 'SCHEDULED'},
            {'hs_meeting_start_time': '2026-09-27T10:00:00Z', 'hs_meeting_outcome': 'CANCELED'},
        ]})
        self.assertFalse(s['had_meeting_ever'])
        self.assertIsNone(s['days_since_personal_touch'])
        self.assertEqual(classify(s), 'action_planned')
        self.assertEqual(prepare_actions(s)[0]['kind'], 'meeting_brief')

    def test_old_inbound_is_not_renewed_by_a_recent_outbound(self):
        s = self.signals({'emails': [
            {'hs_timestamp': '2026-08-01T10:00:00Z', 'hs_email_direction': 'INCOMING_EMAIL'},
            {'hs_timestamp': '2026-09-27T10:00:00Z', 'hs_email_direction': 'EMAIL', 'hs_email_status': 'SENT'},
        ]})
        self.assertFalse(s['inbound_after_last_outbound'])
        self.assertEqual(classify(s), 'recent_activity_no_step_recorded')

    def test_incoming_postponement_requires_review_not_automatic_followup(self):
        s = self.signals({'emails': [{'hs_timestamp': '2026-09-27T10:00:00Z', 'hs_email_direction': 'INCOMING_EMAIL', 'hs_email_text': 'Call me in November'}]})
        self.assertEqual(classify(s), 'needs_reply_sentiment_check')
        actions = prepare_actions(s)
        self.assertEqual([a['kind'] for a in actions], ['reply_review'])
        self.assertIn('Call me in November', actions[0]['text'])

    def test_outbound_draft_and_automated_email_do_not_count_as_activity(self):
        s = self.signals({'emails': [
            {'hs_timestamp': '2026-09-27T10:00:00Z', 'hs_email_direction': 'EMAIL', 'hs_email_status': 'DRAFT'},
            {'hs_timestamp': '2026-09-27T10:00:00Z', 'hs_email_direction': 'EMAIL', 'hs_email_status': 'SENT', 'auto': True},
        ]})
        self.assertIsNone(s['days_since_personal_touch'])

    def test_overdue_task_remains_visible_with_an_upcoming_meeting(self):
        s = self.signals({'tasks': [{'hs_timestamp': '2026-09-25T10:00:00Z', 'hs_task_status': 'NOT_STARTED'}],
                          'meetings': [{'hs_meeting_start_time': '2026-09-30T10:00:00Z', 'hs_meeting_outcome': 'SCHEDULED'}]})
        self.assertEqual(classify(s), 'overdue_action')
        self.assertEqual(len(prepare_actions(s)), 2)

    def test_undated_next_step_prevents_unfounded_no_action_claim(self):
        s = self.signals({}, props={'hs_next_step': 'Buyer asked to reconnect later'})
        self.assertEqual(classify(s), 'next_step_needs_date')
        action = prepare_actions(s)[0]
        self.assertEqual(action['kind'], 'next_step_review')
        self.assertIn('Buyer asked to reconnect later', action['text'])

    def test_past_scheduled_meeting_becomes_a_concrete_review(self):
        s = self.signals({'meetings': [
            {'hs_meeting_start_time': '2026-09-27T10:00:00Z',
             'hs_meeting_outcome': 'SCHEDULED'},
        ]})
        actions = prepare_actions(s)
        self.assertEqual([a['kind'] for a in actions], ['meeting_status_review'])

    def test_thresholds_are_configurable(self):
        events = {'calls': [{'hs_timestamp': '2026-09-08T12:00:00Z', 'hs_call_status': 'COMPLETED'}]}
        self.assertEqual(classify(self.signals(events, rules={'inactivity_days': 30})), 'recent_activity_no_step_recorded')
        self.assertEqual(classify(self.signals(events, rules={'inactivity_days': 10})), 'stalled_pre_meeting')

    def test_invalid_and_timezone_less_timestamps_do_not_crash_or_imply_inactivity(self):
        for date in ['2026-09-28T12:00:00', 'nonsense', '9' * 80]:
            self.assertIsNone(to_dt(date))
        s = self.signals({'meetings': [{'hs_meeting_start_time': 'bad'}]})
        self.assertTrue(s['warnings'])
        self.assertEqual(classify(s), 'unclear')


class ScheduleTests(unittest.TestCase):
    def schedule(self, at='16:15', days=None):
        entry = {'weekdays': [1, 3] if days is None else days, 'at': at}
        return {'mode': 'scheduled', 'timezone': 'Europe/Paris', 'analysis': [entry], 'delivery': [entry]}

    def test_manual_default_has_no_implicit_hour(self):
        self.assertEqual(validate_schedule({}), {'mode': 'manual'})
        self.assertEqual(next_occurrences({}, 'analysis', NOW), [])

    def test_user_days_times_and_timezone(self):
        result = next_occurrences(self.schedule(), 'analysis', NOW)
        self.assertEqual([d.weekday() for d in result], [1, 3, 1])
        self.assertEqual(result[0].isoformat(), '2026-09-29T16:15:00+02:00')

    def test_dst_gap_skipped_and_fall_back_not_duplicated(self):
        schedule = self.schedule('02:30', [6])
        gap = next_occurrences(schedule, 'analysis', datetime(2026, 3, 28, tzinfo=timezone.utc), 1)
        self.assertEqual(gap[0].day, 5)  # April 5; March 29 02:30 does not exist.
        repeat = next_occurrences(schedule, 'analysis', datetime(2026, 10, 24, tzinfo=timezone.utc), 2)
        self.assertEqual([d.day for d in repeat], [25, 1])
        self.assertEqual(repeat[0].fold, 0)

    def test_invalid_settings_fail_without_installing_anything(self):
        for schedule in [self.schedule('25:00'), self.schedule(days=[7]), {**self.schedule(), 'timezone': 'bad-zone'}]:
            with self.assertRaises(ValueError):
                validate_schedule(schedule)


class CoverageTests(unittest.TestCase):
    def test_more_than_ten_deals_are_all_analyzed(self):
        rows = [{'id': str(i), 'properties': {'pipeline': 'p', 'dealstage': 's'}} for i in range(137)]
        settings = {'pipeline_id': 'p', 'stages': {'s': 'Custom'}, 'automation_sender_domains': []}
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            for target, value in [('load_settings', settings), ('fetch_deals', rows)]:
                stack.enter_context(patch.object(analyze, target, return_value=value))
            for target in ['configure', 'validate_open_stages']:
                stack.enter_context(patch.object(analyze, target))
            stack.enter_context(patch.object(analyze, 'analyze_deal', side_effect=lambda d, c: {'deal_id': d['id']}))
            stack.enter_context(patch.object(analyze, 'OUT', Path(temp)))
            stack.enter_context(patch.object(sys, 'argv', ['analyze.py', '--deal-ids', ','.join(d['id'] for d in rows)]))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            analyze.main()
            self.assertEqual(len(json.loads((Path(temp) / 'analysis_adhoc.json').read_text())), 137)
            meta = json.loads((Path(temp) / 'analysis_adhoc.meta.json').read_text())
            self.assertTrue(meta['complete'])
            self.assertEqual(meta['expected_deals'], 137)

    def test_batched_ids_are_not_dropped_above_api_batch_size(self):
        ids = [str(i) for i in range(237)]
        def response(path, body):
            self.assertLessEqual(len(body['inputs']), 100)
            return {'results': [{'id': d['id']} for d in body['inputs']]}
        with patch.object(analyze, '_post', side_effect=response) as request:
            self.assertEqual(len(analyze.fetch_deals(ids)), 237)
            self.assertEqual(request.call_count, 3)

    def test_partial_associations_are_reported_not_silently_ignored(self):
        data = {'results': [{'from': {'id': '1'}, 'to': [], 'paging': {'next': {'after': 'x'}}}]}
        with patch.object(analyze, '_post', return_value=data), self.assertRaises(ValueError):
            analyze.fetch_assoc('deal', 'contact', ['1'])

    def test_large_recap_keeps_every_deal_and_action_within_slack_limits(self):
        rows = [{'deal_id': str(i), 'dealname': f'UNIQUE_DEAL_{i:04}', 'bucket': 'overdue_action',
                 'prepared_actions': [{'title': 'Details', 'text': 'x' * 6500 + f' END_ACTION_{i:04}'}]} for i in range(120)]
        summary, blocks = render_recap(rows)
        messages = slack_messages(summary, blocks)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m['payload']['blocks']) <= 50 for m in messages))
        for b in blocks:
            if b['type'] == 'section':
                self.assertLessEqual(len(b['text']['text']), 3000)
                self.assertEqual(b['text']['type'], 'plain_text')
        text = json.dumps(messages)
        for i in range(120):
            self.assertIn(f'UNIQUE_DEAL_{i:04}', text)
            self.assertIn(f'END_ACTION_{i:04}', text)

    def test_html_escapes_crm_content_and_rejects_executable_links(self):
        rows = [{'deal_id': '1', 'dealname': '<script>alert(1)</script>', 'deal_url': 'javascript:alert(1)', 'bucket': 'unclear'}]
        page = render_html(rows, 'Preview')
        self.assertNotIn('<script>', page)
        self.assertNotIn('href="javascript:', page)
        self.assertIn('&lt;script&gt;', page)


if __name__ == '__main__':
    unittest.main()
