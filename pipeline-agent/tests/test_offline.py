"""Offline checks for migration boundaries; no API calls, real data or tokens."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyze
import runtime
import preview_recap


class MigrationBoundaries(unittest.TestCase):
    def test_crm_writes_are_rejected_before_transport(self):
        session = Mock()
        with patch.object(runtime, '_SESSION', session):
            for method, path in [('PATCH', 'crm/v3/objects/deals/123'),
                                 ('POST', 'crm/v3/objects/notes'),
                                 ('DELETE', 'crm/v3/objects/deals/123')]:
                with self.assertRaises(ValueError):
                    runtime.request_read(method, path, body={})
        session.request.assert_not_called()

    def test_search_post_is_a_read(self):
        response = Mock(status_code=200)
        response.json.return_value = {'results': []}
        session = Mock()
        session.request.return_value = response
        with patch.object(runtime, '_SESSION', session):
            self.assertEqual(runtime.request_read('POST', 'crm/v3/objects/deals/search', body={}), {'results': []})
        self.assertEqual(session.request.call_args.kwargs['timeout'], (10, 45))

    def test_stage_id_does_not_determine_whether_deal_is_closed(self):
        settings = {'pipeline_id': 'p', 'stages': {'closedwon': 'Negotiation'}}
        for is_closed, allowed in [('false', True), ('true', False), (None, False)]:
            result = {'results': [{'id': 'p', 'stages': [{'id': 'closedwon', 'metadata': {'isClosed': is_closed}}]}]}
            with patch.object(runtime, 'request_read', return_value=result):
                if allowed:
                    runtime.validate_open_stages(settings)
                else:
                    with self.assertRaises(ValueError):
                        runtime.validate_open_stages(settings)

    def test_explicit_deal_ids_stay_in_scope_and_do_not_generate_by_default(self):
        settings = {'pipeline_id': 'p', 'stages': {'open': 'Demo'}, 'automation_sender_domains': []}
        deals = [
            {'id': '1', 'properties': {'pipeline': 'p', 'dealstage': 'open', 'dealname': 'Fictional open'}},
            {'id': '2', 'properties': {'pipeline': 'p', 'dealstage': 'lost', 'dealname': 'Fictional lost'}},
            {'id': '3', 'properties': {'pipeline': 'other', 'dealstage': 'open', 'dealname': 'Fictional other pipeline'}},
        ]
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'argv', ['analyze.py', '--deal-ids', '1,2,3']))
            stack.enter_context(patch.object(analyze, 'load_settings', return_value=settings))
            stack.enter_context(patch.object(analyze, 'configure'))
            stack.enter_context(patch.object(analyze, 'validate_open_stages'))
            stack.enter_context(patch.object(analyze, 'fetch_deals', return_value=deals))
            stack.enter_context(patch.object(analyze, 'fetch_assoc', return_value={}))
            stack.enter_context(patch.object(analyze, 'batch_read', return_value={}))
            stack.enter_context(patch.object(analyze, 'fetch_engagements', return_value={k: [] for k in ['notes','emails','calls','meetings']}))
            stack.enter_context(patch.object(analyze, 'OUT', Path(temp)))
            generate = stack.enter_context(patch.object(analyze, 'generate_next_step', side_effect=AssertionError('No paid call expected')))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            analyze.main()
            rows = json.loads((Path(temp) / 'analysis_adhoc.json').read_text())
            self.assertEqual([r['deal_id'] for r in rows], ['1'])
            self.assertIsNone(rows[0]['suggested_next_step'])
            self.assertEqual(rows[0]['sonnet_usage'], {'input': 0, 'output': 0})
            generate.assert_not_called()

    def test_failed_analysis_remains_visible_in_recap(self):
        summary, blocks = preview_recap.render_recap([{'deal_id': 'fictional-error', 'error': 'CRM unavailable'}], date(2026, 10, 2))
        self.assertIn('1 triage', summary)
        self.assertIn('fictional-error', json.dumps(blocks))

    def test_example_config_cannot_select_a_real_pipeline_accidentally(self):
        with self.assertRaises(ValueError):
            runtime.load_settings(runtime.PROJECT_DIR / 'config.example.json')


if __name__ == '__main__':
    unittest.main()
